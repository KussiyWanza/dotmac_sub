#!/usr/bin/env python
"""Audit: live PPPoE session IP vs the exact-service assignment that owns it.

Read-only. Complements ``scripts/one_off/audit_ip_consistency.py``, which
compares the three DESIRED sources (``subscriptions.ipv4_address`` column /
IPAM ``IPAssignment`` / external ``radreply`` Framed-IP). This script adds the
observation that audit never reads — the address the subscriber is **actually
authenticating with right now** (``radius_active_sessions``, the canonical
mirror of external ``radacct``) — plus the duplicate, ambiguity and integrity
classes neither audit covers.

Ownership model (docs/designs/IP_ASSIGNMENT_LIFECYCLE_SOT.md): the active
exact-service ``IPAssignment`` is the desired-address authority.
``Subscription.ipv4_address`` is a rebuildable projection, RADIUS Framed-IP is
a projection, and the live session is an observation. This script therefore
adjudicates against the assignment, NEVER against the served column, and fails
closed rather than choosing an owner when the ledger or the login binding is
ambiguous.

"Live" is decided by ``network.radius_sessions.ACTIVE_SESSION_FRESHNESS``, the
session resolver's own policy. This script does not accept a window override:
what counts as an active session is that owner's decision, not a CLI flag's.

Findings — per-session verdicts:

  - ``session_ip_conflict``  — the live Framed-IP is actively assigned to a
    DIFFERENT service (or, for a legacy unbound assignment, a different
    subscriber). The customer-visible collision. Decided on the assignment
    ledger alone, so it still fires when this subscription has no owning
    assignment or when its served column happens to match the stale address.
  - ``session_ip_mismatch`` — the live Framed-IP differs from the address this
    subscription's single active exact-service assignment owns.
  - ``session_ip_untracked`` — the live Framed-IP exists in no IPAM record.
  - ``session_no_subscription`` — live session whose login resolves to no
    projected subscription.

Findings — ledger health for subscriptions in scope:

  - ``duplicate_login_subscription`` — one login carries more than one ACTIVE
    subscription. ``radius_session_reconcile._resolve_active_subs`` binds the
    session to the LOWEST subscription id, so the stored ``subscription_id`` is
    a deterministic guess rather than a fact. Fail closed: no per-session
    verdict is issued for that login's sessions.
  - ``ambiguous_service_assignment`` — the subscription has more than one
    active exact-service IPv4 assignment, so no owner can be named. No
    ``session_ip_mismatch`` verdict is issued for it. A ``session_ip_conflict``
    still can be, because a conflict is decided by the OTHER party's ownership
    of the observed address, not by this subscription's desired state.
    (``radius_population`` instead picks one via ``setdefault`` over an
    unordered query — an unauthorized decision this audit refuses to
    reproduce.)
  - ``legacy_unbound_assignment`` — the address is backed only by a
    subscriber-level assignment with ``subscription_id IS NULL``. Adjudicable
    at subscriber grain but not at exact-service grain; migration debt, not a
    missing allocation.
  - ``served_projection_unowned`` — the served column carries an address that
    no active assignment backs at either grain. Not adjudicable; repair the
    ledger first.
  - ``served_projection_stale`` — the owning assignment and the served column
    disagree.

Findings — duplicates and integrity:

  - ``duplicate_session_ip`` — two or more live sessions on one Framed-IP.
  - ``duplicate_served_projection`` — two or more subscriptions carrying the
    same ``ipv4_address``. The ledger cannot express a duplicate owner (the
    partial-unique index forbids it), so a duplicate can only enter through the
    unconstrained served column — which ``radius_population`` prefers over the
    assignment and projects straight into ``radreply`` as a duplicate Framed-IP.
    This is the guardrail gap, not a competing source of truth.
  - ``ledger_integrity_violation`` — one address carries more than one ACTIVE
    assignment, counting exact-service and legacy subscriber-level rows alike.
    Structurally impossible while ``uq_ip_assignments_ipv4_active`` exists; if
    it fires, that index is missing or disabled on this database.

Scoping:

``--nas`` matches case-insensitively against the NAS device name, code, or
``nas_ip``, and against the session's raw ``nas_ip_address`` so sessions whose
``nas_device_id`` never resolved are still scoped. Per-session and ledger-health
findings cover the scoped set only. The two duplicate classes are computed
FLEET-WIDE and reported when a group touches the scoped set — a collision's
counterparty is frequently on another NAS — with each member labelled in or out
of scope. ``ledger_integrity_violation`` ignores scoping entirely: a broken
uniqueness guarantee is a database-wide fact.

Usage (inside the app container so the DB resolves):

    docker compose exec app python scripts/one_off/audit_nas_session_ip_divergence.py
    docker compose exec app python scripts/one_off/audit_nas_session_ip_divergence.py --nas eagle
    docker compose exec app python scripts/one_off/audit_nas_session_ip_divergence.py --nas eagle --full

Exit status is non-zero when any finding exists.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.catalog import NasDevice, Subscription, SubscriptionStatus
from app.models.network import IPAssignment, IPv4Address, IPVersion
from app.models.radius_active_session import RadiusActiveSession
from app.services.network.radius_sessions import ACTIVE_SESSION_FRESHNESS
from app.services.radius_access_state import ACTIVE_STATUSES, BLOCKED_STATUSES

SAMPLE_LIMIT = 25

_FINDING_KINDS = (
    "session_ip_conflict",
    "session_ip_mismatch",
    "duplicate_session_ip",
    "duplicate_served_projection",
    "duplicate_login_subscription",
    "ledger_integrity_violation",
    "ambiguous_service_assignment",
    "legacy_unbound_assignment",
    "served_projection_unowned",
    "served_projection_stale",
    "session_ip_untracked",
    "session_no_subscription",
)


def _norm(ip: object) -> str:
    """Canonical host-string form, tolerant of junk and of inet /mask text."""
    if not ip:
        return ""
    text = str(ip).strip().split("/", 1)[0]
    if not text:
        return ""
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return text


def _aware(value: datetime | None) -> datetime | None:
    """Treat a naive timestamp as UTC so the freshness compare never raises."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _matches_nas(needle: str, *values: object) -> bool:
    return any(needle in str(v).lower() for v in values if v)


