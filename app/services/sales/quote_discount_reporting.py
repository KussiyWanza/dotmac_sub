"""Typed read owner for the append-only Quote discount history page."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.party import Party
from app.models.sales import (
    Lead,
    Quote,
    QuoteDiscountAction,
    QuoteDiscountHistory,
    QuoteDiscountType,
    QuoteStatus,
)
from app.models.system_user import SystemUser
from app.services.domain_errors import DomainError
from app.timezone import APP_TIMEZONE


class QuoteDiscountReportingError(DomainError):
    """Stable failure raised for an invalid discount-history query."""


@dataclass(frozen=True, slots=True)
class QuoteDiscountHistoryQuery:
    date_from: date | None = None
    date_to: date | None = None
    customer: str | None = None
    salesperson_id: UUID | None = None
    discount_type: QuoteDiscountType | None = None
    quote_status: QuoteStatus | None = None
    page: int = 1
    page_size: int = 25


@dataclass(frozen=True, slots=True)
class QuoteDiscountHistoryItem:
    history_id: UUID
    quote_id: UUID
    revision: int
    customer_name: str
    currency: str
    original_subtotal: Decimal
    discount_type: QuoteDiscountType
    discount_value: Decimal
    discount_amount: Decimal
    discounted_subtotal: Decimal
    total_after_discount: Decimal
    reason: str | None
    actor_system_user_id: UUID
    actor_name: str
    applied_at: datetime
    action: QuoteDiscountAction
    quote_status: QuoteStatus


@dataclass(frozen=True, slots=True)
class QuoteDiscountHistoryResult:
    items: tuple[QuoteDiscountHistoryItem, ...]
    total_count: int
    page: int
    page_size: int


@dataclass(frozen=True, slots=True)
class QuoteDiscountActorOption:
    system_user_id: UUID
    label: str


def _error(suffix: str, message: str) -> QuoteDiscountReportingError:
    return QuoteDiscountReportingError(
        code=f"sales.quote_discount_reporting.{suffix}", message=message
    )


def _normalized(query: QuoteDiscountHistoryQuery) -> QuoteDiscountHistoryQuery:
    if query.page < 1:
        raise _error("page_invalid", "Discount history page must be at least one.")
    if query.page_size not in {10, 25, 50, 100}:
        raise _error(
            "page_size_invalid",
            "Discount history page size must be 10, 25, 50, or 100.",
        )
    if query.date_from and query.date_to and query.date_from > query.date_to:
        raise _error("date_range_invalid", "From date cannot be after To date.")
    customer = " ".join((query.customer or "").split()) or None
    return QuoteDiscountHistoryQuery(
        date_from=query.date_from,
        date_to=query.date_to,
        customer=customer,
        salesperson_id=query.salesperson_id,
        discount_type=query.discount_type,
        quote_status=query.quote_status,
        page=query.page,
        page_size=query.page_size,
    )


def list_quote_discount_history(
    db: Session, query: QuoteDiscountHistoryQuery
) -> QuoteDiscountHistoryResult:
    """Return one filtered, paginated history projection without side effects."""

    normalized = _normalized(query)
    filters = []
    if normalized.date_from is not None:
        local_start = datetime.combine(
            normalized.date_from, time.min, tzinfo=APP_TIMEZONE
        )
        filters.append(QuoteDiscountHistory.applied_at >= local_start.astimezone(UTC))
    if normalized.date_to is not None:
        local_end = datetime.combine(
            normalized.date_to + timedelta(days=1),
            time.min,
            tzinfo=APP_TIMEZONE,
        )
        filters.append(QuoteDiscountHistory.applied_at < local_end.astimezone(UTC))
    if normalized.customer:
        filters.append(
            or_(
                Party.display_name.icontains(normalized.customer, autoescape=True),
                Lead.title.icontains(normalized.customer, autoescape=True),
            )
        )
    if normalized.salesperson_id is not None:
        filters.append(
            QuoteDiscountHistory.actor_system_user_id == normalized.salesperson_id
        )
    if normalized.discount_type is not None:
        filters.append(
            QuoteDiscountHistory.discount_type == normalized.discount_type.value
        )
    if normalized.quote_status is not None:
        filters.append(Quote.status == normalized.quote_status.value)

    joins = (
        QuoteDiscountHistory.__table__.join(
            Quote.__table__, Quote.id == QuoteDiscountHistory.quote_id
        )
        .outerjoin(Lead.__table__, Lead.id == Quote.lead_id)
        .outerjoin(Party.__table__, Party.id == Lead.party_id)
        .join(
            SystemUser.__table__,
            SystemUser.id == QuoteDiscountHistory.actor_system_user_id,
        )
    )
    count_stmt = select(func.count(QuoteDiscountHistory.id)).select_from(joins)
    if filters:
        count_stmt = count_stmt.where(*filters)
    total_count = int(db.scalar(count_stmt) or 0)

    stmt = (
        select(
            QuoteDiscountHistory,
            Quote.currency,
            Quote.status,
            Party.display_name,
            Lead.title,
            SystemUser.display_name,
            SystemUser.first_name,
            SystemUser.last_name,
        )
        .select_from(joins)
        .order_by(
            QuoteDiscountHistory.applied_at.desc(),
            QuoteDiscountHistory.id.desc(),
        )
        .offset((normalized.page - 1) * normalized.page_size)
        .limit(normalized.page_size)
    )
    if filters:
        stmt = stmt.where(*filters)

    items = []
    for (
        history,
        currency,
        status,
        party_name,
        lead_title,
        actor_display_name,
        actor_first_name,
        actor_last_name,
    ) in db.execute(stmt).all():
        actor_name = actor_display_name or (
            f"{actor_first_name or ''} {actor_last_name or ''}".strip()
        )
        items.append(
            QuoteDiscountHistoryItem(
                history_id=history.id,
                quote_id=history.quote_id,
                revision=history.revision,
                customer_name=party_name or lead_title or "Unknown customer",
                currency=currency,
                original_subtotal=history.original_subtotal,
                discount_type=QuoteDiscountType(history.discount_type),
                discount_value=history.discount_value,
                discount_amount=history.discount_amount,
                discounted_subtotal=history.discounted_subtotal,
                total_after_discount=history.total_after_discount,
                reason=history.reason,
                actor_system_user_id=history.actor_system_user_id,
                actor_name=actor_name or "Unavailable staff user",
                applied_at=history.applied_at,
                action=QuoteDiscountAction(history.action),
                quote_status=QuoteStatus(status),
            )
        )
    return QuoteDiscountHistoryResult(
        items=tuple(items),
        total_count=total_count,
        page=normalized.page,
        page_size=normalized.page_size,
    )


def quote_discount_actor_options(
    db: Session,
) -> tuple[QuoteDiscountActorOption, ...]:
    rows = db.execute(
        select(
            SystemUser.id,
            SystemUser.display_name,
            SystemUser.first_name,
            SystemUser.last_name,
        )
        .join(
            QuoteDiscountHistory,
            QuoteDiscountHistory.actor_system_user_id == SystemUser.id,
        )
        .distinct()
        .order_by(SystemUser.first_name.asc(), SystemUser.last_name.asc())
    ).all()
    return tuple(
        QuoteDiscountActorOption(
            system_user_id=row.id,
            label=(
                row.display_name
                or f"{row.first_name or ''} {row.last_name or ''}".strip()
                or "Unavailable staff user"
            ),
        )
        for row in rows
    )
