"""Pull support tickets from DotMac Omni CRM into local support tickets."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.subscriber import Subscriber
from app.models.support import Ticket, TicketChannel, TicketComment, TicketStatus
from app.services.crm_client import CRMClientError
from app.services.integrations.connectors.dotmac_crm import (
    CrmTicketObservationSource,
)
from app.services.integrations.crm_capability import capability_client
from app.services.support import _coerce_uuid

logger = logging.getLogger(__name__)
_CUSTOMER_ID_PAIR_RE = re.compile(r"\((\d{6,12})\s*-\s*([\d\s\u200b]+)\)")
CrmTicketRecordCallback = Callable[
    [dict[str, Any], str, int, str | None, UUID | None], None
]


@dataclass
class CrmTicketPullResult:
    fetched: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped_leads: int = 0
    skipped_unmapped_subscribers: int = 0
    comments_created: int = 0
    errors: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "fetched": self.fetched,
            "created": self.created,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "skipped_leads": self.skipped_leads,
            "skipped_unmapped_subscribers": self.skipped_unmapped_subscribers,
            "comments_created": self.comments_created,
            "errors": self.errors,
        }


def _parse_datetime(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _enum_value(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "value"):
        value = value.value
    text = str(value).strip()
    return text or None


def _safe_channel(value: Any) -> TicketChannel:
    text = _enum_value(value) or TicketChannel.api.value
    try:
        return TicketChannel(text)
    except ValueError:
        return TicketChannel.api


def _clean_tags(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    tags: list[str] = []
    for item in value:
        text = str(item).strip()
        if text:
            tags.append(text)
    return tags


def _clean_attachments(value: Any) -> list[dict]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _metadata(ticket: dict[str, Any]) -> dict[str, Any]:
    source_metadata = ticket.get("metadata") or ticket.get("metadata_") or {}
    if not isinstance(source_metadata, dict):
        source_metadata = {}
    merged = dict(source_metadata)
    merged.update(
        {
            "sync_source": "crm",
            "crm_ticket_id": str(ticket.get("id") or ""),
            "crm_ticket_number": str(ticket.get("number") or ""),
            "crm_updated_at": ticket.get("updated_at"),
            "crm_created_at": ticket.get("created_at"),
            "crm_lead_id": str(ticket.get("lead_id") or "") or None,
            "crm_customer_person_id": str(ticket.get("customer_person_id") or "")
            or None,
            "crm_created_by_person_id": str(ticket.get("created_by_person_id") or "")
            or None,
            "crm_assigned_to_person_id": str(ticket.get("assigned_to_person_id") or "")
            or None,
            "crm_ticket_manager_person_id": str(
                ticket.get("ticket_manager_person_id") or ""
            )
            or None,
            "crm_service_team_id": str(ticket.get("service_team_id") or "") or None,
        }
    )
    return {key: value for key, value in merged.items() if value is not None}


def _crm_ticket_id_filter(crm_ticket_id: str):
    return Ticket.metadata_["crm_ticket_id"].as_string() == crm_ticket_id


def _find_existing_ticket(db: Session, ticket: dict[str, Any]) -> Ticket | None:
    crm_ticket_id = str(ticket.get("id") or "").strip()
    number = str(ticket.get("number") or "").strip()
    query = db.query(Ticket)
    filters = []
    if number:
        filters.append(Ticket.number == number)
    if crm_ticket_id:
        filters.append(_crm_ticket_id_filter(crm_ticket_id))
    if not filters:
        return None
    return query.filter(or_(*filters)).first()


def _find_local_subscriber_id(
    db: Session,
    client: CrmTicketObservationSource,
    crm_subscriber_id: str | None,
) -> UUID | None:
    if not crm_subscriber_id:
        return None
    try:
        crm_subscriber = client.get_subscriber(crm_subscriber_id)
    except CRMClientError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("CRM subscriber lookup failed id=%s: %s", crm_subscriber_id, exc)
        return None

    external_system = str(crm_subscriber.get("external_system") or "").lower()
    external_id = str(crm_subscriber.get("external_id") or "").strip()
    if external_system != "splynx" or not external_id:
        return None
    try:
        splynx_customer_id = int(external_id)
    except ValueError:
        return None
    subscriber = (
        db.query(Subscriber)
        .filter(Subscriber.splynx_customer_id == splynx_customer_id)
        .first()
    )
    return subscriber.id if subscriber else None


def _extract_customer_id_pair_matches(text: Any) -> set[int]:
    if not text:
        return set()
    matches: set[int] = set()
    for match in _CUSTOMER_ID_PAIR_RE.finditer(str(text)):
        short_id = re.sub(r"\D", "", match.group(2))
        if not short_id:
            continue
        try:
            matches.add(int(short_id))
        except ValueError:
            continue
    return matches


def _find_local_subscriber_id_from_ticket_text(
    crm_ticket: dict[str, Any],
    local_by_splynx: dict[int, UUID] | None,
) -> UUID | None:
    if not local_by_splynx:
        return None

    for field_name in ("title", "description"):
        matched_ids = {
            customer_id
            for customer_id in _extract_customer_id_pair_matches(
                crm_ticket.get(field_name)
            )
            if customer_id in local_by_splynx
        }
        if len(matched_ids) == 1:
            return local_by_splynx[next(iter(matched_ids))]
        if len(matched_ids) > 1:
            return None
    return None


def load_local_crm_id_map(db: Session) -> dict[str, UUID]:
    """Map stored CRM subscriber UUIDs (and aliases) to local subscriber IDs.

    The CRM holds some customers twice (an imported and an erpnext-sourced
    record). The primary link lives in crm_subscriber_id; duplicate CRM ids are
    kept in metadata crm_alias_ids so tickets attached to either record map to
    the same local subscriber.
    """
    mapping: dict[str, UUID] = {}
    for subscriber_id, metadata in (
        db.query(Subscriber.id, Subscriber.metadata_)
        .filter(Subscriber.metadata_.isnot(None))
        .all()
    ):
        for alias in (metadata or {}).get("crm_alias_ids") or []:
            alias_key = str(alias).strip()
            if alias_key:
                mapping[alias_key] = subscriber_id
    # Primary links win over aliases on any key collision.
    mapping.update(
        {
            str(crm_subscriber_id): subscriber_id
            for crm_subscriber_id, subscriber_id in db.query(
                Subscriber.crm_subscriber_id, Subscriber.id
            )
            .filter(Subscriber.crm_subscriber_id.isnot(None))
            .all()
        }
    )
    return mapping


def _persist_crm_subscriber_id(
    db: Session,
    subscriber_id: UUID,
    crm_subscriber_id: str,
    local_by_crm_id: dict[str, UUID] | None,
) -> None:
    """Store a CRM link resolved via the legacy chain so the next run is direct.

    Skips if any subscriber already holds this CRM id (partial unique index).
    """
    if local_by_crm_id is not None and crm_subscriber_id in local_by_crm_id:
        return
    parsed = _coerce_uuid(crm_subscriber_id)
    subscriber = db.get(Subscriber, subscriber_id)
    if parsed is None or subscriber is None or subscriber.crm_subscriber_id:
        return
    subscriber.crm_subscriber_id = parsed
    if local_by_crm_id is not None:
        local_by_crm_id[crm_subscriber_id] = subscriber_id


def build_subscriber_cache(
    db: Session,
    client: CrmTicketObservationSource,
    *,
    max_pages: int = 200,
) -> dict[str, UUID]:
    """Map CRM subscriber UUIDs to local subscribers via imported external IDs."""
    return build_subscriber_cache_from_map(
        load_local_subscriber_map(db),
        client,
        max_pages=max_pages,
    )


def load_local_subscriber_map(db: Session) -> dict[int, UUID]:
    """Map imported customer IDs to local subscriber IDs."""
    return {
        int(splynx_customer_id): subscriber_id
        for splynx_customer_id, subscriber_id in db.query(
            Subscriber.splynx_customer_id, Subscriber.id
        )
        .filter(Subscriber.splynx_customer_id.isnot(None))
        .all()
    }


def build_subscriber_cache_from_map(
    local_by_splynx: dict[int, UUID],
    client: CrmTicketObservationSource,
    *,
    max_pages: int = 200,
) -> dict[str, UUID]:
    """Map CRM subscriber UUIDs to local subscribers without touching the DB."""
    if not local_by_splynx:
        return {}

    cache: dict[str, UUID] = {}
    for page in range(1, max(max_pages, 1) + 1):
        items = client.list_subscribers(
            external_system="splynx", page=page, per_page=100, use_cache=False
        )
        if not items:
            break
        for item in items:
            crm_id = str(item.get("id") or "").strip()
            external_id = str(item.get("external_id") or "").strip()
            if not crm_id or not external_id:
                continue
            try:
                splynx_id = int(external_id)
            except ValueError:
                continue
            local_subscriber_id = local_by_splynx.get(splynx_id)
            if local_subscriber_id:
                cache[crm_id] = local_subscriber_id
        if len(items) < 100:
            break
    return cache


def _apply_ticket_fields(
    ticket: Ticket,
    crm_ticket: dict[str, Any],
    subscriber_id: UUID,
) -> None:
    ticket.subscriber_id = subscriber_id
    ticket.customer_account_id = subscriber_id
    ticket.number = str(crm_ticket.get("number") or "").strip() or None
    ticket.title = str(crm_ticket.get("title") or "").strip() or "CRM ticket"
    ticket.description = crm_ticket.get("description")
    ticket.region = crm_ticket.get("region")
    from app.services.support import transition_ticket_status

    _crm_status = _enum_value(crm_ticket.get("status")) or "open"
    try:
        # Local precedence: a CRM pull never reopens a locally-terminal ticket
        # (closed/canceled/merged). A new ticket (no current status) takes the
        # CRM status as-is.
        transition_ticket_status(ticket, _crm_status, source="crm_pull")
    except ValueError:
        logger.warning("crm_pull ignored invalid CRM status %r", _crm_status)
    ticket.priority = _enum_value(crm_ticket.get("priority")) or "normal"
    ticket.ticket_type = crm_ticket.get("ticket_type")
    ticket.channel = _safe_channel(crm_ticket.get("channel"))
    ticket.tags = _clean_tags(crm_ticket.get("tags"))
    # Merge, never replace: sub-owned decision state on the ticket (csat,
    # resolution confirmation, the field-visit work_order_id link) must
    # survive a CRM pull. CRM-derived keys (crm_*, sync_source, and the CRM
    # payload's own metadata) update in place; keys CRM knows nothing about
    # are preserved.
    ticket.metadata_ = {**(ticket.metadata_ or {}), **_metadata(crm_ticket)}
    ticket.attachments = _clean_attachments(crm_ticket.get("attachments"))
    ticket.due_at = _parse_datetime(crm_ticket.get("due_at"))
    ticket.resolved_at = _parse_datetime(crm_ticket.get("resolved_at"))
    ticket.closed_at = _parse_datetime(crm_ticket.get("closed_at"))
    ticket.is_active = bool(crm_ticket.get("is_active", True))
    created_at = _parse_datetime(crm_ticket.get("created_at"))
    if created_at and not ticket.created_at:
        ticket.created_at = created_at


def _comment_exists(db: Session, crm_comment_id: str) -> bool:
    if not crm_comment_id:
        return False
    return (
        db.query(TicketComment.id)
        .filter(TicketComment.metadata_["crm_comment_id"].as_string() == crm_comment_id)
        .first()
        is not None
    )


def _sync_comments(
    db: Session,
    client: CrmTicketObservationSource,
    local_ticket: Ticket,
    crm_ticket_id: str,
) -> int:
    created = 0
    for comment in client.list_ticket_comments(crm_ticket_id, use_cache=False):
        crm_comment_id = str(comment.get("id") or "").strip()
        if not crm_comment_id or _comment_exists(db, crm_comment_id):
            continue
        db.add(
            TicketComment(
                ticket_id=local_ticket.id,
                author_person_id=None,
                author_type="system",
                body=str(comment.get("body") or "").strip() or "(empty comment)",
                # CRM is provenance-only for tickets. Its legacy boolean was a
                # storage default, not evidence that a customer publication
                # decision occurred in Selfcare.
                is_internal=True,
                attachments=_clean_attachments(comment.get("attachments")),
                metadata_={
                    "sync_source": "crm",
                    "crm_comment_id": crm_comment_id,
                    "crm_ticket_id": crm_ticket_id,
                    "crm_author_person_id": str(comment.get("author_person_id") or "")
                    or None,
                },
                created_at=_parse_datetime(comment.get("created_at"))
                or datetime.now(UTC),
            )
        )
        created += 1
    return created


def sync_ticket(
    db: Session,
    crm_ticket: dict[str, Any],
    *,
    client: CrmTicketObservationSource | None = None,
    sync_comments: bool = True,
    fetch_comments_when_unchanged: bool = True,
    subscriber_cache: dict[str, UUID] | None = None,
    local_by_splynx: dict[int, UUID] | None = None,
    local_by_crm_id: dict[str, UUID] | None = None,
) -> tuple[str, int, Ticket | None]:
    client = client or capability_client(db)
    crm_ticket_id = str(crm_ticket.get("id") or "").strip()
    if not crm_ticket_id:
        raise ValueError("CRM ticket id is required")
    if crm_ticket.get("lead_id") and not crm_ticket.get("subscriber_id"):
        return "skipped_lead", 0, None

    crm_subscriber_id = str(crm_ticket.get("subscriber_id") or "").strip() or None
    subscriber_id: UUID | None = None
    resolved_via_legacy_chain = False
    if crm_subscriber_id and local_by_crm_id is not None:
        subscriber_id = local_by_crm_id.get(crm_subscriber_id)
    if not subscriber_id and crm_subscriber_id:
        subscriber_id = (
            subscriber_cache.get(crm_subscriber_id)
            if subscriber_cache is not None
            else _find_local_subscriber_id(db, client, crm_subscriber_id)
        )
        resolved_via_legacy_chain = subscriber_id is not None
    if not subscriber_id:
        subscriber_id = _find_local_subscriber_id_from_ticket_text(
            crm_ticket,
            local_by_splynx,
        )
    if not subscriber_id:
        return "skipped_unmapped_subscriber", 0, None
    if resolved_via_legacy_chain and crm_subscriber_id:
        # The CRM subscriber's imported external id matched a local subscriber;
        # store the direct link. Text-regex matches are NOT persisted — the
        # title may name a different customer than the ticket's CRM subscriber.
        _persist_crm_subscriber_id(
            db, subscriber_id, crm_subscriber_id, local_by_crm_id
        )

    existing = _find_existing_ticket(db, crm_ticket)
    previous_status = existing.status if existing else None
    if existing:
        # Unchanged in the CRM since we last synced it → skip the rewrite.
        # CRM comment creation does not bump the ticket's updated_at, so the
        # caller decides whether comments still need a look (full sweeps yes,
        # incremental runs handle open tickets separately).
        incoming_updated = str(crm_ticket.get("updated_at") or "")
        stored_updated = str((existing.metadata_ or {}).get("crm_updated_at") or "")
        if incoming_updated and incoming_updated == stored_updated:
            comments_created = 0
            if sync_comments and fetch_comments_when_unchanged:
                comments_created = _sync_comments(db, client, existing, crm_ticket_id)
                db.flush()
            return "unchanged", comments_created, existing
        local_ticket = existing
        outcome = "updated"
    else:
        local_ticket = Ticket(title="CRM ticket")
        db.add(local_ticket)
        db.flush()
        outcome = "created"

    _apply_ticket_fields(local_ticket, crm_ticket, subscriber_id)

    # Proactively notify the customer when their ticket is newly closed
    # (mirrors the work-order/project push). Best-effort — a push failure never
    # breaks the sync.
    # Ticket.status is a plain string column, and transition_ticket_status
    # writes the enum's *value* — so compare against the string, not the member.
    if (
        subscriber_id
        and local_ticket.status == TicketStatus.closed.value
        and previous_status != TicketStatus.closed.value
    ):
        try:
            from app.services import push as push_service

            push_service.send_push(
                db,
                str(subscriber_id),
                title="Support ticket closed",
                body="Your support ticket has been closed.",
                data={"type": "ticket", "ticket_id": str(local_ticket.id)},
            )
        except Exception as exc:  # noqa: BLE001 - notification is advisory
            logger.warning(
                "ticket_closed_push_failed ticket=%s: %s", crm_ticket_id, exc
            )

    comments_created = 0
    if sync_comments:
        comments_created = _sync_comments(db, client, local_ticket, crm_ticket_id)
    db.flush()
    return outcome, comments_created, local_ticket


def sync_ticket_by_id(
    db: Session,
    crm_ticket_id: str,
    *,
    client: CrmTicketObservationSource | None = None,
    sync_comments: bool = True,
) -> CrmTicketPullResult:
    client = client or capability_client(db)
    result = CrmTicketPullResult()
    crm_ticket = client.get_ticket(crm_ticket_id)
    subscriber_cache = build_subscriber_cache(db, client)
    local_by_splynx = load_local_subscriber_map(db)
    local_by_crm_id = load_local_crm_id_map(db)
    result.fetched = 1
    outcome, comments_created, local_ticket = sync_ticket(
        db,
        crm_ticket,
        client=client,
        sync_comments=sync_comments,
        subscriber_cache=subscriber_cache,
        local_by_splynx=local_by_splynx,
        local_by_crm_id=local_by_crm_id,
    )
    if outcome == "created":
        result.created += 1
    elif outcome == "updated":
        result.updated += 1
    elif outcome == "skipped_lead":
        result.skipped_leads += 1
    elif outcome == "skipped_unmapped_subscriber":
        result.skipped_unmapped_subscribers += 1
    result.comments_created += comments_created
    from app.services import support as support_service

    support_service.Tickets.reconcile_ticket_number_sequence(db)
    return result


# Synced tickets in these states get a comment look on every incremental run:
# new CRM comments do not bump the ticket's updated_at, so the watermark alone
# would miss them. Closed-ticket comments are rare and heal in the full sweep.
# Overlap margin on the incremental watermark: tolerates clock skew between
# the CRM and us, and tickets updated while a previous run was paging.
WATERMARK_MARGIN = timedelta(minutes=10)

OPEN_SWEEP_STATUSES = (
    "new",
    "open",
    "pending",
    "waiting_on_customer",
    "on_hold",
    "lastmile_rerun",
    "site_under_construction",
)


def latest_crm_updated_at(db: Session) -> datetime | None:
    """Most recent CRM updated_at across synced tickets (the pull watermark)."""
    latest: datetime | None = None
    rows = db.query(Ticket.metadata_).filter(
        Ticket.metadata_["sync_source"].as_string() == "crm"
    )
    for (metadata,) in rows:
        parsed = _parse_datetime((metadata or {}).get("crm_updated_at"))
        if parsed and (latest is None or parsed > latest):
            latest = parsed
    return latest


def _sweep_open_ticket_comments(
    db: Session,
    client: CrmTicketObservationSource,
    already_synced: set[str],
) -> int:
    """Fetch comments for open-state synced tickets not covered this run."""
    created = 0
    open_tickets = (
        db.query(Ticket)
        .filter(
            Ticket.metadata_["sync_source"].as_string() == "crm",
            Ticket.status.in_(OPEN_SWEEP_STATUSES),
        )
        .all()
    )
    for ticket in open_tickets:
        crm_ticket_id = str((ticket.metadata_ or {}).get("crm_ticket_id") or "")
        if not crm_ticket_id or crm_ticket_id in already_synced:
            continue
        try:
            created += _sync_comments(db, client, ticket, crm_ticket_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("comment sweep failed ticket=%s: %s", crm_ticket_id, exc)
    if created:
        db.flush()
    return created


def pull_tickets(
    db: Session,
    *,
    client: CrmTicketObservationSource | None = None,
    limit: int = 200,
    max_pages: int = 50,
    sync_comments: bool = True,
    since: datetime | None = None,
    subscriber_cache: dict[str, UUID] | None = None,
    local_by_splynx: dict[int, UUID] | None = None,
    local_by_crm_id: dict[str, UUID] | None = None,
    record_callback: CrmTicketRecordCallback | None = None,
) -> CrmTicketPullResult:
    """Pull CRM tickets into local support tickets.

    With ``since`` set (incremental mode), pages are walked in updated_at
    descending order and the walk stops at the first ticket older than
    ``since``; unchanged tickets skip the field rewrite and the per-ticket
    comment fetch, and open synced tickets get a separate comment sweep.
    Without ``since`` (full mode) every ticket is visited and comments are
    fetched even for unchanged tickets — the drift-healing reconciliation.
    """
    client = client or capability_client(db)
    page_size = min(max(limit, 1), 200)
    result = CrmTicketPullResult()
    local_by_splynx = local_by_splynx or load_local_subscriber_map(db)
    subscriber_cache = subscriber_cache or build_subscriber_cache_from_map(
        local_by_splynx, client
    )
    if local_by_crm_id is None:
        local_by_crm_id = load_local_crm_id_map(db)
    incremental = since is not None
    comments_synced: set[str] = set()
    reached_watermark = False
    for page in range(max(max_pages, 1)):
        if reached_watermark:
            break
        offset = page * page_size
        try:
            tickets = client.list_tickets(
                limit=page_size,
                offset=offset,
                order_by="updated_at",
                order_dir="desc",
                use_cache=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("CRM ticket page fetch failed offset=%s", offset)
            result.errors.append({"ticket_id": "", "error": str(exc)})
            break
        if not tickets:
            break
        for crm_ticket in tickets:
            if incremental:
                updated_at = _parse_datetime(crm_ticket.get("updated_at"))
                if updated_at and since is not None and updated_at < since:
                    # Pages are updated_at-descending: everything from here
                    # back was already synced.
                    reached_watermark = True
                    break
            result.fetched += 1
            try:
                outcome, comments_created, local_ticket = sync_ticket(
                    db,
                    crm_ticket,
                    client=client,
                    sync_comments=sync_comments,
                    fetch_comments_when_unchanged=not incremental,
                    subscriber_cache=subscriber_cache,
                    local_by_splynx=local_by_splynx,
                    local_by_crm_id=local_by_crm_id,
                )
            except Exception as exc:  # noqa: BLE001
                ticket_id = str(crm_ticket.get("id") or "")
                logger.exception("CRM ticket sync failed ticket_id=%s", ticket_id)
                result.errors.append({"ticket_id": ticket_id, "error": str(exc)})
                if record_callback:
                    record_callback(crm_ticket, "failed", 0, str(exc), None)
                continue
            if outcome == "created":
                result.created += 1
            elif outcome == "updated":
                result.updated += 1
            elif outcome == "unchanged":
                result.unchanged += 1
            elif outcome == "skipped_lead":
                result.skipped_leads += 1
            elif outcome == "skipped_unmapped_subscriber":
                result.skipped_unmapped_subscribers += 1
            result.comments_created += comments_created
            if sync_comments and outcome in ("created", "updated"):
                comments_synced.add(str(crm_ticket.get("id") or ""))
            if record_callback:
                record_callback(
                    crm_ticket,
                    outcome,
                    comments_created,
                    None,
                    local_ticket.id if local_ticket else None,
                )
        if len(tickets) < page_size:
            break
    if incremental and sync_comments:
        result.comments_created += _sweep_open_ticket_comments(
            db, client, comments_synced
        )
    # Imported CRM numbers bypass native allocation. Advance the local sequence
    # in this same transaction so the next customer/admin ticket cannot start
    # from a stale value and repeatedly collide with imported rows.
    from app.services import support as support_service

    support_service.Tickets.reconcile_ticket_number_sequence(db)
    return result


def delete_all_local_support_tickets(db: Session) -> dict[str, int]:
    """Delete local support tickets and dependent local support rows.

    This is intended for the approved one-time cleanup of test tickets before
    the CRM ticket import becomes the source of truth for support ticket numbers.
    """
    from app.models.provisioning import ServiceOrder
    from app.models.support import (
        TicketAssignee,
        TicketLink,
        TicketMerge,
        TicketSlaEvent,
    )

    ticket_ids = [row[0] for row in db.query(Ticket.id).all()]
    if not ticket_ids:
        return {
            "tickets": 0,
            "comments": 0,
            "sla_events": 0,
            "assignees": 0,
            "links": 0,
            "merges": 0,
            "field_visit_service_orders": 0,
        }

    work_order_ids: list[UUID] = []
    for metadata in db.query(Ticket.metadata_).filter(Ticket.id.in_(ticket_ids)):
        value = (metadata[0] or {}).get("work_order_id")
        parsed = _coerce_uuid(str(value)) if value else None
        if parsed:
            work_order_ids.append(parsed)

    counts = {
        "comments": db.query(TicketComment)
        .filter(TicketComment.ticket_id.in_(ticket_ids))
        .delete(synchronize_session=False),
        "sla_events": db.query(TicketSlaEvent)
        .filter(TicketSlaEvent.ticket_id.in_(ticket_ids))
        .delete(synchronize_session=False),
        "assignees": db.query(TicketAssignee)
        .filter(TicketAssignee.ticket_id.in_(ticket_ids))
        .delete(synchronize_session=False),
        "links": db.query(TicketLink)
        .filter(
            or_(
                TicketLink.from_ticket_id.in_(ticket_ids),
                TicketLink.to_ticket_id.in_(ticket_ids),
            )
        )
        .delete(synchronize_session=False),
        "merges": db.query(TicketMerge)
        .filter(
            or_(
                TicketMerge.source_ticket_id.in_(ticket_ids),
                TicketMerge.target_ticket_id.in_(ticket_ids),
            )
        )
        .delete(synchronize_session=False),
    }
    counts["tickets"] = (
        db.query(Ticket)
        .filter(Ticket.id.in_(ticket_ids))
        .delete(synchronize_session=False)
    )
    counts["field_visit_service_orders"] = 0
    if work_order_ids:
        counts["field_visit_service_orders"] = (
            db.query(ServiceOrder)
            .filter(ServiceOrder.id.in_(work_order_ids))
            .delete(synchronize_session=False)
        )
    db.flush()
    return counts