def _active_ipv4_assignments(db: Session) -> list[tuple[str, str, str]]:
    """Every ACTIVE IPv4 assignment as (address, subscription_id, subscriber_id).

    Legacy rows with ``subscription_id IS NULL`` are included — they still hold
    the address, so excluding them would make a live collision against one look
    like an unowned address.
    """
    rows: list[tuple[str, str, str]] = []
    for subscriber_id, subscription_id, address in db.execute(
        select(
            IPAssignment.subscriber_id,
            IPAssignment.subscription_id,
            IPv4Address.address,
        )
        .join(IPv4Address, IPAssignment.ipv4_address_id == IPv4Address.id)
        .where(IPAssignment.is_active.is_(True))
        .where(IPAssignment.ip_version == IPVersion.ipv4)
    ).all():
        norm = _norm(address)
        if norm:
            rows.append(
                (
                    norm,
                    str(subscription_id) if subscription_id is not None else "",
                    str(subscriber_id) if subscriber_id is not None else "",
                )
            )
    return rows


def _subscription_state(
    db: Session, assignments: list[tuple[str, str, str]]
) -> tuple[dict[str, dict[str, Any]], dict[str, list[str]]]:
    """Per-projected-subscription ledger view, plus the duplicate-login index.

    ``owner_ip`` is set only when exactly ONE active exact-service IPv4
    assignment exists — ambiguity is reported, never resolved. Legacy
    subscriber-level assignments are attributed only when the subscriber has a
    single projected subscription, the same guard ``radius_population`` applies.
    """
    exact: dict[str, list[str]] = {}
    legacy: dict[str, list[str]] = {}
    for address, subscription_id, subscriber_id in assignments:
        if subscription_id:
            exact.setdefault(subscription_id, []).append(address)
        elif subscriber_id:
            legacy.setdefault(subscriber_id, []).append(address)

    # Same population as radius_population.populate(): a blocked/suspended sub
    # still carries a projected login, so a live session on one is adjudicable
    # rather than an unresolvable orphan.
    subs = db.scalars(
        select(Subscription).where(
            Subscription.status.in_(ACTIVE_STATUSES | BLOCKED_STATUSES)
        )
    ).all()
    service_counts = Counter(str(sub.subscriber_id) for sub in subs)

    state: dict[str, dict[str, Any]] = {}
    active_by_login: dict[str, list[str]] = {}
    for sub in subs:
        sub_id = str(sub.id)
        subscriber_id = str(sub.subscriber_id)
        login = (sub.login or "").strip()
        column_ip = _norm(sub.ipv4_address)
        if column_ip == "0.0.0.0":  # nosec B104  # noqa: S104 — string compare
            column_ip = ""
        owner_ips = sorted(set(exact.get(sub_id, [])))
        legacy_ips = (
            sorted(set(legacy.get(subscriber_id, [])))
            if service_counts[subscriber_id] == 1
            else []
        )
        state[sub_id] = {
            "subscription_id": sub_id,
            "subscriber_id": subscriber_id,
            "login": login,
            "column_ip": column_ip,
            "owner_ips": owner_ips,
            "legacy_owner_ips": legacy_ips,
            "owner_ip": owner_ips[0] if len(owner_ips) == 1 else "",
            "ambiguous": len(owner_ips) > 1,
        }
        # Mirrors radius_session_reconcile._resolve_active_subs, which binds a
        # session by ACTIVE status only.
        if login and sub.status == SubscriptionStatus.active:
            active_by_login.setdefault(login, []).append(sub_id)

    duplicate_logins = {
        login: sorted(ids) for login, ids in active_by_login.items() if len(ids) > 1
    }
    return state, duplicate_logins


