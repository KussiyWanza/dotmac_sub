#!/usr/bin/env python3
"""One-time Phase 1 CRM ticket import into native sub support tables.

Inputs:
  SUB_DATABASE_URL=postgresql://...
  CRM_DATABASE_URL=postgresql://...

The importer is dry-run by default. Pass ``--apply`` to write to sub.

Unmapped subscriber policy is intentionally conservative:
  * explicit override CSV rows win;
  * test/probe tickets matching ``--exclude-title-regex`` are skipped;
  * closed/resolved/canceled/merged history can be imported unlinked when
    ``--allow-unmapped-closed`` is set;
  * all other unmapped subscriber rows block the run.

Optional extensions:
  * ``--staff-map staff_map.csv`` (from ``build_crm_staff_map.py``) derives
    comment authorship (spec 10-phase1-tickets.md section 1.4) and
    re-attributes pull-era comments that still carry
    ``metadata.crm_author_person_id`` with no ``author_system_user_id``;
  * ``--include-engine-data`` copies the one-time (non-watermarked) engine
    tables: ticket_assignment_rules/_counters, sla_policies/_targets and
    sla_clocks/_breaches (section 1.9), keyed on CRM UUIDs.
  * ``--sweep-comments`` sweeps CRM ``ticket_comments`` by ``created_at``
    against a second state-file watermark (``last_crm_comment_created_at``,
    minus ``--state-overlap-seconds``). CRM comment creation does not touch
    ``tickets.updated_at``, so the ticket watermark alone misses comments
    added to already-synced tickets; the sweep is one bulk query joined to
    the existing ``crm_ticket_id`` map and inserts only missing comments via
    the same dedupe/author-derivation path. ``--sweep-comments-since-hours``
    overrides the comment watermark with ``now - N hours`` (and implies
    ``--sweep-comments``).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine

CLOSED_STATUSES = {"closed", "resolved", "canceled", "merged"}
LEGACY_STATUS_ALIASES = {"resolved": "closed"}


def canonical_ticket_status(value: object) -> str:
    """Map legacy CRM status vocabulary to the native Ticket vocabulary."""

    normalized = str(value or "open").strip().lower() or "open"
    return LEGACY_STATUS_ALIASES.get(normalized, normalized)


DEFAULT_EXCLUDE_TITLE_REGEX = (
    r"(?i)\b(codex\b.*\bprobe\b|webhook\b.*\bprobe\b|test\b.*\bticket\b)"
)


@dataclass(frozen=True)
class UnmappedDecision:
    action: str
    reason: str
    subscriber_id: str | None = None


@dataclass
class ImportStats:
    fetched: int = 0
    created: int = 0
    updated: int = 0
    skipped: int = 0
    imported_unlinked: int = 0
    blocked_unmapped: int = 0
    comments_inserted: int = 0
    assignees_inserted: int = 0
    merges_inserted: int = 0
    links_inserted: int = 0
    access_tokens_upserted: int = 0
    service_teams_upserted: int = 0
    re_attributed: int = 0
    sla_events_inserted: int = 0
    assignment_rules_upserted: int = 0
    assignment_counters_upserted: int = 0
    sla_policies_upserted: int = 0
    sla_targets_upserted: int = 0
    sla_clocks_upserted: int = 0
    sla_breaches_upserted: int = 0
    comment_sweep_fetched: int = 0
    comment_sweep_inserted: int = 0
    comment_sweep_skipped_unmapped_ticket: int = 0
    max_crm_updated_at: str | None = None
    max_crm_comment_created_at: str | None = None
    blockers: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "fetched": self.fetched,
            "created": self.created,
            "updated": self.updated,
            "skipped": self.skipped,
            "imported_unlinked": self.imported_unlinked,
            "blocked_unmapped": self.blocked_unmapped,
            "comments_inserted": self.comments_inserted,
            "assignees_inserted": self.assignees_inserted,
            "merges_inserted": self.merges_inserted,
            "links_inserted": self.links_inserted,
            "access_tokens_upserted": self.access_tokens_upserted,
            "service_teams_upserted": self.service_teams_upserted,
            "re_attributed": self.re_attributed,
            "sla_events_inserted": self.sla_events_inserted,
            "assignment_rules_upserted": self.assignment_rules_upserted,
            "assignment_counters_upserted": self.assignment_counters_upserted,
            "sla_policies_upserted": self.sla_policies_upserted,
            "sla_targets_upserted": self.sla_targets_upserted,
            "sla_clocks_upserted": self.sla_clocks_upserted,
            "sla_breaches_upserted": self.sla_breaches_upserted,
            "comment_sweep_fetched": self.comment_sweep_fetched,
            "comment_sweep_inserted": self.comment_sweep_inserted,
            "comment_sweep_skipped_unmapped_ticket": (
                self.comment_sweep_skipped_unmapped_ticket
            ),
            "max_crm_updated_at": self.max_crm_updated_at,
            "max_crm_comment_created_at": self.max_crm_comment_created_at,
            "blockers": self.blockers,
        }


def _engine_from_env(name: str) -> Engine:
    url = os.environ.get(name)
    if not url:
        raise SystemExit(f"{name} is required")
    return create_engine(url, pool_pre_ping=True)


def _rows(
    conn: Connection, sql: str, params: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    result = conn.execute(text(sql), params or {})
    return [dict(row._mapping) for row in result]


def _json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return value


def _uuid_or_none(value: Any) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    if not value:
        return None
    return value


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text_value = str(value).strip()
        if not text_value:
            return None
        if text_value.endswith("Z"):
            text_value = text_value[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text_value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _format_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat()


TICKET_WATERMARK_KEY = "last_crm_ticket_updated_at"
COMMENT_WATERMARK_KEY = "last_crm_comment_created_at"


def _read_state(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    state_path = Path(path)
    if not state_path.exists():
        return {}
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def _state_watermark(
    path: str | None, key: str, overlap_seconds: int
) -> datetime | None:
    parsed = _parse_datetime(_read_state(path).get(key))
    if not parsed:
        return None
    overlap = max(overlap_seconds, 0)
    return parsed - timedelta(seconds=overlap)


def _state_since(path: str | None, overlap_seconds: int) -> datetime | None:
    return _state_watermark(path, TICKET_WATERMARK_KEY, overlap_seconds)


def resolve_sweep_since(
    *,
    state_file: str | None,
    overlap_seconds: int,
    since_hours: float | None,
    now: datetime | None = None,
) -> datetime | None:
    """Comment-sweep lower bound: explicit ``--sweep-comments-since-hours``
    overrides the state-file comment watermark (minus overlap); with neither,
    None means a full first-run sweep."""
    if since_hours is not None:
        return (now or datetime.now(UTC)) - timedelta(hours=since_hours)
    return _state_watermark(state_file, COMMENT_WATERMARK_KEY, overlap_seconds)


def _write_state(
    path: str | None,
    *,
    ticket_updated_at: str | None = None,
    comment_created_at: str | None = None,
) -> None:
    """Merge-write watermarks; a None value preserves the existing key."""
    if not path or not (ticket_updated_at or comment_created_at):
        return
    payload = _read_state(path)
    if ticket_updated_at:
        payload[TICKET_WATERMARK_KEY] = ticket_updated_at
    if comment_created_at:
        payload[COMMENT_WATERMARK_KEY] = comment_created_at
    payload["updated_at"] = datetime.now(UTC).isoformat()
    state_path = Path(path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )


def _load_overrides(path: str | None) -> dict[str, UnmappedDecision]:
    if not path:
        return {}
    overrides: dict[str, UnmappedDecision] = {}
    with Path(path).open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            ticket_id = str(row.get("crm_ticket_id") or row.get("ticket_id") or "")
            ticket_id = ticket_id.strip()
            if not ticket_id:
                continue
            action = str(row.get("action") or "").strip().lower()
            subscriber_id = str(row.get("subscriber_id") or "").strip() or None
            reason = str(row.get("reason") or "override_csv").strip()
            if subscriber_id and not action:
                action = "map"
            if action not in {"map", "skip", "unlink"}:
                raise SystemExit(f"Invalid override action for {ticket_id}: {action}")
            overrides[ticket_id] = UnmappedDecision(action, reason, subscriber_id)
    return overrides


def _load_staff_map(path: str | None) -> dict[str, str]:
    """Load ``crm_person_id -> system_user_id`` from build_crm_staff_map.py output."""
    if not path:
        return {}
    staff_map: dict[str, str] = {}
    with Path(path).open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            crm_person_id = str(row.get("crm_person_id") or "").strip().lower()
            system_user_id = str(row.get("system_user_id") or "").strip().lower()
            if not crm_person_id or not system_user_id:
                continue
            existing = staff_map.get(crm_person_id)
            if existing and existing != system_user_id:
                raise SystemExit(
                    f"Conflicting staff-map rows for crm_person_id {crm_person_id}"
                )
            staff_map[crm_person_id] = system_user_id
    return staff_map


def derive_comment_author(
    crm_author_person_id: str | None,
    *,
    staff_map: dict[str, str],
    person_subscriber_map: dict[str, str],
) -> tuple[str, str | None, str | None]:
    """Spec 10-phase1-tickets.md section 1.4 comment author derivation.

    Returns ``(author_type, author_person_id, author_system_user_id)`` where
    ``author_person_id`` is a sub subscriber UUID. Staff-map hits win over
    subscriber links (a staff person commenting is acting as staff); unmapped
    authors stay ``staff`` with a NULL system user and rely on the preserved
    ``metadata.crm_author_person_id`` for later re-attribution.
    """
    if not crm_author_person_id:
        return ("system", None, None)
    key = crm_author_person_id.strip().lower()
    system_user_id = staff_map.get(key)
    if system_user_id:
        return ("staff", None, system_user_id)
    subscriber_id = person_subscriber_map.get(key)
    if subscriber_id:
        return ("customer", subscriber_id, None)
    return ("staff", None, None)


def decide_unmapped_ticket(
    ticket: dict[str, Any],
    *,
    overrides: dict[str, UnmappedDecision],
    exclude_title_re: re.Pattern[str] | None,
    allow_unmapped_closed: bool,
) -> UnmappedDecision:
    ticket_id = str(ticket["id"])
    if ticket_id in overrides:
        return overrides[ticket_id]

    title = str(ticket.get("title") or "")
    if exclude_title_re and exclude_title_re.search(title):
        return UnmappedDecision("skip", "exclude_title_regex")

    status = str(ticket.get("status") or "")
    if allow_unmapped_closed and status in CLOSED_STATUSES:
        return UnmappedDecision("unlink", "unmapped_closed_history")

    return UnmappedDecision("block", "unmapped_subscriber")


def _load_subscriber_map(sub: Connection) -> dict[str, str]:
    rows = _rows(
        sub,
        """
        SELECT id::text AS subscriber_id,
               crm_subscriber_id::text AS crm_subscriber_id
        FROM subscribers
        WHERE crm_subscriber_id IS NOT NULL
        UNION ALL
        SELECT s.id::text AS subscriber_id,
               alias.crm_subscriber_id
        FROM subscribers s
        CROSS JOIN LATERAL json_array_elements_text(
            CASE
                WHEN json_typeof(s.metadata->'crm_alias_ids') = 'array'
                THEN s.metadata->'crm_alias_ids'
                ELSE '[]'::json
            END
        ) AS alias(crm_subscriber_id)
        """,
    )
    mapping: dict[str, str] = {}
    conflicts: list[dict[str, str]] = []
    for row in rows:
        crm_id = str(row["crm_subscriber_id"])
        sub_id = str(row["subscriber_id"])
        existing = mapping.get(crm_id)
        if existing and existing != sub_id:
            conflicts.append(
                {
                    "crm_subscriber_id": crm_id,
                    "subscriber_id": sub_id,
                    "existing_subscriber_id": existing,
                }
            )
        mapping[crm_id] = sub_id
    if conflicts:
        raise SystemExit(
            "CRM subscriber alias conflicts found:\n"
            + json.dumps(conflicts[:25], indent=2)
        )
    return mapping


def _load_person_subscriber_map(
    sub: Connection,
    crm: Connection,
    person_ids: list[str],
    subscriber_map: dict[str, str],
) -> dict[str, str]:
    """Resolve CRM person ids to sub subscriber ids (spec link keys 3/4).

    Chain 1: CRM ``subscribers.person_id`` -> ``crm_subscriber_id`` link map.
    Chain 2: CRM ``people.metadata->>'selfcare_id'`` validated against sub
    ``subscribers.id`` (mirrors preflight ``crm_unmapped_customer_people``).
    """
    if not person_ids:
        return {}
    rows = _rows(
        crm,
        """
        SELECT p.id::text AS person_id,
               p.metadata->>'selfcare_id' AS selfcare_id,
               s.id::text AS crm_subscriber_id
        FROM people p
        LEFT JOIN subscribers s ON s.person_id = p.id
        WHERE p.id::text = ANY(:person_ids)
        ORDER BY p.id, s.created_at NULLS LAST, s.id
        """,
        {"person_ids": person_ids},
    )
    selfcare_ids = sorted(
        {str(row["selfcare_id"]) for row in rows if row.get("selfcare_id")}
    )
    valid_selfcare_ids: set[str] = set()
    if selfcare_ids:
        valid_selfcare_ids = {
            str(row["id"])
            for row in _rows(
                sub,
                "SELECT id::text AS id FROM subscribers WHERE id::text = ANY(:ids)",
                {"ids": selfcare_ids},
            )
        }
    mapping: dict[str, str] = {}
    for row in rows:
        person_id = str(row["person_id"]).lower()
        if person_id in mapping:
            continue
        resolved = None
        crm_subscriber_id = row.get("crm_subscriber_id")
        if crm_subscriber_id:
            resolved = subscriber_map.get(str(crm_subscriber_id))
        if not resolved:
            selfcare_id = row.get("selfcare_id")
            if selfcare_id and str(selfcare_id) in valid_selfcare_ids:
                resolved = str(selfcare_id)
        if resolved:
            mapping[person_id] = resolved
    return mapping


def _load_existing_tickets(sub: Connection) -> dict[str, str]:
    return {
        str(row["crm_ticket_id"]): str(row["support_ticket_id"])
        for row in _rows(
            sub,
            """
            SELECT id::text AS support_ticket_id,
                   metadata->>'crm_ticket_id' AS crm_ticket_id
            FROM support_tickets
            WHERE metadata->>'crm_ticket_id' IS NOT NULL
            """,
        )
        if row.get("crm_ticket_id")
    }


def _crm_tickets(
    crm: Connection, *, limit: int | None, updated_since: datetime | None
) -> list[dict[str, Any]]:
    sql = """
        SELECT id::text,
               subscriber_id::text,
               created_by_person_id::text,
               assigned_to_person_id::text,
               ticket_manager_person_id::text,
               assistant_manager_person_id::text,
               service_team_id::text,
               title,
               description,
               status::text,
               priority::text,
               channel::text,
               tags::text,
               metadata::text,
               due_at,
               resolved_at,
               closed_at,
               is_active,
               created_at,
               updated_at,
               ticket_type,
               lead_id::text,
               customer_person_id::text,
               number,
               region,
               erpnext_id,
               merged_into_ticket_id::text
        FROM tickets
    """
    params: dict[str, Any] = {}
    if updated_since is not None:
        sql += "\nWHERE updated_at >= :updated_since"
        params["updated_since"] = updated_since
    sql += "\nORDER BY updated_at, id"
    if limit:
        sql += "\nLIMIT :limit"
        params["limit"] = limit
    return _rows(crm, sql, params)


def _ticket_payload(
    ticket: dict[str, Any],
    *,
    local_ticket_id: str,
    subscriber_id: str | None,
    policy_reason: str | None,
) -> dict[str, Any]:
    crm_metadata = _json(ticket.get("metadata"), {}) or {}
    attachments = []
    if isinstance(crm_metadata, dict):
        raw_attachments = crm_metadata.pop("attachments", [])
        attachments = raw_attachments if isinstance(raw_attachments, list) else []
    else:
        crm_metadata = {"crm_metadata_raw": crm_metadata}

    metadata = dict(crm_metadata)
    metadata.update(
        {
            "crm_ticket_id": ticket["id"],
            "crm_subscriber_id": ticket.get("subscriber_id"),
            "crm_customer_person_id": ticket.get("customer_person_id"),
            "crm_import_source": "dotmac_crm_phase1",
        }
    )
    if policy_reason:
        metadata["crm_unmapped_subscriber_policy"] = policy_reason

    tags = _json(ticket.get("tags"), []) or []
    if not isinstance(tags, list):
        tags = []

    return {
        "id": local_ticket_id,
        "subscriber_id": subscriber_id,
        "customer_account_id": subscriber_id,
        "customer_person_id": subscriber_id,
        "created_by_person_id": _uuid_or_none(ticket.get("created_by_person_id")),
        "assigned_to_person_id": _uuid_or_none(ticket.get("assigned_to_person_id")),
        "ticket_manager_person_id": _uuid_or_none(
            ticket.get("ticket_manager_person_id")
        ),
        "site_coordinator_person_id": _uuid_or_none(
            ticket.get("assistant_manager_person_id")
        ),
        "service_team_id": _uuid_or_none(ticket.get("service_team_id")),
        "number": ticket.get("number"),
        "title": ticket.get("title") or "Untitled CRM ticket",
        "description": ticket.get("description"),
        "region": ticket.get("region"),
        "status": canonical_ticket_status(ticket.get("status")),
        "priority": ticket.get("priority") or "normal",
        "ticket_type": ticket.get("ticket_type"),
        "erpnext_id": ticket.get("erpnext_id"),
        "channel": ticket.get("channel") or "web",
        "tags": json.dumps(tags),
        "metadata": json.dumps(metadata),
        "attachments": json.dumps(attachments),
        "due_at": ticket.get("due_at"),
        "resolved_at": ticket.get("resolved_at"),
        "closed_at": ticket.get("closed_at"),
        "merged_into_ticket_id": None,
        "is_active": ticket.get("is_active", True),
        "created_at": ticket.get("created_at"),
        "updated_at": ticket.get("updated_at"),
    }


UPSERT_TICKET_SQL = """
INSERT INTO support_tickets (
    id, subscriber_id, customer_account_id, customer_person_id,
    created_by_person_id, assigned_to_person_id, ticket_manager_person_id,
    site_coordinator_person_id, service_team_id, number, title, description,
    region, status, priority, ticket_type, erpnext_id, channel, tags, metadata,
    attachments, due_at, resolved_at, closed_at, merged_into_ticket_id, is_active,
    created_at, updated_at
) VALUES (
    CAST(:id AS uuid), CAST(:subscriber_id AS uuid), CAST(:customer_account_id AS uuid),
    CAST(:customer_person_id AS uuid), CAST(:created_by_person_id AS uuid),
    CAST(:assigned_to_person_id AS uuid), CAST(:ticket_manager_person_id AS uuid),
    CAST(:site_coordinator_person_id AS uuid), CAST(:service_team_id AS uuid),
    :number, :title, :description, :region, :status, :priority, :ticket_type,
    :erpnext_id, :channel, CAST(:tags AS json), CAST(:metadata AS json),
    CAST(:attachments AS json), :due_at, :resolved_at, :closed_at,
    CAST(:merged_into_ticket_id AS uuid), :is_active, :created_at, :updated_at
)
ON CONFLICT (id) DO UPDATE SET
    subscriber_id = EXCLUDED.subscriber_id,
    customer_account_id = EXCLUDED.customer_account_id,
    customer_person_id = EXCLUDED.customer_person_id,
    created_by_person_id = EXCLUDED.created_by_person_id,
    assigned_to_person_id = EXCLUDED.assigned_to_person_id,
    ticket_manager_person_id = EXCLUDED.ticket_manager_person_id,
    site_coordinator_person_id = EXCLUDED.site_coordinator_person_id,
    service_team_id = EXCLUDED.service_team_id,
    number = EXCLUDED.number,
    title = EXCLUDED.title,
    description = EXCLUDED.description,
    region = EXCLUDED.region,
    status = EXCLUDED.status,
    priority = EXCLUDED.priority,
    ticket_type = EXCLUDED.ticket_type,
    erpnext_id = EXCLUDED.erpnext_id,
    channel = EXCLUDED.channel,
    tags = EXCLUDED.tags,
    metadata = EXCLUDED.metadata,
    attachments = EXCLUDED.attachments,
    due_at = EXCLUDED.due_at,
    resolved_at = EXCLUDED.resolved_at,
    closed_at = EXCLUDED.closed_at,
    is_active = EXCLUDED.is_active,
    created_at = EXCLUDED.created_at,
    updated_at = EXCLUDED.updated_at
