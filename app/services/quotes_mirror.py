"""Local mirror of CRM self-serve quote data (Sales/Quotes tracker).

All DB + CRM access for the customer-facing quote flow lives here so the API/web
wrappers stay thin. The CRM owns quotes; this keeps a read-optimised local copy
preserved as historical data after retirement. Reconciliation and commands
never contact CRM, even if a binding remains enabled. The
estimate/feasibility/deposit are computed by the CRM; this is a faithful copy.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.quote_mirror import QuoteMirror, QuoteSyncState
from app.models.subscriber import Subscriber
from app.schemas.notification import PushIntent
from app.services.audit_adapter import AuditActor, record_audit_event
from app.services.common import coerce_uuid
from app.services.domain_errors import DomainError
from app.services.quote_retirement import retirement_outcome

logger = logging.getLogger(__name__)

_DEFAULT_REFRESH_TTL_SECONDS = (
    300  # compatibility parameter; retired reads never refresh
)


class QuoteReadState(StrEnum):
    retired = "retired"
    current = "current"
    stale = "stale"
    unavailable = "unavailable"


@dataclass(frozen=True)
class QuoteReadResult:
    payload: dict[str, object]
    state: QuoteReadState


# ---------------------------------------------------------------------------
# Portal quote COMMANDS (Sub -> CRM) - fail-closed contract
#
# ``request_quote`` and ``accept_quote`` are the last two Sub -> CRM business
# writes (tests/architecture/test_no_crm_writeback.py::DEFERRED_MUTATIONS) and
# they sit on a customer-money path: ``accept_quote`` is the tail of
# ``quote_deposits.verify_deposit``, which has already recorded a deposit
# payment in Sub's ledger by the time it runs.
#
# The CRM/Omni runtime was decommissioned on 2026-08-29, so this transport now
# addresses a system that is not merely unreachable but gone. This module is
# the single owner of what that means, and it owns exactly three guarantees:
#
#   1. EXPLICIT TYPED REFUSAL. A command that cannot be completed raises
#      ``PortalQuoteCommandError`` - never a bare transport ``HTTPException``
#      whose 502 reads as "retry in a minute" for a permanent condition.
#   2. NO SILENT SUCCESS. A command that returns no acknowledged quote
#      identity is a failure, not an empty ``{}`` behind an HTTP 200.
#   3. NO PARTIAL STATE. Nothing is mirrored, and no money is recorded, for a
#      command the CRM did not acknowledge. Callers that are about to move
#      money ask ``ensure_portal_quote_commands_available`` FIRST.
#
# The precondition resolves local configuration only and makes no network
# call, so a retired CRM is refused immediately instead of hanging for a
# connect timeout.
# ---------------------------------------------------------------------------

#: Audit ``entity_type`` for every refused portal quote command.
PORTAL_QUOTE_COMMAND_ENTITY = "portal_quote_command"

#: The typed audit principal for a refusal. A refusal is decided by this owner
#: from local configuration, not by whoever happened to make the request, so the
#: honest actor is the service. Deliberately no ``party_id``: accountability
#: enrichment may describe the person behind a user, but it must never turn a
#: service actor into a person.
PORTAL_QUOTE_COMMAND_ACTOR = AuditActor(
    actor_type=AuditActorType.system,
    actor_id="service:sales.portal_quote",
    label="Portal quote command owner",
)

#: Audit ``action`` values. One per refusal site, so an operator can tell a
#: refused request apart from a refused acceptance apart from a deposit that
#: was never taken.
PORTAL_QUOTE_REQUEST_REFUSED = "sales.portal_quote.request_refused"
PORTAL_QUOTE_ACCEPT_REFUSED = "sales.portal_quote.accept_refused"
PORTAL_QUOTE_DEPOSIT_PREFLIGHT_REFUSED = "sales.portal_quote.deposit_preflight_refused"

#: One customer-safe message for every refusal. It states the two facts a
#: customer needs - nothing was charged, nothing was changed - and leaks no
#: endpoint, host or capability detail.
PORTAL_QUOTE_UNAVAILABLE_MESSAGE = (
    "Online quoting is unavailable. Nothing was charged and no quote was "
    "changed. Please contact support to continue."
)


class PortalQuoteCommandError(DomainError):
    """Transport-neutral refusal of a Sub -> CRM portal quote command.

    Raised instead of returning a partial or empty result. Adapters map it;
    the shared ``DomainError`` handler in ``app/errors.py`` answers 409, which
    is deliberately not the old 502 - the condition is a refusal, not a
    transient upstream blip a client should retry against.
    """


@dataclass(frozen=True, slots=True)
class PortalQuoteAuditContext:
    """Who/what to record when a portal quote command is refused.

    ``details`` carries structured, non-secret decision evidence only: ids and
    reasons, never recipient data, endpoints or credential references.
    """

    action: str
    subscriber_id: str
    details: Mapping[str, str] = field(default_factory=dict)


def _portal_quote_error(suffix: str, **details: object) -> PortalQuoteCommandError:
    return PortalQuoteCommandError(
        code=f"sales.portal_quote.{suffix}",
        message=PORTAL_QUOTE_UNAVAILABLE_MESSAGE,
        details=details,
    )


def _audit_portal_quote_refusal(
    db: Session,
    context: PortalQuoteAuditContext,
    error: PortalQuoteCommandError,
) -> None:
    """Record durable evidence that a money-path quote command was refused.

    Committed on its own: the command is refused, so there is no business
    transaction to attach the row to, and the refusal must survive regardless.
    A failure to audit is logged and must never mask the refusal itself -
    failing closed is the stronger guarantee of the two.
    """
    metadata: dict[str, object] = {
        "subscriber_id": context.subscriber_id,
        "error_code": error.code,
    }
    metadata.update({key: str(value) for key, value in context.details.items()})
    metadata.update({key: str(value) for key, value in error.details.items()})
    try:
        record_audit_event(
            db,
            action=context.action,
            entity_type=PORTAL_QUOTE_COMMAND_ENTITY,
            entity_id=context.subscriber_id,
            actor=PORTAL_QUOTE_COMMAND_ACTOR,
            metadata=metadata,
            status_code=409,
            is_success=False,
        )
    except Exception:  # pragma: no cover - defensive; refusal still stands
        logger.exception(
            "portal_quote_refusal_audit_failed action=%s error_code=%s",
            context.action,
            error.code,
        )


def ensure_portal_quote_commands_available(
    db: Session,
    *,
    audit_context: PortalQuoteAuditContext | None = None,
) -> None:
    error = _portal_quote_error("retired", reason=retirement_outcome().status)
    if audit_context is not None:
        _audit_portal_quote_refusal(db, audit_context, error)
    raise error


def _to_dt(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _to_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _to_int(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return None


def _upsert_row(
    db: Session, *, subscriber_id, item: dict[str, object]
) -> QuoteMirror | None:
    crm_quote_id = str(item.get("id") or "").strip()
    if not crm_quote_id:
        return None
    row = db.scalar(select(QuoteMirror).where(QuoteMirror.crm_quote_id == crm_quote_id))
    if row is None:
        row = QuoteMirror(crm_quote_id=crm_quote_id, subscriber_id=subscriber_id)
        db.add(row)
    row.subscriber_id = subscriber_id
    feasibility_raw = item.get("feasibility")
    feasibility = feasibility_raw if isinstance(feasibility_raw, dict) else {}
    if item.get("status"):
        row.status = str(item["status"])
    if item.get("currency"):
        row.currency = str(item["currency"])
    if item.get("total") is not None:
        row.total = str(item["total"])
    if item.get("deposit_amount") is not None:
        row.deposit_amount = str(item["deposit_amount"])
    if item.get("deposit_percent") is not None:
        row.deposit_percent = _to_int(item.get("deposit_percent"))
    if item.get("deposit_paid") is not None:
        row.deposit_paid = bool(item["deposit_paid"])
    if feasibility.get("coverage") is not None:
        row.feasibility_coverage = str(feasibility["coverage"])
    if item.get("estimate_provisional") is not None:
        row.estimate_provisional = bool(item["estimate_provisional"])
    if item.get("address") is not None:
        row.address = str(item["address"])
    if item.get("latitude") is not None:
        row.latitude = _to_float(item.get("latitude"))
    if item.get("longitude") is not None:
        row.longitude = _to_float(item.get("longitude"))
    if item.get("project_id") is not None:
        row.project_id = str(item["project_id"])
    if item.get("sales_order_id") is not None:
        row.sales_order_id = str(item["sales_order_id"])
    if item.get("created_at") is not None:
        row.quote_created_at = _to_dt(item.get("created_at"))
    # Keep the full CRM payload for rich rendering (line items, feasibility detail).
    row.payload = item
    return row


def _local_subscriber(db: Session, body: dict[str, object]) -> Subscriber | None:
    local_id = str(body.get("subscriber_id") or "").strip()
    if local_id:
        try:
            sub = db.get(Subscriber, coerce_uuid(local_id))
        except (ValueError, TypeError):
            sub = None
        if sub is not None:
            return sub
    crm_subscriber_id = str(body.get("crm_subscriber_id") or "").strip()
    if crm_subscriber_id:
        try:
            crm_uuid = coerce_uuid(crm_subscriber_id)
        except (ValueError, TypeError):
            return None
        return db.scalar(
            select(Subscriber).where(Subscriber.crm_subscriber_id == crm_uuid)
        )
    return None


def reconcile_subscriber(db: Session, subscriber_id: str) -> bool:
    """Compatibility entry point for queued jobs; CRM is retired."""
    return False


def reconcile_all(db: Session, *, stale_after_seconds: int = 3600) -> int:
    """Retired: preserve mirrors and synchronization timestamps unchanged."""
    return 0


def _row_to_item(row: QuoteMirror) -> dict[str, object]:
    if isinstance(row.payload, dict) and row.payload:
        item = dict(row.payload)
        item["id"] = row.crm_quote_id
        return item
    # Fallback shape from columns if the full payload was never stored.
    return {
        "id": row.crm_quote_id,
        "status": row.status,
        "currency": row.currency,
        "total": row.total,
        "deposit_amount": row.deposit_amount,
        "deposit_percent": row.deposit_percent,
        "deposit_paid": row.deposit_paid,
        "feasibility": {"coverage": row.feasibility_coverage},
        "estimate_provisional": row.estimate_provisional,
        "address": row.address,
        "latitude": row.latitude,
        "longitude": row.longitude,
        "project_id": row.project_id,
        "sales_order_id": row.sales_order_id,
        "created_at": row.quote_created_at.isoformat()
        if row.quote_created_at
        else None,
    }


def read_for_subscriber(
    db: Session,
    subscriber_id: str,
    *,
    refresh_ttl_seconds: int = _DEFAULT_REFRESH_TTL_SECONDS,
) -> dict[str, object]:
    """Return the stable mobile/API payload without transport-state fields."""
    return read_for_subscriber_result(
        db,
        subscriber_id,
        refresh_ttl_seconds=refresh_ttl_seconds,
    ).payload


def read_for_subscriber_result(
    db: Session,
    subscriber_id: str,
    *,
    refresh_ttl_seconds: int = _DEFAULT_REFRESH_TTL_SECONDS,
) -> QuoteReadResult:
    """Build quotes plus explicit CRM projection freshness for web renderers."""
    sub_uuid = coerce_uuid(str(subscriber_id))
    rows = db.scalars(
        select(QuoteMirror)
        .where(QuoteMirror.subscriber_id == sub_uuid)
        .order_by(QuoteMirror.created_at.desc())
    ).all()
    items = [_row_to_item(r) for r in rows]
    open_count = sum(
        1 for r in rows if r.status not in ("accepted", "rejected", "expired")
    )
    return QuoteReadResult(
        payload={
            "quotes": items,
            "total": len(items),
            "open": open_count,
            "source_state": retirement_outcome().status,
            "actions_available": retirement_outcome().actions_available,
        },
        state=QuoteReadState.retired,
    )


def request_quote(
    db: Session,
    subscriber_id: str,
    *,
    latitude: float,
    longitude: float,
    address: str | None = None,
    region: str | None = None,
    note: str | None = None,
) -> dict[str, object]:
    """Refuse retired CRM quoting before resolving customer data or transport."""
    ensure_portal_quote_commands_available(
        db,
        audit_context=PortalQuoteAuditContext(
            action=PORTAL_QUOTE_REQUEST_REFUSED,
            subscriber_id=subscriber_id,
        ),
    )
    raise AssertionError("retired quote command must refuse")


def accept_quote(
    db: Session,
    subscriber_id: str,
    quote_id: str,
    *,
    deposit_reference: str,
    deposit_amount: str,
    provider: str | None = None,
) -> dict:
    """Refuse retired CRM acceptance; native Selfcare quoting has its own owner."""
    ensure_portal_quote_commands_available(
        db,
        audit_context=PortalQuoteAuditContext(
            action=PORTAL_QUOTE_ACCEPT_REFUSED,
            subscriber_id=subscriber_id,
        ),
    )
    raise AssertionError("retired quote command must refuse")


_STATUS_EVENTS = {
    "quote.created",
    "quote.updated",
    "quote.accepted",
    "quote.rejected",
}


def apply_webhook(db: Session, event_type: str, body: dict) -> dict:
    """Apply a CRM quote lifecycle event to the mirror. The webhook carries a
    lightweight payload → upsert + mark stale so the next read pulls full detail."""
    crm_quote_id = str(body.get("quote_id") or body.get("id") or "").strip()
    if not crm_quote_id:
        return {"status": "ignored", "reason": "incomplete_payload"}
    if event_type not in _STATUS_EVENTS:
        return {"status": "ignored", "event": event_type}

    subscriber = _local_subscriber(db, body)
    if subscriber is None:
        logger.warning(
            "crm_quote_event_unmapped event=%s quote_id=%s", event_type, crm_quote_id
        )
        return {"status": "ignored", "reason": "unmapped_subscriber"}

    item = dict(body)
    item["id"] = crm_quote_id
    _upsert_row(db, subscriber_id=subscriber.id, item=item)
    # Mark stale so the next read pulls full detail from the CRM.
    sync = db.get(QuoteSyncState, subscriber.id)
    if sync is not None:
        sync.synced_at = datetime(1970, 1, 1, tzinfo=UTC)
    db.commit()

    if event_type == "quote.accepted":
        try:
            from app.services import push as push_service

            push_service.send_push(
                db,
                str(subscriber.id),
                title="Quote accepted",
                body="Your installation is being scheduled.",
                intent=PushIntent(
                    intent_code="quote.accepted",
                    subject_kind="quote",
                    subject_id=crm_quote_id,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - notification is advisory
            logger.warning("quote_push_failed quote_id=%s: %s", crm_quote_id, exc)

    return {"status": "ok", "event": event_type}
