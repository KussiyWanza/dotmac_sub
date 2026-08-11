#!/usr/bin/env python3
"""Phase 1 ticket drift checker — cutover gate G3 (10-phase1-tickets.md §4.2).

Read-only comparison of CRM ``tickets`` against sub ``support_tickets`` joined
on sub ``metadata->>'crm_ticket_id'``. The checker verifies exactly what
``import_crm_tickets_phase1.py`` writes (it imports the importer's own
subscriber map, title-regex default, and datetime normalization):

  * ``crm_missing_in_sub`` — CRM tickets with no sub marker row, excluding
    ``--exclude-title-regex`` probe skips;
  * ``sub_orphan_markers`` — sub marker rows whose ``crm_ticket_id`` no longer
    exists in CRM;
  * ``sub_duplicate_crm_markers`` — ambiguous duplicate markers in sub;
  * ``field_drift`` — per-ticket diffs on title, status (1:1 map with the
    terminal-precedence allowance, reported with a ``terminal_precedence``
    column), priority, ticket_type, region, number, subscriber link (via
    ``crm_subscriber_id`` + ``metadata.crm_alias_ids``),
    ``assistant_manager_person_id -> site_coordinator_person_id`` and the
    other role/team UUIDs, and due/resolved/closed timestamps;
  * ``children_count_mismatch`` — per-ticket comment (by
    ``metadata->>'crm_comment_id'``), assignee, link, and merge counts;
  * ``sub_enrichment`` — informational, non-gating: sub values that are
    deliberately richer than CRM's (subscriber linked in sub while CRM has
    none; sub titles CRM only holds truncated to its String(200) cap);
  * ``unresolved_subscribers`` / ``unmapped_staff`` — informational counts
    echoing the importer/preflight CSV shapes.

Comment-count shortfalls (sub one short) are usually CRM comments created
after the ticket's last incremental sync: CRM ``TicketComments.create`` does
not bump ``tickets.updated_at`` (noted in ``crm_ticket_pull.py`` too), so new
CRM comments are invisible to the importer's ``--state-file`` watermark and
to the incremental pull, whose comment sweep only covers open
``sync_source='crm'`` tickets. They stay gating — sub is genuinely missing
data — and heal with a full importer run (no ``--state-file`` /
``--updated-since``). CRM-side echo copies of sub-pushed comments cannot
cause the asymmetry (``crm_ticket_push.push_comment`` stamps
``crm_comment_id`` on the sub original exactly when the CRM copy is created,
so each pushed comment counts once on both sides) and carry no data marker to
exclude by (CRM ``ticket_comments`` has no metadata column).

Tickets whose CRM ``updated_at`` falls within ``--updated-within-minutes``
(default 30) are counted as ``expected_in_flight`` — the pull/push glue is
still live pre-flip — and do not gate.

Inputs:
  SUB_DATABASE_URL=postgresql://...
  CRM_DATABASE_URL=postgresql://...

Both sessions are forced into READ ONLY transactions and rolled back; the
checker never writes to either database.

Output: summary JSON on stdout plus one CSV per finding class in ``--out``.
Exit code 0 when there is zero drift outside the live window, 1 otherwise,
so a cron/CI job can gate the flip on it.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.migration.import_crm_tickets_phase1 import (  # noqa: E402
    DEFAULT_EXCLUDE_TITLE_REGEX,
    _crm_tickets,
    _engine_from_env,
    _format_datetime,
    _load_staff_map,
    _load_subscriber_map,
    _parse_datetime,
    _rows,
    _uuid_or_none,
)

DEFAULT_UPDATED_WITHIN_MINUTES = 30

# CRM ``resolved`` is a legacy transport value for native ``closed``. Unknown
# values pass through verbatim so preflight can gate them explicitly.
CRM_TO_SUB_STATUS = {
    "new": "new",
    "open": "open",
    "pending": "pending",
    "waiting_on_customer": "waiting_on_customer",
    "lastmile_rerun": "lastmile_rerun",
    "site_under_construction": "site_under_construction",
    "on_hold": "on_hold",
    "pending_confirmation": "pending_confirmation",
    "resolved": "closed",
    "closed": "closed",
    "canceled": "canceled",
    "merged": "merged",
}

# Statuses a sub row keeps even when CRM disagrees (mirrors
# transition_ticket_status source="crm_pull" local precedence, spec §3.3).
TERMINAL_STATUSES = {"closed", "canceled", "merged"}

# CRM ``tickets.title`` is String(200) (schema max_length=200); sub's is
# String(255). Sub titles pushed to CRM arrive truncated to this cap.
CRM_TITLE_MAX_LENGTH = 200

# Fields the importer copies verbatim (CRM column == sub column).
VERBATIM_TEXT_FIELDS = ("ticket_type", "region", "number")

# (CRM column, sub column) UUID pairs the importer carries verbatim; note the
# assistant_manager -> site_coordinator rename (spec §1.2).
VERBATIM_UUID_FIELDS = (
    ("created_by_person_id", "created_by_person_id"),
    ("assigned_to_person_id", "assigned_to_person_id"),
    ("ticket_manager_person_id", "ticket_manager_person_id"),
    ("assistant_manager_person_id", "site_coordinator_person_id"),
    ("service_team_id", "service_team_id"),
)

TIMESTAMP_FIELDS = ("due_at", "resolved_at", "closed_at")

CHILD_KINDS = ("comments", "assignees", "links", "merges")


@dataclass(frozen=True)
class FieldDiff:
    field: str
    crm_value: str | None
    sub_value: str | None
    terminal_precedence: bool = False


@dataclass(frozen=True)
class TicketComparison:
    diffs: tuple[FieldDiff, ...]
    unresolved_subscriber_reason: str | None = None
    # Sub-side values richer than CRM's — desired, never gating (sub linked a
    # subscriber CRM lacks; sub holds the full title CRM truncated).
    enrichments: tuple[FieldDiff, ...] = ()


def expected_sub_status(crm_status: str | None) -> str:
    """Sub status the importer writes for a CRM status (default ``open``)."""
    status = (crm_status or "").strip()
    if not status:
        return "open"
    return CRM_TO_SUB_STATUS.get(status, status)


def status_diff_terminal_precedence(
    *,
    crm_status: str | None,
    sub_status: str | None,
    crm_updated_at: datetime | None,
    sub_updated_at: datetime | None,
) -> bool:
    """True when a status diff is covered by the terminal-precedence allowance.

    A sub row already in closed/canceled/merged keeps its status when CRM
    disagrees. The allowance does NOT hold when CRM moved the ticket *later*
    than sub to a non-terminal status — that is drift the flip would lose.
    """
    if (sub_status or "") not in TERMINAL_STATUSES:
        return False
    if expected_sub_status(crm_status) in TERMINAL_STATUSES:
        return True
    if crm_updated_at and sub_updated_at and crm_updated_at > sub_updated_at:
        return False
    return True


def in_live_window(
    crm_updated_at: datetime | None,
    *,
    now: datetime,
    window_minutes: int,
) -> bool:
    """True when the CRM row changed recently enough to still be in flight."""
    if window_minutes <= 0 or crm_updated_at is None:
        return False
    return crm_updated_at >= now - timedelta(minutes=window_minutes)


def _norm_uuid(value: Any) -> str | None:
    text_value = _uuid_or_none(value)
    return text_value.lower() if text_value else None


def _norm_text(value: Any) -> str | None:
    return None if value is None else str(value)


def title_matches_crm_truncation(crm_title: str | None, sub_title: str | None) -> bool:
    """True when the titles differ only by CRM's String(200) cap (or strip).

    CRM holds pushed sub titles truncated to its ``max_length=200`` schema
    cap; sub's title is authoritative. A CRM title that equals sub's title
    after strip, or is exactly sub's title cut at 200 chars, is desired
    sub-side enrichment, not drift.
    """
    crm = crm_title or ""
    sub = sub_title or ""
    if crm.strip() == sub.strip():
        return True
    return len(sub) > CRM_TITLE_MAX_LENGTH and crm == sub[:CRM_TITLE_MAX_LENGTH]


def compare_ticket_fields(
    crm_ticket: dict[str, Any],
    sub_row: dict[str, Any],
    *,
    subscriber_map: dict[str, str],
) -> TicketComparison:
    """Diff one CRM ticket against its sub row, importer semantics exactly.

    ``sub_row`` keys follow the ``_sub_marker_rows`` select (sub column names
    plus ``unmapped_policy``); ``crm_ticket`` keys follow the importer's
    ``_crm_tickets`` select (CRM column names).
    """
    diffs: list[FieldDiff] = []
    enrichments: list[FieldDiff] = []

    crm_title = _norm_text(crm_ticket.get("title"))
    expected_title = crm_title or "Untitled CRM ticket"
    sub_title = _norm_text(sub_row.get("title"))
    if sub_title != expected_title:
        if title_matches_crm_truncation(crm_title, sub_title):
            enrichments.append(FieldDiff("title", crm_title, sub_title))
        else:
            diffs.append(FieldDiff("title", expected_title, sub_title))

    crm_status = _norm_text(crm_ticket.get("status"))
    sub_status = _norm_text(sub_row.get("status"))
    expected_status = expected_sub_status(crm_status)
    if sub_status != expected_status:
        diffs.append(
            FieldDiff(
                "status",
                expected_status,
                sub_status,
                terminal_precedence=status_diff_terminal_precedence(
                    crm_status=crm_status,
                    sub_status=sub_status,
                    crm_updated_at=_parse_datetime(crm_ticket.get("updated_at")),
                    sub_updated_at=_parse_datetime(sub_row.get("updated_at")),
                ),
            )
        )

    expected_priority = crm_ticket.get("priority") or "normal"
    sub_priority = _norm_text(sub_row.get("priority"))
    if sub_priority != expected_priority:
        diffs.append(FieldDiff("priority", expected_priority, sub_priority))

    for field_name in VERBATIM_TEXT_FIELDS:
        crm_value = _norm_text(crm_ticket.get(field_name))
        sub_value = _norm_text(sub_row.get(field_name))
        if crm_value != sub_value:
            diffs.append(FieldDiff(field_name, crm_value, sub_value))

    for crm_name, sub_name in VERBATIM_UUID_FIELDS:
        crm_value = _norm_uuid(crm_ticket.get(crm_name))
        sub_value = _norm_uuid(sub_row.get(sub_name))
        if crm_value != sub_value:
            diffs.append(FieldDiff(sub_name, crm_value, sub_value))

    for field_name in TIMESTAMP_FIELDS:
        crm_ts = _parse_datetime(crm_ticket.get(field_name))
        sub_ts = _parse_datetime(sub_row.get(field_name))
        if crm_ts != sub_ts:
            diffs.append(
                FieldDiff(
                    field_name, _format_datetime(crm_ts), _format_datetime(sub_ts)
                )
            )

    unresolved_reason: str | None = None
    crm_subscriber_id = _uuid_or_none(crm_ticket.get("subscriber_id"))
    sub_subscriber_id = _norm_uuid(sub_row.get("subscriber_id"))
    if crm_subscriber_id is None:
        if sub_subscriber_id is not None:
            # CRM has no subscriber but sub linked one — desired sub-side
            # enrichment (sub identity resolution is richer), never drift.
            enrichments.append(FieldDiff("subscriber_id", None, sub_subscriber_id))
    else:
        mapped = subscriber_map.get(crm_subscriber_id)
        if mapped:
            if _norm_uuid(mapped) != sub_subscriber_id:
                diffs.append(
                    FieldDiff("subscriber_id", _norm_uuid(mapped), sub_subscriber_id)
                )
        else:
            # Importer policy territory (override/skip/unlink); counted in the
            # unresolved_subscribers summary rather than flagged as drift.
            unresolved_reason = (
                _norm_text(sub_row.get("unmapped_policy")) or "unmapped_subscriber"
            )

    return TicketComparison(tuple(diffs), unresolved_reason, tuple(enrichments))


def compare_children_counts(
    crm_counts: dict[str, int], sub_counts: dict[str, int]
) -> list[tuple[str, int, int]]:
    """Return ``(child, crm_count, sub_count)`` for each mismatched kind."""
    mismatches: list[tuple[str, int, int]] = []
    for child in CHILD_KINDS:
        crm_count = int(crm_counts.get(child, 0))
        sub_count = int(sub_counts.get(child, 0))
        if crm_count != sub_count:
            mismatches.append((child, crm_count, sub_count))
    return mismatches


def _sub_marker_rows(sub: Connection) -> list[dict[str, Any]]:
    return _rows(
        sub,
        """
        SELECT id::text AS support_ticket_id,
               metadata->>'crm_ticket_id' AS crm_ticket_id,
               metadata->>'crm_unmapped_subscriber_policy' AS unmapped_policy,
               subscriber_id::text AS subscriber_id,
               created_by_person_id::text AS created_by_person_id,
               assigned_to_person_id::text AS assigned_to_person_id,
               ticket_manager_person_id::text AS ticket_manager_person_id,
               site_coordinator_person_id::text AS site_coordinator_person_id,
               service_team_id::text AS service_team_id,
               number,
               title,
               region,
               status,
               priority,
               ticket_type,
               due_at,
               resolved_at,
               closed_at,
               updated_at
        FROM support_tickets
        WHERE metadata->>'crm_ticket_id' IS NOT NULL
        """,
    )


def _count_map(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    return {str(row[key]): int(row["n"]) for row in rows}


def _sub_child_counts(sub: Connection) -> dict[str, dict[str, int]]:
    """Per-kind ``local_ticket_id -> count`` maps for sub children."""
    return {
        "comments": _count_map(
            _rows(
                sub,
                """
                SELECT ticket_id::text AS ticket_id, count(*) AS n
                FROM support_ticket_comments
                WHERE metadata->>'crm_comment_id' IS NOT NULL
                GROUP BY ticket_id
                """,
            ),
            "ticket_id",
        ),
        "assignees": _count_map(
            _rows(
                sub,
                """
                SELECT ticket_id::text AS ticket_id, count(*) AS n
                FROM support_ticket_assignees
                GROUP BY ticket_id
                """,
            ),
            "ticket_id",
        ),
        "links": _count_map(
            _rows(
                sub,
                """
                SELECT from_ticket_id::text AS ticket_id, count(*) AS n
                FROM support_ticket_links
                GROUP BY from_ticket_id
                """,
            ),
            "ticket_id",
        ),
        "merges": _count_map(
            _rows(
                sub,
                """
                SELECT source_ticket_id::text AS ticket_id, count(*) AS n
                FROM support_ticket_merges
                GROUP BY source_ticket_id
                """,
            ),
            "ticket_id",
        ),
    }


def _crm_child_counts(
    crm: Connection, importable_crm_ids: set[str]
) -> dict[str, dict[str, int]]:
    """Per-kind ``crm_ticket_id -> expected count`` maps.

    Links/merges mirror the importer: only pairs whose *both* endpoints map to
    sub rows are inserted, links dedupe on the unique (from, to, type) triple,
    assignees dedupe on (ticket_id, person_id).
    """
    comments = _count_map(
        _rows(
            crm,
            """
            SELECT ticket_id::text AS ticket_id, count(*) AS n
            FROM ticket_comments
            GROUP BY ticket_id
            """,
        ),
        "ticket_id",
    )
    assignees = _count_map(
        _rows(
            crm,
            """
            SELECT ticket_id::text AS ticket_id, count(DISTINCT person_id) AS n
            FROM ticket_assignees
            GROUP BY ticket_id
            """,
        ),
        "ticket_id",
    )

    link_rows = _rows(
        crm,
        """
        SELECT from_ticket_id::text AS from_ticket_id,
               to_ticket_id::text AS to_ticket_id,
               link_type
        FROM ticket_links
        """,
    )
    link_triples = {
        (row["from_ticket_id"], row["to_ticket_id"], row["link_type"])
        for row in link_rows
    }
    links: dict[str, int] = {}
    for from_id, to_id, _link_type in link_triples:
        if from_id in importable_crm_ids and to_id in importable_crm_ids:
            links[from_id] = links.get(from_id, 0) + 1

    merge_rows = _rows(
        crm,
        """
        SELECT source_ticket_id::text AS source_ticket_id,
               target_ticket_id::text AS target_ticket_id
        FROM ticket_merges
        """,
    )
    merges: dict[str, int] = {}
    for row in merge_rows:
        source_id = str(row["source_ticket_id"])
        target_id = str(row["target_ticket_id"])
        if source_id in importable_crm_ids and target_id in importable_crm_ids:
            merges[source_id] = merges.get(source_id, 0) + 1

    return {
        "comments": comments,
        "assignees": assignees,
        "links": links,
        "merges": merges,
    }


def _unmapped_staff_rows(
    sub: Connection, crm: Connection, staff_map: dict[str, str]
) -> list[dict[str, Any]]:
    """CRM staff role/assignee people with no staff-map or email match.

    Echoes preflight ``crm_unmapped_staff_people`` (same role-people union and
    system_users email join), minus explicit ``--staff-map`` hits.
    """
    staff_rows = _rows(
        crm,
        """
        WITH role_people AS (
            SELECT created_by_person_id AS person_id
            FROM tickets WHERE created_by_person_id IS NOT NULL
            UNION
            SELECT assigned_to_person_id FROM tickets
            WHERE assigned_to_person_id IS NOT NULL
            UNION
            SELECT ticket_manager_person_id FROM tickets
            WHERE ticket_manager_person_id IS NOT NULL
            UNION
            SELECT assistant_manager_person_id FROM tickets
            WHERE assistant_manager_person_id IS NOT NULL
            UNION
            SELECT person_id FROM ticket_assignees WHERE person_id IS NOT NULL
            UNION
            SELECT merged_by_person_id FROM ticket_merges
            WHERE merged_by_person_id IS NOT NULL
            UNION
            SELECT created_by_person_id FROM ticket_links
            WHERE created_by_person_id IS NOT NULL
        )
        SELECT rp.person_id::text AS crm_person_id,
               p.email,
               p.first_name,
               p.last_name
        FROM role_people rp
        LEFT JOIN people p ON p.id = rp.person_id
        ORDER BY p.email NULLS LAST, rp.person_id
        """,
    )
    staff_emails = [str(row["email"]).lower() for row in staff_rows if row.get("email")]
    mapped_emails: set[str] = set()
    if staff_emails:
        mapped_emails = {
            str(row["email"]).lower()
            for row in _rows(
                sub,
                """
                SELECT email
                FROM system_users
                WHERE lower(email) = ANY(:emails)
                """,
                {"emails": staff_emails},
            )
        }
    return [
        row
        for row in staff_rows
        if str(row["crm_person_id"]).lower() not in staff_map
        and (not row.get("email") or str(row["email"]).lower() not in mapped_emails)
    ]


def _write_csv(path: Path, rows: list[dict[str, Any]], limit: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows[:limit])


def run_drift_check(
    *,
    sub: Connection,
    crm: Connection,
    window_minutes: int,
    exclude_title_re: re.Pattern[str] | None,
    staff_map: dict[str, str],
    now: datetime | None = None,
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    """Run all comparisons; return (summary, per-class CSV rows)."""
    now = now or datetime.now(UTC)

    crm_tickets = _crm_tickets(crm, limit=None, updated_since=None)
    sub_rows = _sub_marker_rows(sub)
    subscriber_map = _load_subscriber_map(sub)

    sub_by_crm_id: dict[str, dict[str, Any]] = {}
    duplicate_markers: dict[str, list[str]] = {}
    for row in sub_rows:
        crm_ticket_id = str(row["crm_ticket_id"])
        if crm_ticket_id in sub_by_crm_id:
            duplicate_markers.setdefault(
                crm_ticket_id,
                [str(sub_by_crm_id[crm_ticket_id]["support_ticket_id"])],
            ).append(str(row["support_ticket_id"]))
            continue
        sub_by_crm_id[crm_ticket_id] = row

    crm_ids = {str(ticket["id"]) for ticket in crm_tickets}
    joined_crm_ids = {cid for cid in sub_by_crm_id if cid in crm_ids}

    sub_children = _sub_child_counts(sub)
    crm_children = _crm_child_counts(crm, joined_crm_ids)

    classes: dict[str, list[dict[str, Any]]] = {
        "crm_missing_in_sub": [],
        "sub_orphan_markers": [],
        "sub_duplicate_crm_markers": [],
        "field_drift": [],
        "children_count_mismatch": [],
        "sub_enrichment": [],
        "expected_in_flight": [],
        "probe_skipped": [],
        "unresolved_subscribers": [],
        "unmapped_staff": [],
    }
    in_flight_findings: dict[str, list[str]] = {}

    for crm_ticket in crm_tickets:
        crm_ticket_id = str(crm_ticket["id"])
        crm_updated_at = _parse_datetime(crm_ticket.get("updated_at"))
        in_window = in_live_window(
            crm_updated_at, now=now, window_minutes=window_minutes
        )
        sub_row = sub_by_crm_id.get(crm_ticket_id)

        if sub_row is None:
            title = str(crm_ticket.get("title") or "")
            if exclude_title_re and exclude_title_re.search(title):
                classes["probe_skipped"].append(
                    {
                        "crm_ticket_id": crm_ticket_id,
                        "number": crm_ticket.get("number"),
                        "title": title,
                        "status": crm_ticket.get("status"),
                    }
                )
                continue
            classes["crm_missing_in_sub"].append(
                {
                    "crm_ticket_id": crm_ticket_id,
                    "number": crm_ticket.get("number"),
                    "title": title,
                    "status": crm_ticket.get("status"),
                    "crm_subscriber_id": crm_ticket.get("subscriber_id"),
                    "crm_updated_at": _format_datetime(crm_updated_at),
                    "in_live_window": in_window,
                }
            )
            if in_window:
                in_flight_findings.setdefault(crm_ticket_id, []).append("missing")
            continue

        support_ticket_id = str(sub_row["support_ticket_id"])
        comparison = compare_ticket_fields(
            crm_ticket, sub_row, subscriber_map=subscriber_map
        )
        for diff in comparison.diffs:
            classes["field_drift"].append(
                {
                    "crm_ticket_id": crm_ticket_id,
                    "support_ticket_id": support_ticket_id,
                    "number": crm_ticket.get("number"),
                    "field": diff.field,
                    "crm_value": diff.crm_value,
                    "sub_value": diff.sub_value,
                    "terminal_precedence": diff.terminal_precedence,
                    "in_live_window": in_window,
                }
            )
            if in_window:
                in_flight_findings.setdefault(crm_ticket_id, []).append(
                    f"field:{diff.field}"
                )
        for enrichment in comparison.enrichments:
            classes["sub_enrichment"].append(
                {
                    "crm_ticket_id": crm_ticket_id,
                    "support_ticket_id": support_ticket_id,
                    "number": crm_ticket.get("number"),
                    "field": enrichment.field,
                    "crm_value": enrichment.crm_value,
                    "sub_value": enrichment.sub_value,
                }
            )
        if comparison.unresolved_subscriber_reason:
            classes["unresolved_subscribers"].append(
                {
                    "crm_ticket_id": crm_ticket_id,
                    "number": crm_ticket.get("number"),
                    "title": crm_ticket.get("title"),
                    "status": crm_ticket.get("status"),
                    "crm_subscriber_id": crm_ticket.get("subscriber_id"),
                    "reason": comparison.unresolved_subscriber_reason,
                }
            )

        crm_counts = {
            child: counts.get(crm_ticket_id, 0)
            for child, counts in crm_children.items()
        }
        sub_counts = {
            child: counts.get(support_ticket_id, 0)
            for child, counts in sub_children.items()
        }
        for child, crm_count, sub_count in compare_children_counts(
            crm_counts, sub_counts
        ):
            classes["children_count_mismatch"].append(
                {
                    "crm_ticket_id": crm_ticket_id,
                    "support_ticket_id": support_ticket_id,
                    "number": crm_ticket.get("number"),
                    "child": child,
                    "crm_count": crm_count,
                    "sub_count": sub_count,
                    "in_live_window": in_window,
                }
            )
            if in_window:
                in_flight_findings.setdefault(crm_ticket_id, []).append(
                    f"children:{child}"
                )

    for crm_ticket_id, row in sub_by_crm_id.items():
        if crm_ticket_id in crm_ids:
            continue
        classes["sub_orphan_markers"].append(
            {
                "support_ticket_id": row["support_ticket_id"],
                "crm_ticket_id": crm_ticket_id,
                "number": row.get("number"),
                "status": row.get("status"),
                "updated_at": _format_datetime(_parse_datetime(row.get("updated_at"))),
            }
        )

    for crm_ticket_id, support_ticket_ids in sorted(duplicate_markers.items()):
        classes["sub_duplicate_crm_markers"].append(
            {
                "crm_ticket_id": crm_ticket_id,
                "support_ticket_ids": ";".join(support_ticket_ids),
                "local_count": len(support_ticket_ids),
            }
        )

    for crm_ticket_id, findings in sorted(in_flight_findings.items()):
        classes["expected_in_flight"].append(
            {
                "crm_ticket_id": crm_ticket_id,
                "findings": "|".join(findings),
            }
        )

    classes["unmapped_staff"] = _unmapped_staff_rows(sub, crm, staff_map)

    drift_counts = {
        "crm_missing_in_sub": sum(
            1 for row in classes["crm_missing_in_sub"] if not row["in_live_window"]
        ),
        "sub_orphan_markers": len(classes["sub_orphan_markers"]),
        "sub_duplicate_crm_markers": len(classes["sub_duplicate_crm_markers"]),
        "field_drift": sum(
            1
            for row in classes["field_drift"]
            if not row["terminal_precedence"] and not row["in_live_window"]
        ),
        "children_count_mismatch": sum(
            1 for row in classes["children_count_mismatch"] if not row["in_live_window"]
        ),
    }
    summary = {
        "checked_at": _format_datetime(now),
        "updated_within_minutes": window_minutes,
        "totals": {
            "crm_tickets": len(crm_tickets),
            "sub_marker_rows": len(sub_rows),
            "joined": len(joined_crm_ids),
        },
        "classes": {
            name: {
                "rows": len(rows),
                "drift": drift_counts.get(name, 0),
            }
            for name, rows in classes.items()
        },
        "drift_total": sum(drift_counts.values()),
    }
    return summary, classes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="ticket-drift-report")
    parser.add_argument(
        "--updated-within-minutes",
        type=int,
        default=DEFAULT_UPDATED_WITHIN_MINUTES,
        help=(
            "CRM tickets updated within this window count as expected_in_flight "
            "(pull/push glue still live), not drift."
        ),
    )
    parser.add_argument(
        "--exclude-title-regex",
        default=DEFAULT_EXCLUDE_TITLE_REGEX,
        help="Regex for CRM probe tickets the importer skips (importer default).",
    )
    parser.add_argument(
        "--staff-map",
        help=(
            "staff_map.csv from build_crm_staff_map.py; role people in it are "
            "excluded from the unmapped_staff class."
        ),
    )
    parser.add_argument("--limit-csv", type=int, default=50000)
    args = parser.parse_args()

    exclude_title_re = (
        re.compile(args.exclude_title_regex) if args.exclude_title_regex else None
    )
    staff_map = _load_staff_map(args.staff_map)
    out_dir = Path(args.out)

    sub_engine = _engine_from_env("SUB_DATABASE_URL")
    crm_engine = _engine_from_env("CRM_DATABASE_URL")
    with sub_engine.connect() as sub, crm_engine.connect() as crm:
        sub.execute(text("SET TRANSACTION READ ONLY"))
        crm.execute(text("SET TRANSACTION READ ONLY"))
        try:
            summary, classes = run_drift_check(
                sub=sub,
                crm=crm,
                window_minutes=args.updated_within_minutes,
                exclude_title_re=exclude_title_re,
                staff_map=staff_map,
            )
        finally:
            sub.rollback()
            crm.rollback()

    for name, rows in classes.items():
        _write_csv(out_dir / f"{name}.csv", rows, max(1, args.limit_csv))

    exit_code = 0 if summary["drift_total"] == 0 else 1
    report = {
        **summary,
        "exclude_title_regex": args.exclude_title_regex,
        "staff_map": args.staff_map,
        "staff_map_entries": len(staff_map),
        "output_dir": str(out_dir),
        "exit_code": exit_code,
    }
    (out_dir / "summary.json").write_text(
        json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, indent=2, default=str))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