"""


def _upsert_service_teams(sub: Connection, crm: Connection) -> int:
    teams = _rows(
        crm,
        """
        SELECT id::text, name, team_type::text, region, manager_person_id::text,
               erp_department, is_active, metadata::text, created_at, updated_at
        FROM service_teams
        ORDER BY created_at, id
        """,
    )
    for team in teams:
        sub.execute(
            text(
                """
                INSERT INTO service_teams (
                    id, name, team_type, region, manager_person_id, erp_department,
                    is_active, metadata, created_at, updated_at
                ) VALUES (
                    CAST(:id AS uuid), :name, :team_type, :region,
                    CAST(:manager_person_id AS uuid), :erp_department, :is_active,
                    CAST(:metadata AS json), :created_at, :updated_at
                )
                ON CONFLICT (id) DO UPDATE SET
                    name = EXCLUDED.name,
                    team_type = EXCLUDED.team_type,
                    region = EXCLUDED.region,
                    manager_person_id = EXCLUDED.manager_person_id,
                    erp_department = EXCLUDED.erp_department,
                    is_active = EXCLUDED.is_active,
                    metadata = EXCLUDED.metadata,
                    updated_at = EXCLUDED.updated_at
                """
            ),
            {
                **team,
                "metadata": json.dumps(_json(team.get("metadata"), {}) or {}),
            },
        )
    return len(teams)


def _insert_comment_rows(
    sub: Connection,
    crm: Connection,
    comments: list[dict[str, Any]],
    crm_to_local_ticket: dict[str, str],
    *,
    staff_map: dict[str, str],
    subscriber_map: dict[str, str],
) -> int:
    """Insert CRM comment rows (shared by the ticket sync and the sweep)."""
    author_person_ids = sorted(
        {
            str(comment["author_person_id"]).lower()
            for comment in comments
            if comment.get("author_person_id")
        }
    )
    person_subscriber_map = _load_person_subscriber_map(
        sub, crm, author_person_ids, subscriber_map
    )
    inserted = 0
    for comment in comments:
        ticket_id = crm_to_local_ticket.get(str(comment["ticket_id"]))
        if not ticket_id:
            continue
        author_type, author_person_id, author_system_user_id = derive_comment_author(
            _uuid_or_none(comment.get("author_person_id")),
            staff_map=staff_map,
            person_subscriber_map=person_subscriber_map,
        )
        metadata = {
            "crm_comment_id": comment["id"],
            "crm_author_person_id": comment.get("author_person_id"),
            "crm_import_source": "dotmac_crm_phase1",
        }
        result = sub.execute(
            text(
                """
                INSERT INTO support_ticket_comments (
                    id, ticket_id, author_person_id, author_type,
                    author_system_user_id, body, is_internal, attachments, metadata,
                    created_at
                ) VALUES (
                    CAST(:id AS uuid), CAST(:ticket_id AS uuid),
                    CAST(:author_person_id AS uuid), :author_type,
                    CAST(:author_system_user_id AS uuid), :body, :is_internal,
                    CAST(:attachments AS json), CAST(:metadata AS json), :created_at
                )
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {
                "id": comment["id"],
                "ticket_id": ticket_id,
                "author_person_id": author_person_id,
                "author_type": author_type,
                "author_system_user_id": author_system_user_id,
                "body": comment.get("body") or "",
                "is_internal": bool(comment.get("is_internal")),
                "attachments": json.dumps(_json(comment.get("attachments"), []) or []),
                "metadata": json.dumps(metadata),
                "created_at": comment.get("created_at"),
            },
        )
        inserted += int(result.rowcount or 0)
    return inserted


