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
from enum import StrEnum
from typing import NoReturn
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import (
    CreditNoteApplication,
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    LedgerEntry,
    PaymentAllocation,
    ServiceEntitlement,
    ServiceEntitlementStatus,
    TaxApplication,
)
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


class PrepaidRecoveryNextAction(StrEnum):
    """Closed operator routing vocabulary for Bill Now eligibility."""

    create_recovery_draft = "create_recovery_draft"
    reconcile_existing_invoice = "reconcile_existing_invoice"
    review_existing_invoice = "review_existing_invoice"
    review_multiple_invoices = "review_multiple_invoices"
    resolve_service_eligibility = "resolve_service_eligibility"


@dataclass(frozen=True, slots=True)
class PrepaidRecoveryDraftEligibility:
    subscription_id: UUID
    eligible: bool
    next_action: PrepaidRecoveryNextAction
    reason: str
    existing_invoice_id: UUID | None = None
    existing_invoice_ids: tuple[UUID, ...] = ()


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


def _unresolved_service_invoices(
    db: Session, subscription_id: UUID, *, lock: bool = False
) -> tuple[Invoice, ...]:
    """Return every unresolved invoice document claiming this prepaid service."""

    statement = (
        select(Invoice)
        .join(InvoiceLine, InvoiceLine.invoice_id == Invoice.id)
        .where(
            InvoiceLine.subscription_id == subscription_id,
            InvoiceLine.is_active.is_(True),
            InvoiceLine.amount > Decimal("0.00"),
            Invoice.is_active.is_(True),
            Invoice.status.in_(_OPEN_INVOICE_STATUSES),
        )
        .order_by(Invoice.created_at, Invoice.id)
    )
    if lock:
        statement = statement.with_for_update(of=Invoice)
    return tuple(db.scalars(statement).unique().all())


def _invoice_has_ambiguous_evidence(db: Session, invoice: Invoice) -> bool:
    if invoice.status is not InvoiceStatus.draft:
        return True
    if (
        db.scalar(
            select(PaymentAllocation.id)
            .where(
                PaymentAllocation.invoice_id == invoice.id,
                PaymentAllocation.is_active.is_(True),
            )
            .limit(1)
        )
        is not None
    ):
        return True
    if (
        db.scalar(
            select(LedgerEntry.id).where(LedgerEntry.invoice_id == invoice.id).limit(1)
        )
        is not None
    ):
        return True
    if (
        db.scalar(
            select(CreditNoteApplication.id)
            .where(CreditNoteApplication.invoice_id == invoice.id)
            .limit(1)
        )
        is not None
    ):
        return True
    return (
        db.scalar(
            select(ServiceEntitlement.id)
            .join(
                InvoiceLine,
                InvoiceLine.subscription_id == ServiceEntitlement.subscription_id,
            )
            .where(
                InvoiceLine.invoice_id == invoice.id,
                InvoiceLine.is_active.is_(True),
                ServiceEntitlement.status == ServiceEntitlementStatus.active,
                ServiceEntitlement.starts_at < invoice.billing_period_end,
                ServiceEntitlement.ends_at > invoice.billing_period_start,
            )
            .limit(1)
        )
        is not None
        if invoice.billing_period_start is not None
        and invoice.billing_period_end is not None
        else True
    )


def resolve_prepaid_recovery_draft_eligibility(
    db: Session, *, subscription_id: UUID
) -> PrepaidRecoveryDraftEligibility:
    """Read the canonical Bill Now eligibility and authoritative next action."""

    subscription = db.get(Subscription, subscription_id)
    if subscription is None:
        _error("subscription_not_found", "Subscription was not found.")
    try:
        _validate_recovery_subscription(db, subscription)
    except PrepaidRecoveryBillingError as exc:
        return PrepaidRecoveryDraftEligibility(
            subscription_id=subscription_id,
            eligible=False,
            next_action=PrepaidRecoveryNextAction.resolve_service_eligibility,
            reason=exc.message,
        )
    invoices = _unresolved_service_invoices(db, subscription_id)
    if not invoices:
        return PrepaidRecoveryDraftEligibility(
            subscription_id=subscription_id,
            eligible=True,
            next_action=PrepaidRecoveryNextAction.create_recovery_draft,
            reason="No unresolved invoice claims this prepaid service.",
        )
    invoice_ids = tuple(invoice.id for invoice in invoices)
    if len(invoices) > 1:
        return PrepaidRecoveryDraftEligibility(
            subscription_id=subscription_id,
            eligible=False,
            next_action=PrepaidRecoveryNextAction.review_multiple_invoices,
            reason="Multiple unresolved invoices claim this prepaid service.",
            existing_invoice_id=invoices[0].id,
            existing_invoice_ids=invoice_ids,
        )
    invoice = invoices[0]
    ambiguous = _invoice_has_ambiguous_evidence(db, invoice)
    return PrepaidRecoveryDraftEligibility(
        subscription_id=subscription_id,
        eligible=False,
        next_action=(
            PrepaidRecoveryNextAction.review_existing_invoice
            if ambiguous
            else PrepaidRecoveryNextAction.reconcile_existing_invoice
        ),
        reason=(
            "The existing service invoice has financial or coverage evidence and requires review."
            if ambiguous
            else "Reconcile or explicitly close the existing prepaid draft before Bill Now."
        ),
        existing_invoice_id=invoice.id,
        existing_invoice_ids=invoice_ids,
    )


