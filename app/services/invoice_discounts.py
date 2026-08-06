"""Typed Invoice discount participant and append-only history query owner."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.billing import (
    Invoice,
    InvoiceDiscountAction,
    InvoiceDiscountHistory,
    InvoiceDiscountSource,
    InvoiceDiscountType,
    InvoiceStatus,
)
from app.models.party import Party
from app.models.subscriber import Subscriber
from app.models.system_user import SystemUser
from app.services.billing._common import _recalculate_invoice_totals
from app.services.common import round_money
from app.services.domain_errors import DomainError
from app.services.events import EventType, emit_event
from app.timezone import APP_TIMEZONE

OWNER = "financial.invoice_discounts"


class InvoiceDiscountError(DomainError, ValueError):
    """Stable rejection raised by the Invoice discount owner."""


@dataclass(frozen=True, slots=True)
class InvoiceDiscountInput:
    discount_type: InvoiceDiscountType
    value: Decimal
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ResolvedInvoiceDiscount:
    discount_type: InvoiceDiscountType
    value: Decimal
    amount: Decimal
    reason: str | None


@dataclass(frozen=True, slots=True)
class StageInvoiceDiscountCommand:
    invoice_id: UUID
    actor_system_user_id: UUID
    command_id: UUID
    discount: InvoiceDiscountInput | None
    source: InvoiceDiscountSource = InvoiceDiscountSource.manual
    source_quote_id: UUID | None = None
    applied_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class InvoiceDiscountHistoryQuery:
    date_from: date | None = None
    date_to: date | None = None
    customer: str | None = None
    salesperson_id: UUID | None = None
    discount_type: InvoiceDiscountType | None = None
    invoice_status: InvoiceStatus | None = None
    source: InvoiceDiscountSource | None = None
    page: int = 1
    page_size: int = 25


@dataclass(frozen=True, slots=True)
class InvoiceDiscountHistoryItem:
    history_id: UUID
    invoice_id: UUID
    invoice_number: str | None
    revision: int
    customer_name: str
    currency: str
    original_subtotal: Decimal
    discount_type: InvoiceDiscountType
    discount_value: Decimal
    discount_amount: Decimal
    discounted_subtotal: Decimal
    total_after_discount: Decimal
    reason: str | None
    actor_system_user_id: UUID
    actor_name: str
    applied_at: datetime
    action: InvoiceDiscountAction
    source: InvoiceDiscountSource
    source_quote_id: UUID | None
    invoice_status: InvoiceStatus


@dataclass(frozen=True, slots=True)
class InvoiceDiscountHistoryResult:
    items: tuple[InvoiceDiscountHistoryItem, ...]
    total_count: int
    page: int
    page_size: int


@dataclass(frozen=True, slots=True)
class InvoiceDiscountActorOption:
    system_user_id: UUID
    label: str


def _error(suffix: str, message: str, **details: object) -> InvoiceDiscountError:
    return InvoiceDiscountError(
        code=f"{OWNER}.{suffix}", message=message, details=details
    )


def quote_inheritance_error(message: str) -> InvoiceDiscountError:
    """Return the stable failure for an unrepresentable Quote inheritance."""

    return _error("quote_inheritance_invalid", message)


def resolve_invoice_discount(
    subtotal: Decimal, discount: InvoiceDiscountInput | None
) -> ResolvedInvoiceDiscount | None:
    """Validate and price one mutually exclusive Invoice-level discount."""

    if discount is None:
        return None
    original_subtotal = round_money(subtotal)
    value = discount.value
    if not value.is_finite() or value <= 0:
        raise _error("value_invalid", "Discount value must be greater than zero.")
    if original_subtotal <= 0:
        raise _error(
            "subtotal_invalid",
            "Add a positive Invoice subtotal before applying a discount.",
        )
    normalized_value = round_money(value)
    if discount.discount_type is InvoiceDiscountType.percentage:
        if normalized_value > Decimal("100"):
            raise _error(
                "value_invalid", "Percentage discount cannot be greater than 100."
            )
        amount = round_money(original_subtotal * normalized_value / Decimal("100"))
    elif discount.discount_type is InvoiceDiscountType.fixed_amount:
        amount = normalized_value
    else:  # pragma: no cover - enum construction prevents this branch
        raise _error("type_invalid", "Select Percentage or Fixed Amount.")
    if amount <= 0:
        raise _error("value_invalid", "Discount is too small to reduce this Invoice.")
    if amount > original_subtotal:
        raise _error(
            "exceeds_subtotal", "Discount cannot be greater than the Invoice subtotal."
        )
    reason = (discount.reason or "").strip() or None
    if reason is not None and len(reason) > 500:
        raise _error(
            "reason_invalid", "Discount reason cannot be longer than 500 characters."
        )
    return ResolvedInvoiceDiscount(
        discount_type=discount.discount_type,
        value=normalized_value,
        amount=amount,
        reason=reason,
    )


def _active_actor(db: Session, actor_id: UUID) -> SystemUser:
    actor = db.scalar(
        select(SystemUser)
        .where(SystemUser.id == actor_id, SystemUser.is_active.is_(True))
        .with_for_update()
    )
    if actor is None:
        raise _error(
            "actor_not_eligible",
            "The logged-in staff user cannot apply Invoice discounts.",
        )
    return actor


def _fingerprint(command: StageInvoiceDiscountCommand) -> str:
    payload = {
        "invoice_id": str(command.invoice_id),
        "actor_system_user_id": str(command.actor_system_user_id),
        "source": command.source.value,
        "source_quote_id": str(command.source_quote_id)
        if command.source_quote_id
        else None,
        "discount": (
            {
                "type": command.discount.discount_type.value,
                "value": str(command.discount.value),
                "reason": command.discount.reason,
            }
            if command.discount
            else None
        ),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def stage_invoice_discount(
    db: Session,
    invoice: Invoice,
    command: StageInvoiceDiscountCommand,
) -> InvoiceDiscountHistory | None:
    """Flush-only participant used by an owning Invoice creation transaction."""

    if invoice.id != command.invoice_id:
        raise _error("invoice_mismatch", "Invoice discount target does not match.")
    if command.source is InvoiceDiscountSource.quote:
        if command.source_quote_id is None or command.discount is None:
            raise _error(
                "quote_source_invalid",
                "A Quote-inherited discount requires its Quote and discount details.",
            )
    elif command.source_quote_id is not None:
        raise _error(
            "quote_source_invalid", "A manual Invoice discount cannot name a Quote."
        )
    if invoice.discount_source == InvoiceDiscountSource.quote.value:
        existing = _current_resolved(invoice)
        requested = resolve_invoice_discount(invoice.subtotal, command.discount)
        if (
            command.source is not InvoiceDiscountSource.quote
            or command.source_quote_id != invoice.discount_source_quote_id
            or requested != existing
        ):
            raise _error(
                "inherited_locked",
                "This discount came from a Quote and cannot be changed or applied twice.",
            )
        return None

    requested = resolve_invoice_discount(invoice.subtotal, command.discount)
    current = _current_resolved(invoice)
    if requested == current and (
        requested is None or invoice.discount_source == command.source.value
    ):
        return None
    if (
        invoice.status is not InvoiceStatus.draft
        and command.source is not InvoiceDiscountSource.quote
    ):
        raise _error(
            "invoice_not_editable",
            "Only a draft Invoice can have its discount changed. Use a Credit Note after issue.",
        )
    actor = _active_actor(db, command.actor_system_user_id)
    applied_at = command.applied_at or datetime.now(UTC)
    revision = int(invoice.discount_revision or 0) + 1
    action = (
        InvoiceDiscountAction.inherited
        if command.source is InvoiceDiscountSource.quote
        else InvoiceDiscountAction.removed
        if requested is None
        else InvoiceDiscountAction.changed
        if current is not None
        else InvoiceDiscountAction.applied
    )
    evidence = current if requested is None else requested
    if evidence is None:
        return None

    if requested is None:
        invoice.discount_type = None
        invoice.discount_value = None
        invoice.discount_amount = Decimal("0.00")
        invoice.discount_reason = None
        invoice.discount_source = None
        invoice.discount_source_quote_id = None
        invoice.discount_applied_by_system_user_id = None
        invoice.discount_applied_at = None
    else:
        invoice.discount_type = requested.discount_type.value
        invoice.discount_value = requested.value
        invoice.discount_amount = requested.amount
        invoice.discount_reason = requested.reason
        invoice.discount_source = command.source.value
        invoice.discount_source_quote_id = command.source_quote_id
        invoice.discount_applied_by_system_user_id = actor.id
        invoice.discount_applied_at = applied_at
    invoice.discount_revision = revision
    _recalculate_invoice_totals(db, invoice)

    history = InvoiceDiscountHistory(
        invoice_id=invoice.id,
        revision=revision,
        action=action.value,
        source=(
            command.source.value
            if requested is not None
            else invoice.discount_source or InvoiceDiscountSource.manual.value
        ),
        source_quote_id=(command.source_quote_id if requested is not None else None),
        discount_type=evidence.discount_type.value,
        discount_value=evidence.value,
        discount_amount=evidence.amount,
        original_subtotal=round_money(invoice.subtotal),
        discounted_subtotal=round_money(invoice.discounted_subtotal),
        tax_total=round_money(invoice.tax_total),
        total_after_discount=round_money(invoice.total),
        reason=evidence.reason,
        actor_system_user_id=actor.id,
        command_id=command.command_id,
        command_fingerprint=_fingerprint(command),
        applied_at=applied_at,
    )
    db.add(history)
    event_type = {
        InvoiceDiscountAction.applied: EventType.invoice_discount_applied,
        InvoiceDiscountAction.changed: EventType.invoice_discount_changed,
        InvoiceDiscountAction.removed: EventType.invoice_discount_removed,
        InvoiceDiscountAction.inherited: EventType.invoice_discount_inherited,
    }[action]
    emit_event(
        db,
        event_type,
        {
            "invoice_id": str(invoice.id),
            "revision": revision,
            "action": action.value,
            "source": history.source,
            "source_quote_id": (
                str(history.source_quote_id) if history.source_quote_id else None
            ),
            "discount_type": evidence.discount_type.value,
            "discount_value": str(evidence.value),
            "discount_amount": str(evidence.amount),
            "currency": invoice.currency,
            "total": str(invoice.total),
        },
        actor=str(actor.id),
        account_id=invoice.account_id,
        invoice_id=invoice.id,
    )
    db.flush()
    return history


def _current_resolved(invoice: Invoice) -> ResolvedInvoiceDiscount | None:
    if not invoice.discount_type:
        return None
    return ResolvedInvoiceDiscount(
        discount_type=InvoiceDiscountType(invoice.discount_type),
        value=round_money(invoice.discount_value or 0),
        amount=round_money(invoice.discount_amount or 0),
        reason=(invoice.discount_reason or "").strip() or None,
    )


def _normalized_query(
    query: InvoiceDiscountHistoryQuery,
) -> InvoiceDiscountHistoryQuery:
    if query.page < 1:
        raise _error("page_invalid", "Discount history page must be at least one.")
    if query.page_size not in {10, 25, 50, 100}:
        raise _error("page_size_invalid", "Page size must be 10, 25, 50, or 100.")
    if query.date_from and query.date_to and query.date_from > query.date_to:
        raise _error("date_range_invalid", "From date cannot be after To date.")
    return InvoiceDiscountHistoryQuery(
        date_from=query.date_from,
        date_to=query.date_to,
        customer=" ".join((query.customer or "").split()) or None,
        salesperson_id=query.salesperson_id,
        discount_type=query.discount_type,
        invoice_status=query.invoice_status,
        source=query.source,
        page=query.page,
        page_size=query.page_size,
    )


def list_invoice_discount_history(
    db: Session, query: InvoiceDiscountHistoryQuery
) -> InvoiceDiscountHistoryResult:
    """Return the filtered, paginated append-only Invoice discount projection."""

    query = _normalized_query(query)
    filters = []
    if query.date_from:
        start = datetime.combine(query.date_from, time.min, tzinfo=APP_TIMEZONE)
        filters.append(InvoiceDiscountHistory.applied_at >= start.astimezone(UTC))
    if query.date_to:
        end = datetime.combine(
            query.date_to + timedelta(days=1), time.min, tzinfo=APP_TIMEZONE
        )
        filters.append(InvoiceDiscountHistory.applied_at < end.astimezone(UTC))
    if query.customer:
        filters.append(
            or_(
                Party.display_name.icontains(query.customer, autoescape=True),
                Subscriber.account_number.icontains(query.customer, autoescape=True),
            )
        )
    if query.salesperson_id:
        filters.append(
            InvoiceDiscountHistory.actor_system_user_id == query.salesperson_id
        )
    if query.discount_type:
        filters.append(
            InvoiceDiscountHistory.discount_type == query.discount_type.value
        )
    if query.invoice_status:
        filters.append(Invoice.status == query.invoice_status)
    if query.source:
        filters.append(InvoiceDiscountHistory.source == query.source.value)

    joins = (
        InvoiceDiscountHistory.__table__.join(
            Invoice.__table__, Invoice.id == InvoiceDiscountHistory.invoice_id
        )
        .join(Subscriber.__table__, Subscriber.id == Invoice.account_id)
        .outerjoin(Party.__table__, Party.id == Subscriber.party_id)
        .join(
            SystemUser.__table__,
            SystemUser.id == InvoiceDiscountHistory.actor_system_user_id,
        )
    )
    count_stmt = select(func.count(InvoiceDiscountHistory.id)).select_from(joins)
    if filters:
        count_stmt = count_stmt.where(*filters)
    total_count = int(db.scalar(count_stmt) or 0)
    stmt = (
        select(
            InvoiceDiscountHistory,
            Invoice.invoice_number,
            Invoice.currency,
            Invoice.status,
            Party.display_name,
            Subscriber.account_number,
            SystemUser.display_name,
            SystemUser.first_name,
            SystemUser.last_name,
        )
        .select_from(joins)
        .order_by(
            InvoiceDiscountHistory.applied_at.desc(),
            InvoiceDiscountHistory.id.desc(),
        )
        .offset((query.page - 1) * query.page_size)
        .limit(query.page_size)
    )
    if filters:
        stmt = stmt.where(*filters)
    items = []
    for row in db.execute(stmt).all():
        (
            history,
            invoice_number,
            currency,
            status,
            customer_name,
            account_number,
            actor_display_name,
            actor_first_name,
            actor_last_name,
        ) = row
        actor_name = actor_display_name or (
            f"{actor_first_name or ''} {actor_last_name or ''}".strip()
        )
        items.append(
            InvoiceDiscountHistoryItem(
                history_id=history.id,
                invoice_id=history.invoice_id,
                invoice_number=invoice_number,
                revision=history.revision,
                customer_name=customer_name or account_number or "Unknown customer",
                currency=currency,
                original_subtotal=history.original_subtotal,
                discount_type=InvoiceDiscountType(history.discount_type),
                discount_value=history.discount_value,
                discount_amount=history.discount_amount,
                discounted_subtotal=history.discounted_subtotal,
                total_after_discount=history.total_after_discount,
                reason=history.reason,
                actor_system_user_id=history.actor_system_user_id,
                actor_name=actor_name or "Unavailable staff user",
                applied_at=history.applied_at,
                action=InvoiceDiscountAction(history.action),
                source=InvoiceDiscountSource(history.source),
                source_quote_id=history.source_quote_id,
                invoice_status=status,
            )
        )
    return InvoiceDiscountHistoryResult(
        items=tuple(items),
        total_count=total_count,
        page=query.page,
        page_size=query.page_size,
    )


def invoice_discount_actor_options(
    db: Session,
) -> tuple[InvoiceDiscountActorOption, ...]:
    rows = db.execute(
        select(
            SystemUser.id,
            SystemUser.display_name,
            SystemUser.first_name,
            SystemUser.last_name,
        )
        .join(
            InvoiceDiscountHistory,
            InvoiceDiscountHistory.actor_system_user_id == SystemUser.id,
        )
        .distinct()
        .order_by(SystemUser.first_name.asc(), SystemUser.last_name.asc())
    ).all()
    return tuple(
        InvoiceDiscountActorOption(
            system_user_id=row.id,
            label=(
                row.display_name
                or f"{row.first_name or ''} {row.last_name or ''}".strip()
                or "Unavailable staff user"
            ),
        )
        for row in rows
    )