def _sync_ticket_comments(
    sub: Connection,
    crm: Connection,
    crm_to_local_ticket: dict[str, str],
    *,
    staff_map: dict[str, str],
    subscriber_map: dict[str, str],
) -> int:
    sub.execute(
        text(
            """
            DELETE FROM support_ticket_comments
            WHERE ticket_id = ANY(CAST(:ticket_ids AS uuid[]))
              AND metadata->>'crm_comment_id' IS NOT NULL
            """
        ),
        {"ticket_ids": list(crm_to_local_ticket.values())},
    )
    comments = _rows(
        crm,
        """
        SELECT id::text, ticket_id::text, author_person_id::text, body, is_internal,
               attachments::text, created_at
        FROM ticket_comments
        WHERE ticket_id::text = ANY(:ticket_ids)
        ORDER BY created_at, id
        """,
        {"ticket_ids": list(crm_to_local_ticket)},
    )
    return _insert_comment_rows(
        sub,
        crm,
        comments,
        crm_to_local_ticket,
        staff_map=staff_map,
        subscriber_map=subscriber_map,
    )


def plan_comment_sweep(
    comments: list[dict[str, Any]],
    ticket_map: dict[str, str],
    existing_comment_ids: set[str],
) -> tuple[list[dict[str, Any]], int]:
    """Pure sweep planner: keep comments whose CRM ticket maps to a sub ticket
    and whose crm_comment_id is not already present.

    Returns ``(missing_comments, skipped_unmapped_ticket_count)``. Unmapped
    tickets are safe to skip past the watermark: their comments arrive with
    the full per-ticket sync when the ticket itself first imports.
    """
    mapped = [
        comment for comment in comments if str(comment["ticket_id"]) in ticket_map
    ]
    skipped_unmapped = len(comments) - len(mapped)
    missing = [
        comment for comment in mapped if str(comment["id"]) not in existing_comment_ids
    ]
    return missing, skipped_unmapped


