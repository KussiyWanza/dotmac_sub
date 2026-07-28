"""Guarded administrative recovery billing for suspended prepaid services.

This owner deliberately creates a *draft* renewal invoice.  It never voids an
existing invoice, spends a generic displayed balance, or resumes access.  The
separate settlement command only spends confirmed unallocated payment credit
when it can settle the exact draft in full; it then derives the entitlement and
asks financial-access resolution to clear only eligible locks.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import NoReturn
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import Invoice, InvoiceLine, InvoiceStatus, TaxApplication
from app.models.catalog import (
    BillingMode,
    Subscription,
    SubscriptionStatus,
    billing_cycle_noun,
)
from app.models.collections import FinancialAccessOrigin
from app.models.enforcement_lock import EnforcementLock, EnforcementReason
from app.schemas.billing import InvoiceCreate, SystemInvoiceLineCreate
from app.services.billing._common import get_account_credit_balance, lock_account
from app.services.billing.invoices import InvoiceLines, Invoices
from app.services.billing.reconcile_unposted import (
    _allocatable_payments,
    settle_single_invoice_from_credit,
)
from app.services.common import round_money
from app.services.domain_errors import DomainError
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.prepaid_service_renewals import resolve_prepaid_monthly_charge

_OWNER = "financial.prepaid_recovery_billing"
_CREATE_DEFINITION = OwnerCommandDefinition(
    owner=_OWNER,
    concern="suspended prepaid replacement-cycle draft creation",
    name="create_prepaid_recovery_draft",
)
_SETTLE_DEFINITION = OwnerCommandDefinition(
    owner=_OWNER,
    concern="full settlement and restoration of a prepaid recovery invoice",
    name="settle_prepaid_recovery_invoice",
)
_OPEN_INVOICE_STATUSES = frozenset(
    {
        InvoiceStatus.draft,
        InvoiceStatus.issued,
        InvoiceStatus.partially_paid,
        InvoiceStatus.overdue,
    }
)


class PrepaidRecoveryBillingError(DomainError):
    """Transport-neutral rejection for recovery billing."""


def _error(suffix: str, message: str, **details: object) -> NoReturn:
    raise PrepaidRecoveryBillingError(
        code=f"{_OWNER}.{suffix}", message=message, details=details
    )


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _fingerprint(*parts: object) -> str:
    payload = "|".join(str(part) for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class PrepaidRecoveryDraftPreview:
    subscription_id: UUID
    account_id: UUID
    starts_at: datetime
    ends_at: datetime
    subtotal: Decimal
    tax_total: Decimal
    total: Decimal
    currency: str
    fingerprint: str


@dataclass(frozen=True, slots=True)
class PrepaidRecoveryDraftResult:
    invoice_id: UUID
    invoice_number: str | None
    preview: PrepaidRecoveryDraftPreview
    replayed: bool


@dataclass(frozen=True, slots=True)
class PrepaidRecoveryDraftConfirmation:
    subscription_id: UUID
    starts_at: datetime
    fingerprint: str


@dataclass(frozen=True, slots=True)
class PrepaidRecoverySettlementPreview:
    invoice_id: UUID
    subscription_id: UUID
    account_id: UUID
    balance_due: Decimal
    payment_backed_credit: Decimal
    can_settle: bool
    fingerprint: str


@dataclass(frozen=True, slots=True)
class PrepaidRecoverySettlementConfirmation:
    invoice_id: UUID
    fingerprint: str


@dataclass(frozen=True, slots=True)
class PrepaidRecoverySettlementResult:
    invoice_id: UUID
    subscription_id: UUID
    amount_applied: Decimal
    restored_subscriptions: int
    replayed: bool


def _locked_subscription(db: Session, subscription_id: UUID) -> Subscription:
    subscription = db.scalar(
        select(Subscription).where(Subscription.id == subscription_id).with_for_update()
    )
    if subscription is None:
        _error("subscription_not_found", "Subscription was not found.")
    return subscription


def _has_prepaid_lock(db: Session, subscription_id: UUID) -> bool:
    return bool(
        db.scalar(
            select(EnforcementLock.id).where(
                EnforcementLock.subscription_id == subscription_id,
                EnforcementLock.reason == EnforcementReason.prepaid,
                EnforcementLock.is_active.is_(True),
            )
        )
    )


def _validate_recovery_subscription(db: Session, subscription: Subscription) -> None:
    if subscription.billing_mode != BillingMode.prepaid:
        _error(
            "ineligible_billing_mode",
            "Bill now is only available for prepaid services.",
        )
    if subscription.status != SubscriptionStatus.suspended:
        _error(
            "ineligible_status", "Bill now is only available for a suspended service."
        )
    if not _has_prepaid_lock(db, subscription.id):
        _error(
            "prepaid_lock_missing",
            "The service is not suspended by a prepaid financial lock.",
        )


def _open_recovery_invoice(db: Session, subscription_id: UUID) -> Invoice | None:
    return db.scalar(
        select(Invoice)
        .join(InvoiceLine, InvoiceLine.invoice_id == Invoice.id)
        .where(
            InvoiceLine.subscription_id == subscription_id,
            InvoiceLine.is_active.is_(True),
            Invoice.is_active.is_(True),
            Invoice.status.in_(_OPEN_INVOICE_STATUSES),
            InvoiceLine.metadata_["kind"].astext == "prepaid_recovery_cycle",
        )
        .order_by(Invoice.created_at.desc())
    )


def preview_prepaid_recovery_draft(
    db: Session, *, subscription_id: UUID, effective_at: datetime | None = None
) -> PrepaidRecoveryDraftPreview:
    subscription = db.get(Subscription, subscription_id)
    if subscription is None:
        _error("subscription_not_found", "Subscription was not found.")
    _validate_recovery_subscription(db, subscription)
    existing = _open_recovery_invoice(db, subscription.id)
    if existing is not None:
        _error(
            "open_recovery_invoice",
            "This service already has an open recovery invoice; settle or void it first.",
            invoice_id=str(existing.id),
        )
    starts_at = _utc(effective_at or datetime.now(UTC))
    charge = resolve_prepaid_monthly_charge(db, subscription, starts_at)
    if charge is None:
        _error(
            "unsupported_cycle",
            "This prepaid service has no supported recurring monthly charge.",
        )
    total, currency, cycle = charge
    from app.services.billing_automation import _period_end

    ends_at = _period_end(starts_at, cycle)
    subtotal = round_money(Decimal(str(subscription.unit_price or "0")))
    tax_total = round_money(total - subtotal)
    if subtotal <= Decimal("0.00") or tax_total < Decimal("0.00"):
        _error(
            "invalid_charge",
            "The service recurring charge could not be resolved safely.",
        )
    return PrepaidRecoveryDraftPreview(
        subscription_id=subscription.id,
        account_id=subscription.subscriber_id,
        starts_at=starts_at,
        ends_at=ends_at,
        subtotal=subtotal,
        tax_total=tax_total,
        total=round_money(total),
        currency=currency,
        fingerprint=_fingerprint(
            subscription.id,
            subscription.updated_at,
            starts_at.isoformat(),
            ends_at.isoformat(),
            subtotal,
            tax_total,
            total,
            currency,
        ),
    )


def create_prepaid_recovery_draft(
    db: Session,
    *,
    context: CommandContext,
    confirmation: PrepaidRecoveryDraftConfirmation,
) -> PrepaidRecoveryDraftResult:
    """Create one replacement-cycle draft after a locked stale-preview check."""

    def operation() -> PrepaidRecoveryDraftResult:
        candidate = db.get(Subscription, confirmation.subscription_id)
        if candidate is None:
            _error("subscription_not_found", "Service was not found.")
        lock_account(db, str(candidate.subscriber_id))
        subscription = _locked_subscription(db, confirmation.subscription_id)
        _validate_recovery_subscription(db, subscription)
        current = preview_prepaid_recovery_draft(
            db, subscription_id=subscription.id, effective_at=confirmation.starts_at
        )
        if current.fingerprint != confirmation.fingerprint:
            _error(
                "stale_preview",
                "The service or price changed after preview; preview again.",
            )
        existing = _open_recovery_invoice(db, subscription.id)
        if existing is not None:
            metadata = dict(existing.metadata_ or {})
            if metadata.get("prepaid_recovery_fingerprint") == confirmation.fingerprint:
                return PrepaidRecoveryDraftResult(
                    existing.id,
                    existing.invoice_number,
                    current,
                    True,
                )
            _error(
                "open_recovery_invoice",
                "This service already has an open recovery invoice; settle or void it first.",
            )
        invoice = Invoices.stage_system_invoice(
            db,
            InvoiceCreate(
                account_id=current.account_id,
                status=InvoiceStatus.draft,
                currency=current.currency,
                subtotal=current.subtotal,
                tax_total=current.tax_total,
                total=current.total,
                balance_due=current.total,
                billing_period_start=current.starts_at,
                billing_period_end=current.ends_at,
                memo="Prepaid service recovery cycle; created by an administrator.",
            ),
            reason="prepaid_recovery_bill_now",
        )
        invoice.metadata_ = {"prepaid_recovery_fingerprint": current.fingerprint}
        InvoiceLines.stage_system_line(
            db,
            SystemInvoiceLineCreate(
                invoice_id=invoice.id,
                subscription_id=subscription.id,
                description=(
                    f"{subscription.offer.name if subscription.offer else 'Service'} — "
                    f"{billing_cycle_noun(subscription.billing_cycle)} recovery cycle"
                ),
                quantity=Decimal("1.000"),
                unit_price=current.subtotal,
                amount=current.subtotal,
                tax_application=TaxApplication.exclusive,
                metadata_={
                    "kind": "prepaid_recovery_cycle",
                    "billing_period_start": current.starts_at.isoformat(),
                    "billing_period_end": current.ends_at.isoformat(),
                    "subscription_id": str(subscription.id),
                    "created_by_command_id": str(context.command_id),
                },
                billing_line_key=f"prepaid-recovery:{subscription.id}:{current.fingerprint}",
            ),
            reason="prepaid_recovery_bill_now",
        )
        return PrepaidRecoveryDraftResult(
            invoice.id, invoice.invoice_number, current, False
        )

    return execute_owner_command(
        db, definition=_CREATE_DEFINITION, context=context, operation=operation
    )


def _recovery_invoice(
    db: Session, invoice_id: UUID, *, lock: bool = False
) -> tuple[Invoice, Subscription]:
    invoice_query = select(Invoice).where(Invoice.id == invoice_id)
    if lock:
        invoice_query = invoice_query.with_for_update()
    invoice = db.scalar(invoice_query)
    if invoice is None or not invoice.is_active:
        _error("invoice_not_found", "Invoice was not found.")
    line = db.scalar(
        select(InvoiceLine).where(
            InvoiceLine.invoice_id == invoice.id,
            InvoiceLine.is_active.is_(True),
            InvoiceLine.metadata_["kind"].astext == "prepaid_recovery_cycle",
        )
    )
    if line is None or line.subscription_id is None:
        _error(
            "not_recovery_invoice", "This invoice is not a prepaid recovery invoice."
        )
    subscription = (
        _locked_subscription(db, line.subscription_id)
        if lock
        else db.get(Subscription, line.subscription_id)
    )
    if subscription is None:
        _error("subscription_not_found", "Subscription was not found.")
    if subscription.subscriber_id != invoice.account_id:
        _error(
            "invoice_scope_mismatch",
            "The invoice does not belong to the service account.",
        )
    return invoice, subscription


def preview_prepaid_recovery_settlement(
    db: Session, *, invoice_id: UUID
) -> PrepaidRecoverySettlementPreview:
    invoice, subscription = _recovery_invoice(db, invoice_id)
    _validate_recovery_subscription(db, subscription)
    if invoice.status == InvoiceStatus.paid and invoice.balance_due <= Decimal("0.00"):
        return PrepaidRecoverySettlementPreview(
            invoice.id,
            subscription.id,
            invoice.account_id,
            Decimal("0.00"),
            Decimal("0.00"),
            True,
            _fingerprint(invoice.id, "paid"),
        )
    if invoice.status != InvoiceStatus.draft:
        _error(
            "invoice_not_draft", "Only the recovery draft can be paid by this action."
        )
    balance_due = round_money(Decimal(str(invoice.balance_due)))
    currency = invoice.currency or "NGN"
    payment_backed = round_money(
        sum(
            (
                room
                for payment, room in _allocatable_payments(db, str(invoice.account_id))
                if (payment.currency or "NGN") == currency
            ),
            Decimal("0.00"),
        )
    )
    payment_backed = min(
        payment_backed,
        max(
            get_account_credit_balance(db, str(invoice.account_id), currency=currency),
            Decimal("0.00"),
        ),
    )
    return PrepaidRecoverySettlementPreview(
        invoice.id,
        subscription.id,
        invoice.account_id,
        balance_due,
        payment_backed,
        payment_backed >= balance_due,
        _fingerprint(
            invoice.id,
            invoice.updated_at,
            subscription.updated_at,
            balance_due,
            payment_backed,
            currency,
        ),
    )


def settle_prepaid_recovery_invoice(
    db: Session,
    *,
    context: CommandContext,
    confirmation: PrepaidRecoverySettlementConfirmation,
) -> PrepaidRecoverySettlementResult:
    """Issue, fully settle, grant coverage, and restore only after exact payment evidence."""

    def operation() -> PrepaidRecoverySettlementResult:
        candidate = db.get(Invoice, confirmation.invoice_id)
        if candidate is None:
            _error("invoice_not_found", "Invoice was not found.")
        lock_account(db, str(candidate.account_id))
        invoice, subscription = _recovery_invoice(
            db, confirmation.invoice_id, lock=True
        )
        if invoice.status == InvoiceStatus.paid and invoice.balance_due <= Decimal(
            "0.00"
        ):
            return PrepaidRecoverySettlementResult(
                invoice.id, subscription.id, Decimal("0.00"), 0, True
            )
        current = preview_prepaid_recovery_settlement(db, invoice_id=invoice.id)
        if current.fingerprint != confirmation.fingerprint:
            _error(
                "stale_preview", "Invoice funding changed after preview; preview again."
            )
        if not current.can_settle:
            _error(
                "insufficient_confirmed_credit",
                "Confirmed payment credit cannot fully settle this invoice.",
            )
        Invoices.issue_draft_system(
            db,
            str(invoice.id),
            issued_at=datetime.now(UTC),
            due_at=datetime.now(UTC),
            reason="prepaid_recovery_invoice_pay_now",
            apply_available_credit=False,
        )
        applied = settle_single_invoice_from_credit(db, invoice, only_if_full=True)
        if invoice.status != InvoiceStatus.paid or invoice.balance_due > Decimal(
            "0.00"
        ):
            _error(
                "settlement_incomplete",
                "The invoice could not be fully settled; no service was restored.",
            )
        from app.services.collections._core import restore_account_services

        restored = restore_account_services(
            db,
            str(invoice.account_id),
            invoice_id=str(invoice.id),
            origin=FinancialAccessOrigin.financial_reconciliation,
            idempotency_key=f"prepaid-recovery-restore:{invoice.id}",
            resolved_by=f"prepaid_recovery_invoice:{invoice.id}",
        )
        return PrepaidRecoverySettlementResult(
            invoice.id, subscription.id, applied, restored, False
        )

    return execute_owner_command(
        db, definition=_SETTLE_DEFINITION, context=context, operation=operation
    )
