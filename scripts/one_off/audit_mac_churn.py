#!/usr/bin/env python
"""Measure how often a PPPoE login's observed device MAC changes, and how.

Read-only. This is the measurement that has to come BEFORE deciding whether to
enforce a per-service device binding. Enforcing without it would either break
ordinary device replacement or fail to catch sharing, and there is currently no
evidence about which is common.

MikroTik reports the PPPoE client MAC as ``Calling-Station-Id``, so the
authoritative history is external ``radacct``. ``Subscription.mac_address`` is
NOT used here: it is legacy ambiguous state, part operator-entered and part
written by the accounting importer before that write was removed, and the two
are no longer distinguishable.

The discriminator is not "how many MACs" but HOW they interleave:

  ``stable``       one MAC across the window.
  ``replaced``     MACs appear in clean succession -- the old one stops before
                   the new one starts. This is ordinary CPE replacement and
                   must not be flagged.
  ``interleaved``  two or more MACs alternate or overlap in time. One credential
                   in use from more than one device, which is the sharing signal
                   an enforced binding would be aimed at.
  ``churning``     many distinct MACs. Either a bridged/rotating CPE or a widely
                   shared credential; needs a human, not a rule.

Interleaving is judged by overlap of each MAC's observed window. A device that
is replaced yields disjoint windows; a credential in two places yields
overlapping ones. Sessions are attributed to a MAC only when the value parses
to twelve hex digits -- an unreadable ``Calling-Station-Id`` makes a login
``unparseable`` rather than silently reducing its distinct-MAC count.

Usage (inside the app container so the DB resolves):

    docker compose exec app python scripts/one_off/audit_mac_churn.py
    ... --days 30 --json

Exit status is non-zero when any login is interleaved, churning or unparseable.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Column, DateTime, String, select

from app.db import SessionLocal

_MAC_HEX_RE = re.compile(r"[^0-9A-Fa-f]")

#: More distinct devices than this in the window is not a replacement story.
CHURN_THRESHOLD = 4


def _normalize_mac(value: object) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    compact = _MAC_HEX_RE.sub("", raw)
    if len(compact) != 12:
        return None
    return ":".join(compact[i : i + 2] for i in range(0, 12, 2)).upper()


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _windows_overlap(
    first: tuple[datetime, datetime], second: tuple[datetime, datetime]
) -> bool:
    """Two observation windows overlap in time.

    Replacement yields disjoint windows; one credential in two places yields
    overlapping ones. Touching endpoints are not an overlap -- a CPE swap can
    legitimately produce a stop and a start in the same second.
    """
    return first[0] < second[1] and second[0] < first[1]


def classify(windows: list[tuple[datetime, datetime]]) -> str:
    """Classify a login from its per-MAC observation windows."""
    if not windows:
        return "unobserved"
    if len(windows) == 1:
        return "stable"
    if len(windows) > CHURN_THRESHOLD:
        return "churning"
    ordered = sorted(windows, key=lambda item: item[0])
    for index, current in enumerate(ordered):
        for later in ordered[index + 1 :]:
            if _windows_overlap(current, later):
                return "interleaved"
    return "replaced"


def _radacct_table():
    from app.services.radius import _external_radius_table

    return _external_radius_table(
        "radacct",
        Column("username", String),
        Column("callingstationid", String),
        Column("acctstarttime", DateTime),
        Column("acctstoptime", DateTime),
        Column("acctupdatetime", DateTime),
    )


def audit_mac_churn(db, *, days: int) -> dict[str, Any]:
    from app.services.radius import (
        _active_external_sync_configs,
        _get_external_engine,
    )

    now = datetime.now(UTC)
    cutoff = now - timedelta(days=days)
    radacct = _radacct_table()

    # login -> mac -> [first_seen, last_seen]
    seen: dict[str, dict[str, list[datetime]]] = defaultdict(dict)
    unparseable: dict[str, int] = defaultdict(int)
    sessions = 0
    errors: list[str] = []

    configs = _active_external_sync_configs(db)
    if not configs:
        return {
            "ok": False,
            "error": "no external RADIUS configured; nothing to measure",
        }

    for config in configs:
        try:
            engine = _get_external_engine(config["db_url"])
            with engine.connect() as conn:
                rows = conn.execute(
                    select(
                        radacct.c.username,
                        radacct.c.callingstationid,
                        radacct.c.acctstarttime,
                        radacct.c.acctstoptime,
                        radacct.c.acctupdatetime,
                    ).where(radacct.c.acctstarttime >= cutoff)
                ).all()
            for row in rows:
                login = (row.username or "").strip()
                if not login:
                    continue
                sessions += 1
                mac = _normalize_mac(row.callingstationid)
                if mac is None:
                    unparseable[login] += 1
                    continue
                start = _aware(row.acctstarttime)
                # An open session is observed up to now, not to its start --
                # otherwise a live session looks like an instant and never
                # overlaps anything.
                end = _aware(row.acctstoptime) or _aware(row.acctupdatetime) or now
                if start is None:
                    continue
                if end < start:
                    end = start
                window = seen[login].get(mac)
                if window is None:
                    seen[login][mac] = [start, end]
                else:
                    window[0] = min(window[0], start)
                    window[1] = max(window[1], end)
        except Exception as exc:  # noqa: BLE001 - reported, never guessed around
            errors.append(str(exc))

    findings: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for login, macs in seen.items():
        windows = [(first, last) for first, last in macs.values()]
        verdict = classify(windows)
        row = {
            "login": login,
            "distinct_macs": len(macs),
            "verdict": verdict,
            "macs": [
                {
                    "mac": mac,
                    "first_seen": window[0].isoformat(),
                    "last_seen": window[1].isoformat(),
                }
                for mac, window in sorted(macs.items(), key=lambda kv: kv[1][0])
            ],
        }
        if unparseable.get(login):
            row["unparseable_sessions"] = unparseable[login]
        findings[verdict].append(row)

    for login, count in unparseable.items():
        if login not in seen:
            findings["unparseable"].append(
                {
                    "login": login,
                    "unparseable_sessions": count,
                    "verdict": "unparseable",
                }
            )

    counts = {verdict: len(rows) for verdict, rows in sorted(findings.items())}
    needs_review = (
        counts.get("interleaved", 0)
        + counts.get("churning", 0)
        + counts.get("unparseable", 0)
    )
    return {
        "window_days": days,
        "sessions_examined": sessions,
        "logins_observed": len(seen),
        "counts": counts,
        "needs_review": needs_review,
        "errors": errors,
        "findings": findings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30, help="window, default 30")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--limit", type=int, default=20, help="rows per verdict in human output"
    )
    args = parser.parse_args()

    session = SessionLocal()
    try:
        result = audit_mac_churn(session, days=args.days)
    finally:
        session.close()

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if not result.get("needs_review") else 1

    if result.get("error"):
        print(result["error"])
        return 2
    print(
        f"MAC churn over {result['window_days']}d: "
        f"{result['sessions_examined']} sessions, "
        f"{result['logins_observed']} logins"
    )
    for verdict, count in result["counts"].items():
        print(f"  {verdict:14} {count}")
    if result["errors"]:
        print(f"  !! incomplete read: {result['errors']}")
    for verdict in ("interleaved", "churning", "unparseable"):
        rows = result["findings"].get(verdict, [])
        if not rows:
            continue
        print(f"\n--- {verdict} ---")
        for row in rows[: args.limit]:
            macs = ", ".join(item["mac"] for item in row.get("macs", []))
            print(f"  {row['login']:12} {row['distinct_macs']:>2} MACs  {macs}")
    return 0 if not result["needs_review"] else 1


if __name__ == "__main__":
    sys.exit(main())