def sweep_recent_comments(
    sub: Connection,
    crm: Connection,
    *,
    apply: bool,
    since: datetime | None,
    ticket_map: dict[str, str],
    staff_map: dict[str, str],
    subscriber_map: dict[str, str],
) -> tuple[int, int, int, datetime | None]:
    """Sweep CRM comments by ``created_at`` (comments never bump the parent
    ticket's ``updated_at``, so the ticket watermark misses them).

    One bulk CRM fetch, one sub dedupe query; no per-ticket round trips.
    Returns ``(fetched, inserted, skipped_unmapped_ticket, max_created_at)``;
    in dry-run ``inserted`` is the planned count.
    """
    sql = """
        SELECT id::text, ticket_id::text, author_person_id::text, body, is_internal,
               attachments::text, created_at
        FROM ticket_comments
    """
    params: dict[str, Any] = {}
    if since is not None:
        sql += "\nWHERE created_at >= :since"
        params["since"] = since
    sql += "\nORDER BY created_at, id"
    comments = _rows(crm, sql, params)
    max_created_at: datetime | None = None
    for comment in comments:
        created_at = _parse_datetime(comment.get("created_at"))
        if created_at and (max_created_at is None or created_at > max_created_at):
            max_created_at = created_at
    if not comments:
        return 0, 0, 0, None

    existing_comment_ids = {
        str(row["crm_comment_id"])
        for row in _rows(
            sub,
            """
            SELECT metadata->>'crm_comment_id' AS crm_comment_id
            FROM support_ticket_comments
            WHERE metadata->>'crm_comment_id' = ANY(:ids)
            """,
            {"ids": [str(comment["id"]) for comment in comments]},
        )
        if row.get("crm_comment_id")
    }
    missing, skipped_unmapped = plan_comment_sweep(
        comments, ticket_map, existing_comment_ids
    )
    if not apply:
        return len(comments), len(missing), skipped_unmapped, max_created_at

    inserted = _insert_comment_rows(
        sub,
        crm,
        missing,
        ticket_map,
        staff_map=staff_map,
        subscriber_map=subscriber_map,
    )
    return len(comments), inserted, skipped_unmapped, max_created_at


