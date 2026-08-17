"""Shared invoice collectibility predicates and balance helpers."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.models.billing import Invoice, InvoiceDueDateBasis, InvoiceStatus
from app.services.common import coerce_uuid

OPEN_INVOICE_STATUSES = (
    InvoiceStatus.issued,
    InvoiceStatus.partially_paid,
    InvoiceStatus.overdue,
)
DUE_INVOICE_STATUSES = OPEN_INVOICE_STATUSES
OVERDUE_DEBT_STATUSES = (
    InvoiceStatus.issued,
    InvoiceStatus.partially_paid,
    InvoiceStatus.overdue,
)


def collection_due_date_eligible_filter():
    """Exclude explicit unknown provenance while legacy classification shrinks."""

    return or_(
        Invoice.due_date_basis.is_(None),
        Invoice.due_date_basis != InvoiceDueDateBasis.unknown_unverified,
    )


def is_collection_due_date_eligible(invoice: Invoice) -> bool:
    """Python equivalent of :func:`collection_due_date_eligible_filter`."""

    return invoice.due_date_basis != InvoiceDueDateBasis.unknown_unverified


def open_invoice_filters(account_id=None) -> tuple:
    """SQL predicates for active invoices with customer-facing debt."""
    filters = [
        Invoice.is_active.is_(True),
        Invoice.status.in_(OPEN_INVOICE_STATUSES),
        Invoice.balance_due > Decimal("0.00"),
    ]
    if account_id is not None:
        filters.insert(0, Invoice.account_id == coerce_uuid(str(account_id)))
    return tuple(filters)


def open_invoice_filters_for_accounts(account_ids) -> tuple:
    """SQL predicates for active customer-facing debt across accounts."""
    return (
        Invoice.account_id.in_(_coerce_account_ids(account_ids)),
        *open_invoice_filters(),
    )


def due_invoice_filters(account_id=None, *, now: datetime | None = None) -> tuple:
    """SQL predicates for open invoices that are due for collection now."""
    now = _as_aware(now)
    return (
        *open_invoice_filters(account_id),
        collection_due_date_eligible_filter(),
        or_(
            Invoice.due_at <= now,
            and_(Invoice.due_at.is_(None), Invoice.status == InvoiceStatus.overdue),
        ),
    )


def due_invoice_filters_for_accounts(
    account_ids, *, now: datetime | None = None
) -> tuple:
    """SQL predicates for invoices due for collection across accounts."""
    now = _as_aware(now)
    return (
        *open_invoice_filters_for_accounts(account_ids),
        collection_due_date_eligible_filter(),
        or_(
            Invoice.due_at <= now,
            and_(Invoice.due_at.is_(None), Invoice.status == InvoiceStatus.overdue),
        ),
    )


def overdue_debt_filters(account_id=None, *, now: datetime | None = None) -> tuple:
    """SQL predicates for debt that is overdue or should be treated as overdue."""
    now = _as_aware(now)
    filters = [
        Invoice.is_active.is_(True),
        Invoice.status.in_(OVERDUE_DEBT_STATUSES),
        Invoice.balance_due > Decimal("0.00"),
        collection_due_date_eligible_filter(),
        or_(
            Invoice.status == InvoiceStatus.overdue,
            Invoice.due_at < now,
        ),
    ]
    if account_id is not None:
        filters.insert(0, Invoice.account_id == coerce_uuid(str(account_id)))
    return tuple(filters)


def overdue_debt_filters_for_accounts(
    account_ids, *, now: datetime | None = None
) -> tuple:
    """SQL predicates for overdue debt across accounts."""
    now = _as_aware(now)
    return (
        Invoice.account_id.in_(_coerce_account_ids(account_ids)),
        Invoice.is_active.is_(True),
        Invoice.status.in_(OVERDUE_DEBT_STATUSES),
        Invoice.balance_due > Decimal("0.00"),
        collection_due_date_eligible_filter(),
        or_(
            Invoice.status == InvoiceStatus.overdue,
            Invoice.due_at < now,
        ),
    )


def overdue_status_filters(account_id=None) -> tuple:
    """SQL predicates for invoices already marked overdue."""
    filters = [
        Invoice.is_active.is_(True),
        Invoice.status == InvoiceStatus.overdue,
        Invoice.balance_due > Decimal("0.00"),
        collection_due_date_eligible_filter(),
    ]
    if account_id is not None:
        filters.insert(0, Invoice.account_id == coerce_uuid(str(account_id)))
    return tuple(filters)


def overdue_status_filters_for_accounts(account_ids) -> tuple:
    """SQL predicates for invoices already marked overdue across accounts."""
    return (
        Invoice.account_id.in_(_coerce_account_ids(account_ids)),
        *overdue_status_filters(),
    )


def list_open_invoices(
    db: Session, account_id, *, due_only: bool = False
) -> list[Invoice]:
    """Return open invoices for an account in deterministic collection order."""
    filters = (
        due_invoice_filters(account_id)
        if due_only
        else open_invoice_filters(account_id)
    )
    return list(
        db.scalars(
            select(Invoice)
            .where(*filters)
            .order_by(Invoice.due_at.asc().nullslast(), Invoice.created_at.asc())
        ).all()
    )


def invoice_balance_sum(db: Session, filters: Iterable) -> Decimal:
    total = db.execute(
        select(func.coalesce(func.sum(Invoice.balance_due), Decimal("0.00"))).where(
            *tuple(filters)
        )
    ).scalar()
    return Decimal(str(total or "0.00"))


def invoice_balance_sum_by_currency(
    db: Session, filters: Iterable
) -> list[tuple[str | None, Decimal]]:
    return [
        (currency, Decimal(str(total or "0.00")))
        for currency, total in db.execute(
            select(
                Invoice.currency,
                func.coalesce(func.sum(Invoice.balance_due), Decimal("0.00")),
            )
            .where(*tuple(filters))
            .group_by(Invoice.currency)
            .order_by(Invoice.currency.asc())
        ).all()
    ]


def open_invoice_balance(db: Session, account_id) -> Decimal:
    return invoice_balance_sum(db, open_invoice_filters(account_id))


def open_invoice_balance_for_accounts(db: Session, account_ids) -> Decimal:
    return invoice_balance_sum(db, open_invoice_filters_for_accounts(account_ids))


def due_invoice_balance(
    db: Session, account_id, *, now: datetime | None = None
) -> Decimal:
    return invoice_balance_sum(db, due_invoice_filters(account_id, now=now))


def overdue_debt_balance(
    db: Session, account_id, *, now: datetime | None = None
) -> Decimal:
    return invoice_balance_sum(db, overdue_debt_filters(account_id, now=now))


def overdue_debt_balance_for_accounts(
    db: Session, account_ids, *, now: datetime | None = None
) -> Decimal:
    return invoice_balance_sum(
        db, overdue_debt_filters_for_accounts(account_ids, now=now)
    )


def accounts_with_due_debt(db: Session, account_ids, *, now: datetime | None = None):
    """Which of these accounts carry a due balance right now.

    A set-shaped answer for cohort callers — audience segmentation asks about
    thousands of accounts at once, and the per-account reads above issue a
    query each. It reuses ``due_invoice_filters_for_accounts`` rather than
    restating the rule, so a cohort answer can never disagree with what the
    customer sees on their own invoice page.
    """
    ids = _coerce_account_ids(account_ids)
    if not ids:
        return set()
    rows = db.execute(
        select(Invoice.account_id)
        .where(*due_invoice_filters_for_accounts(ids, now=now))
        .group_by(Invoice.account_id)
        .having(func.coalesce(func.sum(Invoice.balance_due), Decimal("0.00")) > 0)
    ).all()
    return {row[0] for row in rows}


def accounts_with_overdue_debt(
    db: Session, account_ids, *, now: datetime | None = None
):
    """Which of these accounts carry overdue debt right now.

    Same contract as ``accounts_with_due_debt``, over
    ``overdue_debt_filters_for_accounts``.
    """
    ids = _coerce_account_ids(account_ids)
    if not ids:
        return set()
    rows = db.execute(
        select(Invoice.account_id)
        .where(*overdue_debt_filters_for_accounts(ids, now=now))
        .group_by(Invoice.account_id)
        .having(func.coalesce(func.sum(Invoice.balance_due), Decimal("0.00")) > 0)
    ).all()
    return {row[0] for row in rows}


def overdue_status_count(db: Session, account_id) -> int:
    return int(
        db.execute(
            select(func.count(Invoice.id)).where(*overdue_status_filters(account_id))
        ).scalar()
        or 0
    )


def collection_blocking_balance(db: Session, account_id) -> Decimal:
    """Balance that blocks collection-sensitive self-service actions.

    This preserves the legacy arrangement/plan-change rule: only invoices
    already marked ``overdue`` and still classified as collectible AR block the
    action. Broader past-due debt remains available via ``overdue_debt_balance``.
    """
    from app.services.invoice_classification import (
        collectible_ar_invoice_filter,
    )

    return invoice_balance_sum(
        db,
        (
            Invoice.account_id == coerce_uuid(str(account_id)),
            Invoice.status == InvoiceStatus.overdue,
            Invoice.is_active.is_(True),
            collectible_ar_invoice_filter(),
        ),
    )


def overdue_status_count_for_accounts(db: Session, account_ids) -> int:
    return int(
        db.execute(
            select(func.count(Invoice.id)).where(
                *overdue_status_filters_for_accounts(account_ids)
            )
        ).scalar()
        or 0
    )


def _coerce_account_ids(account_ids) -> tuple:
    return tuple(
        account_id
        for account_id in (coerce_uuid(str(value)) for value in account_ids or [])
        if account_id is not None
    )


def _as_aware(value: datetime | None) -> datetime:
    value = value or datetime.now(UTC)
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value