def _reject_ineligible_recovery(eligibility: PrepaidRecoveryDraftEligibility) -> None:
    if eligibility.eligible:
        return
    details: dict[str, object] = {
        "subscription_id": str(eligibility.subscription_id),
        "next_action": eligibility.next_action.value,
    }
    if eligibility.existing_invoice_id is not None:
        details["invoice_id"] = str(eligibility.existing_invoice_id)
    if eligibility.existing_invoice_ids:
        details["invoice_ids"] = tuple(
            str(invoice_id) for invoice_id in eligibility.existing_invoice_ids
        )
    _error("unresolved_service_invoice", eligibility.reason, **details)


def preview_prepaid_recovery_draft(
    db: Session, *, subscription_id: UUID, effective_at: datetime | None = None
) -> PrepaidRecoveryDraftPreview:
    subscription = db.get(Subscription, subscription_id)
    if subscription is None:
        _error("subscription_not_found", "Subscription was not found.")
    _validate_recovery_subscription(db, subscription)
    _reject_ineligible_recovery(
        resolve_prepaid_recovery_draft_eligibility(db, subscription_id=subscription.id)
    )
    return _build_prepaid_recovery_draft_preview(
        db, subscription=subscription, effective_at=effective_at
    )


def _build_prepaid_recovery_draft_preview(
    db: Session,
    *,
    subscription: Subscription,
    effective_at: datetime | None,
) -> PrepaidRecoveryDraftPreview:
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


def _replayed_prepaid_recovery_draft_preview(
    *,
    invoice: Invoice,
    subscription: Subscription,
    fingerprint: str,
) -> PrepaidRecoveryDraftPreview:
    if invoice.billing_period_start is None or invoice.billing_period_end is None:
        _error(
            "unresolved_service_invoice",
            "The matching recovery invoice has incomplete period evidence.",
            subscription_id=str(subscription.id),
            invoice_id=str(invoice.id),
            next_action=PrepaidRecoveryNextAction.review_existing_invoice.value,
        )
    return PrepaidRecoveryDraftPreview(
        subscription_id=subscription.id,
        account_id=invoice.account_id,
        starts_at=_utc(invoice.billing_period_start),
        ends_at=_utc(invoice.billing_period_end),
        subtotal=round_money(Decimal(str(invoice.subtotal))),
        tax_total=round_money(Decimal(str(invoice.tax_total))),
        total=round_money(Decimal(str(invoice.total))),
        currency=(invoice.currency or "NGN").upper(),
        fingerprint=fingerprint,
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
        invoices = _unresolved_service_invoices(db, subscription.id, lock=True)
        matching_recovery = next(
            (
                invoice
                for invoice in invoices
                if dict(invoice.metadata_ or {}).get("prepaid_recovery_fingerprint")
                == confirmation.fingerprint
            ),
            None,
        )
        if invoices and matching_recovery is None:
            eligibility = resolve_prepaid_recovery_draft_eligibility(
                db, subscription_id=subscription.id
            )
            _reject_ineligible_recovery(eligibility)
        if matching_recovery is not None:
            return PrepaidRecoveryDraftResult(
                matching_recovery.id,
                matching_recovery.invoice_number,
                _replayed_prepaid_recovery_draft_preview(
                    invoice=matching_recovery,
                    subscription=subscription,
                    fingerprint=confirmation.fingerprint,
                ),
                True,
            )
        current = _build_prepaid_recovery_draft_preview(
            db, subscription=subscription, effective_at=confirmation.starts_at
        )
        if current.fingerprint != confirmation.fingerprint:
            _error(
                "stale_preview",
                "The service or price changed after preview; preview again.",
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
