"""Canonical commands for staged financial and subscription imports."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, cast

from sqlalchemy.orm import Session

from app.models.billing import (
    Invoice,
    InvoiceDueDateBasis,
    InvoiceStatus,
    Payment,
    PaymentStatus,
)
from app.models.catalog import BillingMode, Subscription
from app.models.subscriber import Subscriber
from app.schemas.billing import InvoiceCreate, PaymentAllocationApply, PaymentCreate
from app.schemas.catalog import SubscriptionCreate
from app.services.billing.invoices import Invoices
from app.services.billing.payments import Payments
from app.services.catalog.subscriptions import Subscriptions
from app.services.notification_suppression import suppress_notifications

FINANCIAL_IMPORT_MODULES = frozenset({"subscriptions", "invoices", "payments"})


@dataclass(frozen=True)
class FinancialImportPersistence:
    record: Invoice | Payment | Subscription
    created_new: bool


@contextmanager
def _owner_session(db: Session) -> Iterator[Session]:
    """Let commit-owning legacy services operate inside the caller's savepoint."""
    owner_db = Session(
        bind=db.connection(),
        autoflush=False,
        autocommit=False,
        join_transaction_mode="create_savepoint",
    )
    try:
        yield owner_db
        owner_db.commit()
    except Exception:
        owner_db.rollback()
        raise
    finally:
        owner_db.close()


def _assert_same(label: str, actual: Any, expected: Any) -> None:
    if actual != expected:
        raise ValueError(
            f"Import key already belongs to a different {label}: "
            f"stored={actual!s}, incoming={expected!s}"
        )


def _lock_account_import(db: Session, account_id: Any) -> None:
    """Serialize natural-key checks for imports affecting one subscriber."""
    exists = (
        db.query(Subscriber.id)
        .filter(Subscriber.id == account_id)
        .with_for_update()
        .one_or_none()
    )
    if exists is None:
        raise ValueError(f"Subscriber {account_id} was not found")


def _import_invoice(db: Session, row: Any, source_name: str) -> tuple[Invoice, bool]:
    _lock_account_import(db, row.account_id)
    if not row.invoice_number:
        raise ValueError("invoice_number is required for idempotent invoice imports")
    if row.status not in {
        InvoiceStatus.draft,
        InvoiceStatus.issued,
        InvoiceStatus.overdue,
    }:
        raise ValueError(
            "Import invoices as draft/issued/overdue and import their payments; "
            "paid, void, and written-off states must be derived by domain commands"
        )
    if row.status != InvoiceStatus.draft and row.balance_due != row.total:
        raise ValueError(
            "Open imported invoices must start with balance_due equal to total; "
            "payments derive the remaining balance"
        )

    existing = (
        db.query(Invoice)
        .filter(Invoice.account_id == row.account_id)
        .filter(Invoice.invoice_number == row.invoice_number)
        .filter(Invoice.is_active.is_(True))
        .one_or_none()
    )
    if existing is not None:
        _assert_same("invoice total", existing.total, row.total)
        _assert_same("invoice currency", existing.currency, row.currency)
        return existing, False

    invoice = Invoices.create_imported_observation(
        db,
        InvoiceCreate(
            account_id=row.account_id,
            invoice_number=row.invoice_number,
            status=row.status,
            currency=row.currency,
            subtotal=row.subtotal,
            tax_total=row.tax_total,
            total=row.total,
            balance_due=row.balance_due,
            issued_at=row.issued_at,
            due_at=row.due_at,
            due_date_basis=(
                None
                if row.status == InvoiceStatus.draft
                else InvoiceDueDateBasis.unknown_unverified
            ),
            paid_at=row.paid_at,
            memo=row.memo,
        ),
    )
    invoice.metadata_ = {
        **(invoice.metadata_ or {}),
        "imported_via": "system_import_run",
        "source_name": source_name,
        **(
            {"billing_mode": row.billing_mode.value}
            if row.billing_mode is not None
            else {}
        ),
    }
    db.commit()
    db.refresh(invoice)
    return invoice, True