def _address_holders(
    assignments: list[tuple[str, str, str]],
) -> tuple[dict[str, dict[str, str]], dict[str, list[dict[str, str]]]]:
    """address -> the single ACTIVE assignment holding it, plus violations.

    ``uq_ip_assignments_ipv4_active`` (migration 177) permits one active
    assignment per address, so this mapping is unambiguous. Any address showing
    more than one is returned separately: that means the partial-unique index is
    absent or disabled on this database — a structural failure, not drift.
    """
    seen: dict[str, list[dict[str, str]]] = {}
    for address, subscription_id, subscriber_id in assignments:
        seen.setdefault(address, []).append(
            {"subscription_id": subscription_id, "subscriber_id": subscriber_id}
        )
    holders = {
        address: holding[0] for address, holding in seen.items() if len(holding) == 1
    }
    violations = {
        address: holding for address, holding in seen.items() if len(holding) > 1
    }
    return holders, violations


def _known_ipam_addresses(db: Session) -> set[str]:
    return {
        norm
        for norm in (_norm(a) for a in db.scalars(select(IPv4Address.address)).all())
        if norm
    }


def _label(state: dict[str, dict[str, Any]], holder: dict[str, str]) -> str:
    """Human-readable identity for whoever holds an address."""
    subscription_id = holder.get("subscription_id") or ""
    if subscription_id:
        info = state.get(subscription_id) or {}
        return info.get("login") or subscription_id
    return f"legacy subscriber {holder.get('subscriber_id') or 'unknown'}"