def _reattribute_pull_era_comments(sub: Connection, staff_map: dict[str, str]) -> int:
    """Re-attribute existing comments still carrying only the pull-era marker.

    Targets rows with ``metadata.crm_author_person_id`` set and no
    ``author_system_user_id`` (spec section 3.4 data-quality win); rows already
    resolved to a subscriber author are left alone.
    """
    if not staff_map:
        return 0
    result = sub.execute(
        text(
            """
            UPDATE support_ticket_comments c
            SET author_type = 'staff',
                author_system_user_id = m.system_user_id
            FROM (
                SELECT unnest(CAST(:crm_person_ids AS text[])) AS crm_person_id,
                       unnest(CAST(:system_user_ids AS uuid[])) AS system_user_id
            ) m
            WHERE lower(c.metadata->>'crm_author_person_id') = m.crm_person_id
              AND c.author_system_user_id IS NULL
              AND c.author_person_id IS NULL
            """
        ),
        {
            "crm_person_ids": list(staff_map),
            "system_user_ids": [staff_map[key] for key in staff_map],
        },
    )
    return int(result.rowcount or 0)


def _sync_sla_events(
    sub: Connection, crm: Connection, crm_to_local_ticket: dict[str, str]
) -> int:
    events = _rows(
        crm,
        """
        SELECT id::text, ticket_id::text, event_type, expected_at, actual_at,
               metadata::text, created_at
        FROM ticket_sla_events
        WHERE ticket_id::text = ANY(:ticket_ids)
        ORDER BY created_at, id
        """,
        {"ticket_ids": list(crm_to_local_ticket)},
    )
    inserted = 0
    for event in events:
        ticket_id = crm_to_local_ticket.get(str(event["ticket_id"]))
        if not ticket_id:
            continue
        metadata = _json(event.get("metadata"), {}) or {}
        if not isinstance(metadata, dict):
            metadata = {"crm_metadata_raw": metadata}
        metadata["sync_source"] = "crm_migration"
        result = sub.execute(
            text(
                """
                INSERT INTO support_ticket_sla_events (
                    id, ticket_id, event_type, expected_at, actual_at, metadata,
                    created_at
                ) VALUES (
                    CAST(:id AS uuid), CAST(:ticket_id AS uuid), :event_type,
                    :expected_at, :actual_at, CAST(:metadata AS json), :created_at
                )
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {
                "id": event["id"],
                "ticket_id": ticket_id,
                "event_type": event.get("event_type"),
                "expected_at": event.get("expected_at"),
                "actual_at": event.get("actual_at"),
                "metadata": json.dumps(metadata),
                "created_at": event.get("created_at"),
            },
        )
        inserted += int(result.rowcount or 0)
    return inserted


def _sync_ticket_assignees(
    sub: Connection, crm: Connection, crm_to_local_ticket: dict[str, str]
) -> int:
    sub.execute(
        text(
            """
            DELETE FROM support_ticket_assignees
            WHERE ticket_id = ANY(CAST(:ticket_ids AS uuid[]))
            """
        ),
        {"ticket_ids": list(crm_to_local_ticket.values())},
    )
    assignees = _rows(
        crm,
        """
        SELECT ticket_id::text, person_id::text, created_at
        FROM ticket_assignees
        WHERE ticket_id::text = ANY(:ticket_ids)
        ORDER BY created_at, ticket_id, person_id
        """,
        {"ticket_ids": list(crm_to_local_ticket)},
    )
    inserted = 0
    for assignee in assignees:
        ticket_id = crm_to_local_ticket.get(str(assignee["ticket_id"]))
        if not ticket_id:
            continue
        sub.execute(
            text(
                """
                INSERT INTO support_ticket_assignees (ticket_id, person_id, created_at)
                VALUES (
                    CAST(:ticket_id AS uuid), CAST(:person_id AS uuid), :created_at
                )
                ON CONFLICT (ticket_id, person_id) DO NOTHING
                """
            ),
            {
                "ticket_id": ticket_id,
                "person_id": assignee["person_id"],
                "created_at": assignee.get("created_at"),
            },
        )
        inserted += 1
    return inserted


def _sync_ticket_merges(
    sub: Connection, crm: Connection, crm_to_local_ticket: dict[str, str]
) -> int:
    sub.execute(
        text(
            """
            DELETE FROM support_ticket_merges
            WHERE source_ticket_id = ANY(CAST(:ticket_ids AS uuid[]))
               OR target_ticket_id = ANY(CAST(:ticket_ids AS uuid[]))
            """
        ),
        {"ticket_ids": list(crm_to_local_ticket.values())},
    )
    merges = _rows(
        crm,
        """
        SELECT source_ticket_id::text, target_ticket_id::text, reason,
               merged_by_person_id::text, created_at
        FROM ticket_merges
        ORDER BY created_at, source_ticket_id, target_ticket_id
        """,
    )
    inserted = 0
    for merge in merges:
        source_id = crm_to_local_ticket.get(str(merge["source_ticket_id"]))
        target_id = crm_to_local_ticket.get(str(merge["target_ticket_id"]))
        if not source_id or not target_id:
            continue
        sub.execute(
            text(
                """
                INSERT INTO support_ticket_merges (
                    source_ticket_id, target_ticket_id, reason,
                    merged_by_person_id, created_at
                ) VALUES (
                    CAST(:source_ticket_id AS uuid), CAST(:target_ticket_id AS uuid),
                    :reason, CAST(:merged_by_person_id AS uuid), :created_at
                )
                ON CONFLICT (source_ticket_id, target_ticket_id) DO UPDATE SET
                    reason = EXCLUDED.reason,
                    merged_by_person_id = EXCLUDED.merged_by_person_id,
                    created_at = EXCLUDED.created_at
                """
            ),
            {
                "source_ticket_id": source_id,
                "target_ticket_id": target_id,
                "reason": merge.get("reason"),
                "merged_by_person_id": merge.get("merged_by_person_id"),
                "created_at": merge.get("created_at"),
            },
        )
        inserted += 1
    return inserted


def _sync_ticket_links(
    sub: Connection, crm: Connection, crm_to_local_ticket: dict[str, str]
) -> int:
    sub.execute(
        text(
            """
            DELETE FROM support_ticket_links
            WHERE from_ticket_id = ANY(CAST(:ticket_ids AS uuid[]))
               OR to_ticket_id = ANY(CAST(:ticket_ids AS uuid[]))
            """
        ),
        {"ticket_ids": list(crm_to_local_ticket.values())},
    )
    links = _rows(
        crm,
        """
        SELECT id::text, from_ticket_id::text, to_ticket_id::text, link_type,
               created_by_person_id::text, created_at
        FROM ticket_links
        ORDER BY created_at, id
        """,
    )
    inserted = 0
    for link in links:
        from_id = crm_to_local_ticket.get(str(link["from_ticket_id"]))
        to_id = crm_to_local_ticket.get(str(link["to_ticket_id"]))
        if not from_id or not to_id:
            continue
        sub.execute(
            text(
                """
                INSERT INTO support_ticket_links (
                    id, from_ticket_id, to_ticket_id, link_type,
                    created_by_person_id, created_at
                ) VALUES (
                    CAST(:id AS uuid), CAST(:from_ticket_id AS uuid),
                    CAST(:to_ticket_id AS uuid), :link_type,
                    CAST(:created_by_person_id AS uuid), :created_at
                )
                ON CONFLICT (from_ticket_id, to_ticket_id, link_type) DO NOTHING
                """
            ),
            {
                "id": link["id"],
                "from_ticket_id": from_id,
                "to_ticket_id": to_id,
                "link_type": link["link_type"],
                "created_by_person_id": link.get("created_by_person_id"),
                "created_at": link.get("created_at"),
            },
        )
        inserted += 1
    return inserted


def _sync_access_tokens(
    sub: Connection, crm: Connection, crm_to_local_ticket: dict[str, str]
) -> int:
    tokens = _rows(
        crm,
        """
        SELECT id::text, ticket_id::text, token, purpose, expires_at, accessed_at,
               responded_at, is_active, created_at, updated_at
        FROM ticket_access_tokens
        ORDER BY created_at, id
        """,
    )
    upserted = 0
    for token in tokens:
        ticket_id = crm_to_local_ticket.get(str(token["ticket_id"]))
        if not ticket_id:
            continue
        sub.execute(
            text(
                """
                INSERT INTO ticket_access_tokens (
                    id, ticket_id, token, purpose, expires_at, accessed_at,
                    responded_at, is_active, created_at, updated_at
                ) VALUES (
                    CAST(:id AS uuid), CAST(:ticket_id AS uuid), :token, :purpose,
                    :expires_at, :accessed_at, :responded_at, :is_active,
                    :created_at, :updated_at
                )
                ON CONFLICT (token) DO UPDATE SET
                    ticket_id = EXCLUDED.ticket_id,
                    purpose = EXCLUDED.purpose,
                    expires_at = EXCLUDED.expires_at,
                    accessed_at = EXCLUDED.accessed_at,
                    responded_at = EXCLUDED.responded_at,
                    is_active = EXCLUDED.is_active,
                    updated_at = EXCLUDED.updated_at
                """
            ),
            {**token, "ticket_id": ticket_id},
        )
        upserted += 1
    return upserted


def _sync_assignment_rules(sub: Connection, crm: Connection) -> int:
    rules = _rows(
        crm,
        """
        SELECT id::text, name, priority, is_active, match_config::text,
               strategy::text, team_id::text, assign_manager, assign_spc,
               created_at, updated_at
        FROM ticket_assignment_rules
        ORDER BY created_at, id
        """,
    )
    for rule in rules:
        match_config = _json(rule.get("match_config"), None)
        sub.execute(
            text(
                """
                INSERT INTO ticket_assignment_rules (
                    id, name, priority, is_active, match_config, strategy,
                    team_id, assign_manager, assign_spc, created_at, updated_at
                ) VALUES (
                    CAST(:id AS uuid), :name, :priority, :is_active,
                    CAST(:match_config AS json), :strategy, CAST(:team_id AS uuid),
                    :assign_manager, :assign_spc, :created_at, :updated_at
                )
                ON CONFLICT (id) DO UPDATE SET
                    name = EXCLUDED.name,
                    priority = EXCLUDED.priority,
                    is_active = EXCLUDED.is_active,
                    match_config = EXCLUDED.match_config,
                    strategy = EXCLUDED.strategy,
                    team_id = EXCLUDED.team_id,
                    assign_manager = EXCLUDED.assign_manager,
                    assign_spc = EXCLUDED.assign_spc,
                    updated_at = EXCLUDED.updated_at
                """
            ),
            {
                **rule,
                "match_config": (
                    json.dumps(match_config) if match_config is not None else None
                ),
            },
        )
    return len(rules)


def _sync_assignment_counters(
    sub: Connection, crm: Connection, staff_map: dict[str, str]
) -> int:
    counters = _rows(
        crm,
        """
        SELECT id::text, rule_id::text, last_assigned_person_id::text, updated_at
        FROM ticket_assignment_counters
        ORDER BY rule_id, id
        """,
    )
    for counter in counters:
        last_assigned = _uuid_or_none(counter.get("last_assigned_person_id"))
        if last_assigned:
            last_assigned = staff_map.get(last_assigned.lower(), last_assigned)
        sub.execute(
            text(
                """
                INSERT INTO ticket_assignment_counters (
                    id, rule_id, last_assigned_person_id, updated_at
                ) VALUES (
                    CAST(:id AS uuid), CAST(:rule_id AS uuid),
                    CAST(:last_assigned_person_id AS uuid), :updated_at
                )
                ON CONFLICT (rule_id) DO UPDATE SET
                    last_assigned_person_id = EXCLUDED.last_assigned_person_id,
                    updated_at = EXCLUDED.updated_at
                """
            ),
            {**counter, "last_assigned_person_id": last_assigned},
        )
    return len(counters)


def _sync_sla_policies(sub: Connection, crm: Connection) -> int:
    policies = _rows(
        crm,
        """
        SELECT id::text, name, entity_type::text, description, is_active,
               created_at, updated_at
        FROM sla_policies
        ORDER BY created_at, id
        """,
    )
    for policy in policies:
        sub.execute(
            text(
                """
                INSERT INTO sla_policies (
                    id, name, entity_type, description, is_active,
                    created_at, updated_at
                ) VALUES (
                    CAST(:id AS uuid), :name, :entity_type, :description,
                    :is_active, :created_at, :updated_at
                )
                ON CONFLICT (id) DO UPDATE SET
                    name = EXCLUDED.name,
                    entity_type = EXCLUDED.entity_type,
                    description = EXCLUDED.description,
                    is_active = EXCLUDED.is_active,
                    updated_at = EXCLUDED.updated_at
                """
            ),
            policy,
        )
    return len(policies)


def _sync_sla_targets(sub: Connection, crm: Connection) -> int:
    targets = _rows(
        crm,
        """
        SELECT id::text, policy_id::text, priority, target_minutes,
               warning_minutes, is_active, created_at, updated_at
        FROM sla_targets
        ORDER BY created_at, id
        """,
    )
    for target in targets:
        sub.execute(
            text(
                """
                INSERT INTO sla_targets (
                    id, policy_id, priority, target_minutes, warning_minutes,
                    is_active, created_at, updated_at
                ) VALUES (
                    CAST(:id AS uuid), CAST(:policy_id AS uuid), :priority,
                    :target_minutes, :warning_minutes, :is_active,
                    :created_at, :updated_at
                )
                ON CONFLICT (id) DO UPDATE SET
                    policy_id = EXCLUDED.policy_id,
                    priority = EXCLUDED.priority,
                    target_minutes = EXCLUDED.target_minutes,
                    warning_minutes = EXCLUDED.warning_minutes,
                    is_active = EXCLUDED.is_active,
                    updated_at = EXCLUDED.updated_at
                """
            ),
            target,
        )
    return len(targets)


def _sync_sla_clocks(
    sub: Connection, crm: Connection, crm_to_local_ticket: dict[str, str]
) -> tuple[int, set[str]]:
    """Copy all SLA clocks; ticket clocks are re-keyed to merged sub ticket ids.

    Ticket clocks whose CRM ticket has no sub counterpart are skipped (their
    breaches too); non-ticket entity ids are carried verbatim (plain UUIDs,
    later phases attach those entities).
    """
    clocks = _rows(
        crm,
        """
        SELECT id::text, policy_id::text, entity_type::text, entity_id::text,
               priority, status::text, started_at, paused_at,
               total_paused_seconds, due_at, completed_at, breached_at,
               created_at, updated_at
        FROM sla_clocks
        ORDER BY created_at, id
        """,
    )
    upserted = 0
    clock_ids: set[str] = set()
    for clock in clocks:
        entity_id = str(clock["entity_id"])
        if str(clock.get("entity_type")) == "ticket":
            local_ticket_id = crm_to_local_ticket.get(entity_id)
            if not local_ticket_id:
                continue
            entity_id = local_ticket_id
        sub.execute(
            text(
                """
                INSERT INTO sla_clocks (
                    id, policy_id, entity_type, entity_id, priority, status,
                    started_at, paused_at, total_paused_seconds, due_at,
                    completed_at, breached_at, created_at, updated_at
                ) VALUES (
                    CAST(:id AS uuid), CAST(:policy_id AS uuid), :entity_type,
                    CAST(:entity_id AS uuid), :priority, :status, :started_at,
                    :paused_at, :total_paused_seconds, :due_at, :completed_at,
                    :breached_at, :created_at, :updated_at
                )
                ON CONFLICT (id) DO UPDATE SET
                    policy_id = EXCLUDED.policy_id,
                    entity_type = EXCLUDED.entity_type,
                    entity_id = EXCLUDED.entity_id,
                    priority = EXCLUDED.priority,
                    status = EXCLUDED.status,
                    started_at = EXCLUDED.started_at,
                    paused_at = EXCLUDED.paused_at,
                    total_paused_seconds = EXCLUDED.total_paused_seconds,
                    due_at = EXCLUDED.due_at,
                    completed_at = EXCLUDED.completed_at,
                    breached_at = EXCLUDED.breached_at,
                    updated_at = EXCLUDED.updated_at
                """
            ),
            {**clock, "entity_id": entity_id},
        )
        upserted += 1
        clock_ids.add(str(clock["id"]))
    return upserted, clock_ids


def _sync_sla_breaches(sub: Connection, crm: Connection, clock_ids: set[str]) -> int:
    if not clock_ids:
        return 0
    breaches = _rows(
        crm,
        """
        SELECT id::text, clock_id::text, status::text, breached_at, notes,
               created_at, updated_at
        FROM sla_breaches
        WHERE clock_id::text = ANY(:clock_ids)
        ORDER BY created_at, id
        """,
        {"clock_ids": sorted(clock_ids)},
    )
    for breach in breaches:
        sub.execute(
            text(
                """
                INSERT INTO sla_breaches (
                    id, clock_id, status, breached_at, notes,
                    created_at, updated_at
                ) VALUES (
                    CAST(:id AS uuid), CAST(:clock_id AS uuid), :status,
                    :breached_at, :notes, :created_at, :updated_at
                )
                ON CONFLICT (id) DO UPDATE SET
                    clock_id = EXCLUDED.clock_id,
                    status = EXCLUDED.status,
                    breached_at = EXCLUDED.breached_at,
                    notes = EXCLUDED.notes,
                    updated_at = EXCLUDED.updated_at
                """
            ),
            breach,
        )
    return len(breaches)


def import_tickets(
    *,
    sub: Connection,
    crm: Connection,
    apply: bool,
    limit: int | None,
    updated_since: datetime | None,
    overrides: dict[str, UnmappedDecision],
    exclude_title_re: re.Pattern[str] | None,
    allow_unmapped_closed: bool,
    sync_comments: bool,
    staff_map: dict[str, str] | None = None,
    include_engine_data: bool = False,
    sweep_comments: bool = False,
    sweep_since: datetime | None = None,
) -> ImportStats:
    staff_map = staff_map or {}
    stats = ImportStats()
    subscriber_map = _load_subscriber_map(sub)
    existing_by_crm_ticket = _load_existing_tickets(sub)
    tickets = _crm_tickets(crm, limit=limit, updated_since=updated_since)
    stats.fetched = len(tickets)
    max_updated_at: datetime | None = None

    crm_to_local_ticket: dict[str, str] = {}
    planned_rows: list[tuple[dict[str, Any], dict[str, Any], bool]] = []

    for ticket in tickets:
        ticket_updated_at = _parse_datetime(ticket.get("updated_at"))
        if ticket_updated_at and (
            max_updated_at is None or ticket_updated_at > max_updated_at
        ):
            max_updated_at = ticket_updated_at
        crm_ticket_id = str(ticket["id"])
        local_ticket_id = existing_by_crm_ticket.get(crm_ticket_id) or str(uuid.uuid4())
        subscriber_id = None
        policy_reason = None

        crm_subscriber_id = _uuid_or_none(ticket.get("subscriber_id"))
        if crm_subscriber_id:
            subscriber_id = subscriber_map.get(crm_subscriber_id)
            if not subscriber_id:
                decision = decide_unmapped_ticket(
                    ticket,
                    overrides=overrides,
                    exclude_title_re=exclude_title_re,
                    allow_unmapped_closed=allow_unmapped_closed,
                )
                if decision.action == "map":
                    subscriber_id = decision.subscriber_id
                    policy_reason = decision.reason
                elif decision.action == "skip":
                    stats.skipped += 1
                    continue
                elif decision.action == "unlink":
                    stats.imported_unlinked += 1
                    policy_reason = decision.reason
                else:
                    stats.blocked_unmapped += 1
                    stats.blockers.append(
                        {
                            "crm_ticket_id": crm_ticket_id,
                            "number": ticket.get("number"),
                            "title": ticket.get("title"),
                            "status": ticket.get("status"),
                            "crm_subscriber_id": crm_subscriber_id,
                            "reason": decision.reason,
                        }
                    )
                    continue

        payload = _ticket_payload(
            ticket,
            local_ticket_id=local_ticket_id,
            subscriber_id=subscriber_id,
            policy_reason=policy_reason,
        )
        planned_rows.append((ticket, payload, crm_ticket_id in existing_by_crm_ticket))
        crm_to_local_ticket[crm_ticket_id] = local_ticket_id

    stats.max_crm_updated_at = _format_datetime(max_updated_at)

    if stats.blockers:
        return stats

    full_ticket_map = {**existing_by_crm_ticket, **crm_to_local_ticket}

    def _run_comment_sweep() -> None:
        # Additive to the ticket watermark: comments never bump the parent
        # ticket's updated_at, so already-synced tickets need this sweep.
        # In dry-run the planned count can include comments the per-ticket
        # sync of this same run would insert anyway.
        (
            stats.comment_sweep_fetched,
            stats.comment_sweep_inserted,
            stats.comment_sweep_skipped_unmapped_ticket,
            max_comment_created_at,
        ) = sweep_recent_comments(
            sub,
            crm,
            apply=apply,
            since=sweep_since,
            ticket_map=full_ticket_map,
            staff_map=staff_map,
            subscriber_map=subscriber_map,
        )
        stats.max_crm_comment_created_at = _format_datetime(max_comment_created_at)

    if not apply:
        for _ticket, _payload, existed in planned_rows:
            if existed:
                stats.updated += 1
            else:
                stats.created += 1
        if sweep_comments:
            _run_comment_sweep()
        return stats

    stats.service_teams_upserted = _upsert_service_teams(sub, crm)
    for _ticket, payload, existed in planned_rows:
        sub.execute(text(UPSERT_TICKET_SQL), payload)
        if existed:
            stats.updated += 1
        else:
            stats.created += 1

    if sync_comments and crm_to_local_ticket:
        stats.comments_inserted = _sync_ticket_comments(
            sub,
            crm,
            crm_to_local_ticket,
            staff_map=staff_map,
            subscriber_map=subscriber_map,
        )
        stats.sla_events_inserted = _sync_sla_events(sub, crm, crm_to_local_ticket)
        stats.assignees_inserted = _sync_ticket_assignees(sub, crm, crm_to_local_ticket)
        stats.merges_inserted = _sync_ticket_merges(sub, crm, crm_to_local_ticket)
        stats.links_inserted = _sync_ticket_links(sub, crm, crm_to_local_ticket)
        stats.access_tokens_upserted = _sync_access_tokens(
            sub, crm, crm_to_local_ticket
        )

    if sweep_comments:
        # After the per-ticket sync so its inserts are seen by the dedupe.
        _run_comment_sweep()

    if staff_map:
        stats.re_attributed = _reattribute_pull_era_comments(sub, staff_map)

    if include_engine_data:
        # One-time engine copy (spec section 1.9): not watermark-scoped, so
        # ticket clocks re-key through every known crm_ticket_id, not just
        # this run's fetch window.
        stats.assignment_rules_upserted = _sync_assignment_rules(sub, crm)
        stats.assignment_counters_upserted = _sync_assignment_counters(
            sub, crm, staff_map
        )
        stats.sla_policies_upserted = _sync_sla_policies(sub, crm)
        stats.sla_targets_upserted = _sync_sla_targets(sub, crm)
        stats.sla_clocks_upserted, clock_ids = _sync_sla_clocks(
            sub, crm, full_ticket_map
        )
        stats.sla_breaches_upserted = _sync_sla_breaches(sub, crm, clock_ids)

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--updated-since",
        help=(
            "Only import CRM tickets with tickets.updated_at at or after this "
            "ISO timestamp. Overrides --state-file when both are provided."
        ),
    )
    parser.add_argument(
        "--state-file",
        help=(
            "JSON state file storing last_crm_ticket_updated_at for scheduled "
            "incremental runs."
        ),
    )
    parser.add_argument(
        "--state-overlap-seconds",
        type=int,
        default=600,
        help="Subtract this overlap from the state-file watermark.",
    )
    parser.add_argument("--overrides-csv")
    parser.add_argument(
        "--staff-map",
        help=(
            "staff_map.csv from build_crm_staff_map.py (crm_person_id -> "
            "system_user_id); derives comment authorship and re-attributes "
            "pull-era comments."
        ),
    )
    parser.add_argument(
        "--include-engine-data",
        action="store_true",
        help=(
            "One-time copy of ticket_assignment_rules/_counters, "
            "sla_policies/_targets and sla_clocks/_breaches (not watermarked)."
        ),
    )
    parser.add_argument(
        "--sweep-comments",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "Sweep CRM ticket_comments by created_at against the "
            "last_crm_comment_created_at state watermark (comments do not "
            "bump tickets.updated_at, so the ticket watermark misses them)."
        ),
    )
    parser.add_argument(
        "--sweep-comments-since-hours",
        type=float,
        help=(
            "Sweep comments created in the last N hours instead of the state "
            "watermark. Implies --sweep-comments."
        ),
    )
    parser.add_argument(
        "--exclude-title-regex",
        default=DEFAULT_EXCLUDE_TITLE_REGEX,
        help="Regex for CRM tickets to skip before unmapped-subscriber blocking.",
    )
    parser.add_argument(
        "--allow-unmapped-closed",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Import closed/resolved/canceled/merged unmapped tickets as unlinked.",
    )
    parser.add_argument(
        "--sync-comments",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Import comments, assignees, merges, links, and access tokens.",
    )
    args = parser.parse_args()

    overrides = _load_overrides(args.overrides_csv)
    staff_map = _load_staff_map(args.staff_map)
    exclude_title_re = (
        re.compile(args.exclude_title_regex) if args.exclude_title_regex else None
    )
    updated_since = (
        _parse_datetime(args.updated_since)
        if args.updated_since
        else _state_since(args.state_file, args.state_overlap_seconds)
    )
    sweep_comments = bool(
        args.sweep_comments or args.sweep_comments_since_hours is not None
    )
    sweep_since = (
        resolve_sweep_since(
            state_file=args.state_file,
            overlap_seconds=args.state_overlap_seconds,
            since_hours=args.sweep_comments_since_hours,
        )
        if sweep_comments
        else None
    )

    sub_engine = _engine_from_env("SUB_DATABASE_URL")
    crm_engine = _engine_from_env("CRM_DATABASE_URL")
    with sub_engine.connect() as sub, crm_engine.connect() as crm:
        sub_trans = sub.begin()
        crm.execute(text("SET TRANSACTION READ ONLY"))
        if not args.apply:
            sub.execute(text("SET TRANSACTION READ ONLY"))
        try:
            stats = import_tickets(
                sub=sub,
                crm=crm,
                apply=args.apply,
                limit=args.limit,
                updated_since=updated_since,
                overrides=overrides,
                exclude_title_re=exclude_title_re,
                allow_unmapped_closed=args.allow_unmapped_closed,
                sync_comments=args.sync_comments,
                staff_map=staff_map,
                include_engine_data=args.include_engine_data,
                sweep_comments=sweep_comments,
                sweep_since=sweep_since,
            )
        except Exception:
            sub_trans.rollback()
            crm.rollback()
            raise
        if args.apply and not stats.blockers:
            sub_trans.commit()
            _write_state(
                args.state_file,
                ticket_updated_at=stats.max_crm_updated_at,
                comment_created_at=stats.max_crm_comment_created_at,
            )
        else:
            sub_trans.rollback()
        crm.rollback()

    report = {
        "apply": args.apply,
        "updated_since": _format_datetime(updated_since),
        "state_file": args.state_file,
        "state_overlap_seconds": args.state_overlap_seconds,
        "allow_unmapped_closed": args.allow_unmapped_closed,
        "exclude_title_regex": args.exclude_title_regex,
        "staff_map": args.staff_map,
        "staff_map_entries": len(staff_map),
        "include_engine_data": args.include_engine_data,
        "sweep_comments": sweep_comments,
        "sweep_comments_since_hours": args.sweep_comments_since_hours,
        "sweep_since": _format_datetime(sweep_since),
        "stats": stats.as_dict(),
    }
    print(json.dumps(report, indent=2, default=str))
    if stats.blockers:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