def _import_payment(db: Session, row: Any) -> tuple[Payment, bool]:
    _lock_account_import(db, row.account_id)
    external_id = (row.external_id or "").strip()
    if not external_id:
        raise ValueError("external_id is required for idempotent payment imports")
    if row.status != PaymentStatus.succeeded:
        raise ValueError(
            "Only succeeded payments are financial postings; import provider "
            "attempt history through provider events instead"
        )

    existing = (
        db.query(Payment)
        .filter(Payment.account_id == row.account_id)
        .filter(Payment.external_id == external_id)
        .filter(Payment.is_active.is_(True))
        .one_or_none()
    )
    if existing is not None:
        _assert_same("payment amount", existing.amount, row.amount)
        _assert_same("payment currency", existing.currency, row.currency)
        _assert_same("payment status", existing.status, PaymentStatus.succeeded)
        return existing, False

    allocations = None
    if row.invoice_number:
        invoice = (
            db.query(Invoice)
            .filter(Invoice.account_id == row.account_id)
            .filter(Invoice.invoice_number == row.invoice_number)
            .filter(Invoice.is_active.is_(True))
            .one_or_none()
        )
        if invoice is None:
            raise ValueError(
                f"Invoice {row.invoice_number} was not found for payment allocation"
            )
        allocations = [
            PaymentAllocationApply(
                invoice_id=invoice.id,
                amount=min(Decimal(row.amount), Decimal(invoice.balance_due)),
                memo=f"Imported payment {external_id}",
            )
        ]

    return (
        Payments.create(
            db,
            PaymentCreate(
                account_id=row.account_id,
                amount=row.amount,
                currency=row.currency,
                status=row.status,
                memo=row.memo,
                external_id=external_id,
                allocations=allocations,
            ),
            auto_allocate=allocations is None,
        ),
        True,
    )


def _import_subscription(db: Session, row: Any) -> tuple[Subscription, bool]:
    _lock_account_import(db, row.subscriber_id)
    existing = (
        db.query(Subscription)
        .filter(Subscription.subscriber_id == row.subscriber_id)
        .filter(Subscription.offer_id == row.offer_id)
        .filter(Subscription.start_at == row.start_at)
        .order_by(Subscription.created_at.asc())
        .first()
    )
    if existing is not None:
        _assert_same(
            "subscription billing mode",
            existing.billing_mode,
            row.billing_mode or BillingMode.prepaid,
        )
        return existing, False

    return (
        Subscriptions.create(
            db,
            SubscriptionCreate(
                subscriber_id=row.subscriber_id,
                offer_id=row.offer_id,
                status=row.status,
                billing_mode=row.billing_mode or BillingMode.prepaid,
                start_at=row.start_at,
                end_at=row.end_at,
                next_billing_at=row.next_billing_at,
                canceled_at=row.canceled_at,
                cancel_reason=row.cancel_reason,
            ),
        ),
        True,
    )


def persist_import_row(
    db: Session,
    module: str,
    row: Any,
    *,
    source_name: str,
    with_provenance: bool = False,
) -> Invoice | Payment | Subscription | FinancialImportPersistence:
    """Apply one staged row through its owning service and return the local row."""
    if module not in FINANCIAL_IMPORT_MODULES:
        raise ValueError(f"Unsupported financial import module: {module}")

    model: type[Invoice] | type[Payment] | type[Subscription]
    record: Invoice | Payment | Subscription
    created_new: bool
    record_id: Any
    with _owner_session(db) as owner_db, suppress_notifications():
        if module == "invoices":
            record, created_new = _import_invoice(owner_db, row, source_name)
            model = Invoice
        elif module == "payments":
            record, created_new = _import_payment(owner_db, row)
            model = Payment
        else:
            record, created_new = _import_subscription(owner_db, row)
            model = Subscription
        record_id = record.id

    persisted = cast(Invoice | Payment | Subscription | None, db.get(model, record_id))
    if persisted is None:
        raise RuntimeError(f"Imported {module} row was not visible after posting")
    if with_provenance:
        return FinancialImportPersistence(
            record=persisted,
            created_new=created_new,
        )
    return persisted