def audit(db: Session, nas_filter: str | None) -> dict[str, Any]:
    needle = (nas_filter or "").strip().lower()
    cutoff = datetime.now(UTC) - ACTIVE_SESSION_FRESHNESS

    nas_by_id = {str(n.id): n for n in db.scalars(select(NasDevice)).all()}
    assignments = _active_ipv4_assignments(db)
    state, duplicate_logins = _subscription_state(db, assignments)
    holders, ledger_violations = _address_holders(assignments)
    ipam_known = _known_ipam_addresses(db)

    findings: dict[str, list[dict[str, Any]]] = {k: [] for k in _FINDING_KINDS}
    # Reported unscoped: a broken uniqueness guarantee is a database-wide fact,
    # not something a per-NAS run should hide.
    findings["ledger_integrity_violation"] = [
        {
            "address": address,
            "holders": [_label(state, holder) for holder in holding],
        }
        for address, holding in sorted(ledger_violations.items())
    ]

    # Pass 1 — classify every live session fleet-wide. Duplicate detection needs
    # the counterparty even when it sits on a different NAS, so the scope filter
    # is recorded here and applied only to per-session adjudication below.
    live: list[dict[str, Any]] = []
    stale = 0
    for session in db.scalars(select(RadiusActiveSession)).all():
        fresh_at = _aware(session.last_update) or _aware(session.session_start)
        if fresh_at is None or fresh_at < cutoff:
            stale += 1
            continue
        nas = (
            nas_by_id.get(str(session.nas_device_id)) if session.nas_device_id else None
        )
        live.append(
            {
                "login": session.username,
                "nas": (nas.name if nas else None)
                or session.nas_ip_address
                or "unknown",
                "observed_ip": _norm(session.framed_ip_address),
                "subscription_id": (
                    str(session.subscription_id) if session.subscription_id else ""
                ),
                "subscriber_id": (
                    str(session.subscriber_id) if session.subscriber_id else ""
                ),
                "session_start": (
                    session.session_start.isoformat() if session.session_start else None
                ),
                "in_scope": not needle
                or _matches_nas(
                    needle,
                    session.nas_ip_address,
                    nas.name if nas else None,
                    nas.code if nas else None,
                    nas.nas_ip if nas else None,
                ),
            }
        )

    scoped = [s for s in live if s["in_scope"]]
    scoped_subscription_ids = {
        s["subscription_id"] for s in scoped if s["subscription_id"]
    }

    # Pass 2 — per-session verdicts, scoped set only.
    unbound_logins: set[str] = set()
    for entry in scoped:
        observed = entry["observed_ip"]
        sub_id = entry["subscription_id"]
        base = {k: entry[k] for k in ("login", "nas", "observed_ip", "session_start")}

        # The stored subscription_id is the reconciler's lowest-id pick when a
        # login has several ACTIVE subscriptions. That is a guess, so no verdict
        # may rest on it.
        if entry["login"] in duplicate_logins:
            unbound_logins.add(entry["login"])
            continue

        info = state.get(sub_id)
        if info is None:
            findings["session_no_subscription"].append(base)
            continue

        row = {
            **base,
            "subscription_id": sub_id,
            "owner_ip": info["owner_ip"],
            "owner_ips": info["owner_ips"],
            "legacy_owner_ips": info["legacy_owner_ips"],
            "column_ip": info["column_ip"],
        }

        # Conflict is decided on the assignment ledger alone: whoever holds the
        # observed address holds it, regardless of what this subscription's own
        # desired state says (or fails to say). A legacy holder is compared at
        # subscriber grain, which is the only grain it has.
        holder = holders.get(observed) if observed else None
        if holder is not None and (
            holder["subscription_id"] != sub_id
            if holder["subscription_id"]
            else holder["subscriber_id"] != entry["subscriber_id"]
        ):
            findings["session_ip_conflict"].append(
                {
                    **row,
                    "assignment_holder": _label(state, holder),
                    "assignment_holder_subscription_id": holder["subscription_id"],
                    "assignment_holder_subscriber_id": holder["subscriber_id"],
                }
            )
        elif observed and info["owner_ip"] and observed != info["owner_ip"]:
            findings["session_ip_mismatch"].append(row)

        if observed and observed not in ipam_known:
            findings["session_ip_untracked"].append(row)

    for login in sorted(unbound_logins):
        findings["duplicate_login_subscription"].append(
            {
                "login": login,
                "active_subscription_ids": duplicate_logins[login],
                "note": "session binding is the reconciler's lowest-id pick; "
                "no per-session verdict issued",
            }
        )

    # Pass 3 — per-subscription ledger health, scoped set only, deduped.
    for sub_id in sorted(scoped_subscription_ids):
        info = state.get(sub_id)
        if info is None or info["login"] in duplicate_logins:
            continue
        row = {
            "login": info["login"],
            "subscription_id": sub_id,
            "owner_ip": info["owner_ip"],
            "owner_ips": info["owner_ips"],
            "legacy_owner_ips": info["legacy_owner_ips"],
            "column_ip": info["column_ip"],
        }
        if info["ambiguous"]:
            findings["ambiguous_service_assignment"].append(row)
        elif not info["owner_ip"] and info["legacy_owner_ips"]:
            findings["legacy_unbound_assignment"].append(row)
        elif not info["owner_ip"] and info["column_ip"]:
            findings["served_projection_unowned"].append(row)
        elif (
            info["owner_ip"]
            and info["column_ip"]
            and info["owner_ip"] != info["column_ip"]
        ):
            findings["served_projection_stale"].append(row)

    # Pass 4 — duplicates. Computed fleet-wide, reported when the group touches
    # the scoped set, with each member labelled so the off-NAS counterparty of
    # a scoped collision is visible rather than filtered away.
    sessions_by_ip: dict[str, list[dict[str, Any]]] = {}
    for entry in live:
        if entry["observed_ip"]:
            sessions_by_ip.setdefault(entry["observed_ip"], []).append(entry)
    for ip, entries in sorted(sessions_by_ip.items()):
        if len(entries) > 1 and any(e["in_scope"] for e in entries):
            findings["duplicate_session_ip"].append(
                {
                    "observed_ip": ip,
                    "sessions": sorted(
                        (
                            {
                                "login": e["login"],
                                "nas": e["nas"],
                                "in_scope": e["in_scope"],
                            }
                            for e in entries
                        ),
                        key=lambda e: e["login"] or "",
                    ),
                }
            )

    subs_by_served_ip: dict[str, list[dict[str, Any]]] = {}
    for info in state.values():
        if info["column_ip"]:
            subs_by_served_ip.setdefault(info["column_ip"], []).append(info)
    for ip, infos in sorted(subs_by_served_ip.items()):
        if len(infos) > 1 and any(
            i["subscription_id"] in scoped_subscription_ids for i in infos
        ):
            findings["duplicate_served_projection"].append(
                {
                    "served_ip": ip,
                    # Which claimant, if any, the ledger actually backs — the
                    # others are projections with no holder behind them.
                    "assignment_holder": (
                        _label(state, holders[ip]) if ip in holders else ""
                    ),
                    "subscriptions": sorted(
                        (
                            {
                                "login": i["login"],
                                "subscription_id": i["subscription_id"],
                                "in_scope": i["subscription_id"]
                                in scoped_subscription_ids,
                            }
                            for i in infos
                        ),
                        key=lambda i: i["login"] or i["subscription_id"],
                    ),
                }
            )

    return {
        "nas_filter": nas_filter or "(all)",
        "freshness_policy": (
            f"network.radius_sessions.ACTIVE_SESSION_FRESHNESS="
            f"{int(ACTIVE_SESSION_FRESHNESS.total_seconds())}s"
        ),
        "sessions_live_fleetwide": len(live),
        "sessions_stale_skipped": stale,
        "sessions_in_scope": len(scoped),
        "subscriptions_in_scope": len(scoped_subscription_ids),
        "subscriptions_projected": len(state),
        "counts": {k: len(v) for k, v in findings.items()},
        "findings": findings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--nas",
        help="Scope to one NAS (substring of its name, code, or IP), e.g. 'eagle'.",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help=f"Print every finding instead of capping each list at {SAMPLE_LIMIT}.",
    )
    args = parser.parse_args()

    session = SessionLocal()
    try:
        result = audit(session, args.nas)
    finally:
        session.close()

    if not args.full:
        result["findings"] = {
            kind: rows[:SAMPLE_LIMIT] for kind, rows in result["findings"].items()
        }
        result["sample_limit"] = SAMPLE_LIMIT

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if not any(result["counts"].values()) else 1


if __name__ == "__main__":
    sys.exit(main())
