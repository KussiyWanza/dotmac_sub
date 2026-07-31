"""Guarded administrative recovery billing for suspended prepaid services.

This owner deliberately creates a *draft* renewal invoice. It never voids an
existing invoice, spends a generic displayed balance, settles a draft, or
resumes access. All prepaid draft classification and reconciliation belongs to
``financial.prepaid_draft_reconciliation``.
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
from app.models.enforcement_lock import EnforcementLock, EnforcementReason
from app.schemas.billing import InvoiceCreate, SystemInvoiceLineCreate
from app.services.billing._common import lock_account
from app.services.billing.invoices import InvoiceLines, Invoices
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
