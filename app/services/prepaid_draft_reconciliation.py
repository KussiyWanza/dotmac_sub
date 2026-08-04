"""Evidence-first owner for stranded prepaid draft invoices.

Preview is read-only and classifies one exact invoice. Confirmation locks the
account and invoice, recomputes the preview fingerprint, and performs one safe
repair:

* exact native payment-backed funding issues and fully settles the draft; or
* settlement-backed payments plus reviewed opening funding settle the exact
  remainder without representing that opening source as a Payment; or
* an exact direct-renewal debit/entitlement voids the duplicate draft without
  charging the customer again.

Automatic mixed-source discovery creates a durable operator exception.
Insufficient funding, legacy/unbacked credit, multiple drafts, mixed invoices,
and ambiguous coverage otherwise remain unchanged and fail closed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import NoReturn
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.billing import (
    AccountAdjustment,
    CreditNoteApplication,
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    LedgerCategory,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentAllocation,
    PaymentRefund,
    PaymentReversal,
    PaymentSettlement,
    PaymentStatus,
    ServiceEntitlement,
    ServiceEntitlementStatus,
)
from app.models.catalog import BillingMode, Subscription, SubscriptionStatus
from app.models.collections import (
    FinancialAccessAction,
    FinancialAccessConsequence,
    FinancialAccessOrigin,
)
from app.models.idempotency import IdempotencyKey
from app.models.prepaid_funding import (
    PrepaidDraftReconciliationException,
    PrepaidFundingBaseline,
    PrepaidOpeningFundingConsumption,
)
from app.schemas.audit import AuditEventCreate
from app.schemas.billing import LedgerEntryCreate
from app.services.audit import AuditEvents
from app.services.billing._common import lock_account
from app.services.billing.account_credit import (
    AccountCreditApplicationError,
    AccountCreditApplications,
    AccountCreditInvoiceFundingPreview,
)
from app.services.billing.adjustments import AccountAdjustmentOrigin
from app.services.billing.invoices import (
    InvoiceOwnerError,
    Invoices,
    PaidPrepaidInvoiceDocumentRepair,
    PrepaidProformaDocumentAdoption,
)
from app.services.billing.ledger import LedgerEntries
from app.services.billing.payments import finalize_invoice_application_for_owner
from app.services.common import round_money, to_decimal
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.locking import lock_for_update
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.prepaid_funding_reconstruction import (
    PrepaidFundingBaselineMissingError,
    verified_prepaid_funding_balance,
)

_OWNER = "financial.prepaid_draft_reconciliation"
_CONCERN = "stranded prepaid draft invoice reconciliation"
_COMMAND = OwnerCommandDefinition(
    owner=_OWNER,
    concern=_CONCERN,
    name="reconcile_prepaid_draft_invoice",
)
_PROFORMA_ADOPTION_COMMAND = OwnerCommandDefinition(
    owner=_OWNER,
    concern="funded onboarding proforma documentary adoption",
    name="adopt_funded_prepaid_proforma",
)
_PAID_INVOICE_REPAIR_COMMAND = OwnerCommandDefinition(
    owner=_OWNER,
    concern="historical paid prepaid invoice identity and coverage repair",
    name="repair_historical_paid_prepaid_invoice",
)
_IDEMPOTENCY_SCOPE = "prepaid_draft_reconcile"
_PROFORMA_ADOPTION_IDEMPOTENCY_SCOPE = "prepaid_proforma_adoption"
_PAID_INVOICE_REPAIR_IDEMPOTENCY_SCOPE = "paid_prepaid_invoice_repair"
_METADATA_KEY = "prepaid_draft_reconciliation"
_PROFORMA_ADOPTION_METADATA_KEY = "prepaid_proforma_adoption"
_PAID_INVOICE_REPAIR_METADATA_KEY = "paid_prepaid_invoice_repair"
_RENEWAL_ORIGIN = AccountAdjustmentOrigin.prepaid_service_renewal


class PrepaidDraftDisposition(StrEnum):
    exact_payment_fundable = "exact_payment_fundable"
    reviewed_opening_fundable = "reviewed_opening_fundable"
    already_renewed = "already_renewed"
    insufficient_funding = "insufficient_funding"
    legacy_unbacked_funding = "legacy_unbacked_funding"
    manual_review = "manual_review"
    already_reconciled = "already_reconciled"


class PrepaidDraftAction(StrEnum):
    settle_paid = "settle_paid"
    void_duplicate = "void_duplicate"
    none = "none"


class PrepaidProformaAdoptionDisposition(StrEnum):
    exact_funded_onboarding_proforma = "exact_funded_onboarding_proforma"
    manual_review = "manual_review"
    already_adopted = "already_adopted"


class PaidPrepaidInvoiceRepairDisposition(StrEnum):
    exact_paid_unlinked_invoice = "exact_paid_unlinked_invoice"
    manual_review = "manual_review"
    already_repaired = "already_repaired"


class PrepaidDraftReconciliationError(DomainError):
    """Stable fail-closed reconciliation error."""


@dataclass(frozen=True, slots=True)
class PrepaidDraftReconciliationPreview:
    invoice_id: UUID
    account_id: UUID
    invoice_number: str | None
    disposition: PrepaidDraftDisposition
    recommended_action: PrepaidDraftAction
    currency: str
    invoice_total: Decimal
    balance_due: Decimal
    payment_backed_credit: Decimal
    authoritative_funding: Decimal
    opening_funding_available: Decimal
    opening_funding_required: Decimal
    opening_funding_baseline_id: UUID | None
    unbacked_credit: Decimal
    shortfall: Decimal
    subscription_ids: tuple[UUID, ...]
    entitlement_ids: tuple[UUID, ...]
    renewal_adjustment_ids: tuple[UUID, ...]
    reason: str
    fingerprint: str

    @property
    def actionable(self) -> bool:
        return (
            self.disposition is not PrepaidDraftDisposition.already_reconciled
            and self.recommended_action is not PrepaidDraftAction.none
        )


@dataclass(frozen=True, slots=True)
class ReconcilePrepaidDraftCommand:
    context: CommandContext
    invoice_id: UUID
    preview_fingerprint: str
    effective_at: datetime


@dataclass(frozen=True, slots=True)
class PrepaidDraftReconciliationResult:
    invoice_id: UUID
    disposition: PrepaidDraftDisposition
    action: PrepaidDraftAction
    final_status: InvoiceStatus
    applied_amount: Decimal
    payment_applied_amount: Decimal
    opening_funding_applied_amount: Decimal
    opening_funding_consumption_id: UUID | None
    preview_fingerprint: str
    replayed: bool


@dataclass(frozen=True, slots=True)
class FundingChangeDraftResult:
    drafts_found: int
    drafts_settled: int
    drafts_blocked: int
    review_exceptions: int
    invoice_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class ReviewedOpeningFundingPreview:
    baseline_id: UUID | None
    approved_amount: Decimal
    previously_consumed: Decimal
    available_amount: Decimal
    authoritative_funding: Decimal
    approval_evidence_ref: str | None
    approval_actor: str | None


@dataclass(frozen=True, slots=True)
class PrepaidProformaAdoptionQuery:
    invoice_id: UUID
    subscription_id: UUID


@dataclass(frozen=True, slots=True)
class PrepaidProformaAdoptionPreview:
    invoice_id: UUID
    account_id: UUID
    invoice_number: str | None
    subscription_id: UUID
    line_id: UUID | None
    settlement_payment_id: UUID | None
    settlement_effective_at: datetime | None
    billing_period_start: datetime | None
    billing_period_end: datetime | None
    disposition: PrepaidProformaAdoptionDisposition
    currency: str
    invoice_total: Decimal
    payment_backed_credit: Decimal
    reason: str
    fingerprint: str

    @property
    def actionable(self) -> bool:
        return (
            self.disposition
            is PrepaidProformaAdoptionDisposition.exact_funded_onboarding_proforma
        )


@dataclass(frozen=True, slots=True)
class AdoptFundedPrepaidProformaCommand:
    context: CommandContext
    invoice_id: UUID
    subscription_id: UUID
    preview_fingerprint: str


@dataclass(frozen=True, slots=True)
class PrepaidProformaAdoptionResult:
    invoice_id: UUID
    subscription_id: UUID
    line_id: UUID
    settlement_payment_id: UUID
    settlement_effective_at: datetime
    billing_period_start: datetime
    billing_period_end: datetime
    preview_fingerprint: str
    replayed: bool


@dataclass(frozen=True, slots=True)
class PaidPrepaidInvoiceRepairQuery:
    invoice_id: UUID
    subscription_id: UUID


@dataclass(frozen=True, slots=True)
class PaidPrepaidInvoiceRepairPreview:
    invoice_id: UUID
    account_id: UUID
    invoice_number: str | None
    subscription_id: UUID
    line_id: UUID | None
    allocation_id: UUID | None
    settlement_id: UUID | None
    payment_id: UUID | None
    settlement_effective_at: datetime | None
    billing_period_start: datetime | None
    billing_period_end: datetime | None
    disposition: PaidPrepaidInvoiceRepairDisposition
    currency: str
    invoice_total: Decimal
    allocated_amount: Decimal
    reason: str
    fingerprint: str

    @property
    def actionable(self) -> bool:
        return (
            self.disposition
            is PaidPrepaidInvoiceRepairDisposition.exact_paid_unlinked_invoice
        )


@dataclass(frozen=True, slots=True)
class RepairHistoricalPaidPrepaidInvoiceCommand:
    context: CommandContext
    invoice_id: UUID
    subscription_id: UUID
    preview_fingerprint: str


@dataclass(frozen=True, slots=True)
class PaidPrepaidInvoiceRepairResult:
    invoice_id: UUID
    subscription_id: UUID
    line_id: UUID
    allocation_id: UUID
    settlement_id: UUID
    payment_id: UUID
    entitlement_id: UUID
    access_consequence_id: UUID
    billing_period_start: datetime
    billing_period_end: datetime
    preview_fingerprint: str
    subscriptions_restored: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class _PaidPrepaidInvoiceRepairEvidence:
    """Structural evidence produced by one completed paid-invoice repair."""

    line: InvoiceLine
    allocation: PaymentAllocation
    payment: Payment
    settlement: PaymentSettlement
    entitlement: ServiceEntitlement


def _error(suffix: str, message: str, **details: object) -> NoReturn:
    raise PrepaidDraftReconciliationError(
        code=f"{_OWNER}.{suffix}",
        message=message,
        details=details,
    )


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _hash(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=lambda value: (
            value.value
            if isinstance(value, StrEnum)
            else value.isoformat()
            if isinstance(value, datetime)
            else f"{value:.2f}"
            if isinstance(value, Decimal)
            else str(value)
        ),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _active_positive_lines(db: Session, invoice_id: UUID) -> list[InvoiceLine]:
    return list(
        db.scalars(
            select(InvoiceLine)
            .where(
                InvoiceLine.invoice_id == invoice_id,
                InvoiceLine.is_active.is_(True),
                InvoiceLine.amount > Decimal("0.00"),
            )
            .order_by(InvoiceLine.id)
        ).all()
    )


def _funding_preview(
    db: Session,
    invoice: Invoice,
) -> AccountCreditInvoiceFundingPreview:
    baseline = db.scalar(
        select(PrepaidFundingBaseline).where(
            PrepaidFundingBaseline.account_id == invoice.account_id,
            PrepaidFundingBaseline.currency == (invoice.currency or "NGN").upper(),
            PrepaidFundingBaseline.is_active.is_(True),
        )
    )
    return AccountCreditApplications.preview_invoice_funding(
        db,
        invoice,
        funding_position_at=baseline.position_at if baseline is not None else None,
    )


def _reviewed_opening_funding_preview(
    db: Session,
    *,
    invoice: Invoice,
    payment_funding: AccountCreditInvoiceFundingPreview,
) -> ReviewedOpeningFundingPreview:
    currency = (invoice.currency or "NGN").upper()
    baseline = db.scalar(
        select(PrepaidFundingBaseline).where(
            PrepaidFundingBaseline.account_id == invoice.account_id,
            PrepaidFundingBaseline.currency == currency,
            PrepaidFundingBaseline.is_active.is_(True),
        )
    )
    try:
        authoritative = round_money(
            verified_prepaid_funding_balance(
                db,
                invoice.account_id,
                currency=currency,
            )
        )
    except PrepaidFundingBaselineMissingError:
        _error(
            "opening_funding_unavailable",
            "Reviewed opening funding is unavailable for this invoice.",
            invoice_id=str(invoice.id),
            account_id=str(invoice.account_id),
            currency=currency,
        )
    if baseline is None or baseline.amount <= Decimal("0.00"):
        return ReviewedOpeningFundingPreview(
            baseline_id=None,
            approved_amount=Decimal("0.00"),
            previously_consumed=Decimal("0.00"),
            available_amount=Decimal("0.00"),
            authoritative_funding=authoritative,
            approval_evidence_ref=None,
            approval_actor=None,
        )
    consumed = round_money(
        to_decimal(
            db.query(
                func.coalesce(
                    func.sum(PrepaidOpeningFundingConsumption.amount),
                    0,
                )
            )
            .filter(PrepaidOpeningFundingConsumption.baseline_id == baseline.id)
            .scalar()
        )
    )
    source_remaining = max(
        Decimal("0.00"),
        round_money(to_decimal(baseline.amount) - consumed),
    )
    authoritative_nonpayment = max(
        Decimal("0.00"),
        round_money(authoritative - payment_funding.payment_backed_credit),
    )
    # Untyped ledger credit is not opening-funding provenance. Keep it
    # quarantined rather than allowing it to revive an already spent baseline.
    available = (
        Decimal("0.00")
        if payment_funding.unbacked_credit > Decimal("0.00")
        else min(source_remaining, authoritative_nonpayment)
    )
    return ReviewedOpeningFundingPreview(
        baseline_id=baseline.id,
        approved_amount=round_money(to_decimal(baseline.amount)),
        previously_consumed=consumed,
        available_amount=round_money(available),
        authoritative_funding=authoritative,
        approval_evidence_ref=baseline.batch.evidence_ref,
        approval_actor=baseline.batch.approved_by,
    )


def _build_proforma_adoption_preview(
    *,
    invoice: Invoice,
    subscription_id: UUID,
    disposition: PrepaidProformaAdoptionDisposition,
    reason: str,
    line: InvoiceLine | None = None,
    payment: Payment | None = None,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
    funding: AccountCreditInvoiceFundingPreview | None = None,
) -> PrepaidProformaAdoptionPreview:
    payload = {
        "invoice_id": invoice.id,
        "account_id": invoice.account_id,
        "invoice_number": invoice.invoice_number,
        "invoice_status": invoice.status.value,
        "invoice_is_active": invoice.is_active,
        "invoice_is_proforma": invoice.is_proforma,
        "invoice_updated_at": invoice.updated_at,
        "invoice_total": round_money(to_decimal(invoice.total)),
        "invoice_balance_due": round_money(to_decimal(invoice.balance_due)),
        "invoice_period_start": invoice.billing_period_start,
        "invoice_period_end": invoice.billing_period_end,
        "subscription_id": subscription_id,
        "line_id": line.id if line is not None else None,
        "line_subscription_id": line.subscription_id if line is not None else None,
        "line_quantity": line.quantity if line is not None else None,
        "line_unit_price": line.unit_price if line is not None else None,
        "line_amount": line.amount if line is not None else None,
        "line_updated_at": line.updated_at if line is not None else None,
        "payment_id": payment.id if payment is not None else None,
        "payment_paid_at": payment.paid_at if payment is not None else None,
        "period_start": period_start,
        "period_end": period_end,
        "funding_fingerprint": funding.fingerprint if funding is not None else None,
        "disposition": disposition,
        "reason": reason,
    }
    return PrepaidProformaAdoptionPreview(
        invoice_id=invoice.id,
        account_id=invoice.account_id,
        invoice_number=invoice.invoice_number,
        subscription_id=subscription_id,
        line_id=line.id if line is not None else None,
        settlement_payment_id=payment.id if payment is not None else None,
        settlement_effective_at=payment.paid_at if payment is not None else None,
        billing_period_start=period_start,
        billing_period_end=period_end,
        disposition=disposition,
        currency=(invoice.currency or "NGN").upper(),
        invoice_total=round_money(to_decimal(invoice.total)),
        payment_backed_credit=(
            funding.payment_backed_credit if funding is not None else Decimal("0.00")
        ),
        reason=reason,
        fingerprint=_hash(payload),
    )


def _proforma_adoption_ref(invoice_id: UUID, preview_fingerprint: str) -> str:
    return f"{invoice_id}|{preview_fingerprint}"


def _parse_proforma_adoption_ref(ref_id: str | None) -> tuple[UUID, str] | None:
    if ref_id is None:
        return None
    invoice_raw, separator, fingerprint = ref_id.partition("|")
    if not separator or len(fingerprint) != 64:
        return None
    try:
        invoice_id = UUID(invoice_raw)
        int(fingerprint, 16)
    except ValueError:
        return None
    return invoice_id, fingerprint


def _proforma_adoption_reservation(
    db: Session,
    *,
    invoice: Invoice,
) -> IdempotencyKey | None:
    return db.scalar(
        select(IdempotencyKey)
        .where(
            IdempotencyKey.scope == _PROFORMA_ADOPTION_IDEMPOTENCY_SCOPE,
            IdempotencyKey.account_id == invoice.account_id,
            IdempotencyKey.ref_id.like(f"{invoice.id}|%"),
        )
        .order_by(IdempotencyKey.created_at, IdempotencyKey.id)
        .limit(1)
    )


def _proforma_adoption_payment(db: Session, invoice: Invoice) -> Payment | None:
    allocated_payment_ids = tuple(
        db.scalars(
            select(PaymentAllocation.payment_id)
            .where(
                PaymentAllocation.invoice_id == invoice.id,
                PaymentAllocation.is_active.is_(True),
            )
            .distinct()
            .order_by(PaymentAllocation.payment_id)
        ).all()
    )
    if len(allocated_payment_ids) == 1:
        return db.get(Payment, allocated_payment_ids[0])
    if allocated_payment_ids:
        return None
    funding = _funding_preview(db, invoice)
    if len(funding.source_payment_ids) != 1:
        return None
    return db.get(Payment, funding.source_payment_ids[0])


def preview_funded_prepaid_proforma_adoption(
    db: Session,
    query: PrepaidProformaAdoptionQuery,
) -> PrepaidProformaAdoptionPreview:
    """Preview one exact, payment-backed onboarding proforma adoption.

    This resolver does not infer a subscription or date from proximity. The
    operator supplies the exact subscription identity; the service period is
    derived from the sole exact native payment source and contracted cadence.
    """

    invoice = db.get(Invoice, query.invoice_id)
    if invoice is None:
        _error(
            "invoice_not_found",
            "Invoice was not found.",
            invoice_id=str(query.invoice_id),
        )
    adoption_reservation = _proforma_adoption_reservation(db, invoice=invoice)
    adoption_ref = (
        _parse_proforma_adoption_ref(adoption_reservation.ref_id)
        if adoption_reservation is not None
        else None
    )
    if (
        adoption_ref is not None
        and adoption_ref[0] == invoice.id
        and not invoice.is_proforma
    ):
        line = next(
            (
                item
                for item in _active_positive_lines(db, invoice.id)
                if item.subscription_id == query.subscription_id
            ),
            None,
        )
        payment = _proforma_adoption_payment(db, invoice)
        if (
            line is not None
            and payment is not None
            and payment.paid_at is not None
            and invoice.billing_period_start is not None
            and invoice.billing_period_end is not None
        ):
            return _build_proforma_adoption_preview(
                invoice=invoice,
                subscription_id=query.subscription_id,
                disposition=PrepaidProformaAdoptionDisposition.already_adopted,
                reason="invoice carries durable prepaid proforma adoption evidence",
                line=line,
                payment=payment,
                period_start=invoice.billing_period_start,
                period_end=invoice.billing_period_end,
            )

    if (
        not invoice.is_active
        or invoice.status is not InvoiceStatus.draft
        or not invoice.is_proforma
        or round_money(to_decimal(invoice.balance_due)) <= Decimal("0.00")
        or invoice.billing_period_start is not None
        or invoice.billing_period_end is not None
    ):
        return _build_proforma_adoption_preview(
            invoice=invoice,
            subscription_id=query.subscription_id,
            disposition=PrepaidProformaAdoptionDisposition.manual_review,
            reason="invoice is not a pristine active proforma without a period",
        )

    lines = _active_positive_lines(db, invoice.id)
    line = lines[0] if len(lines) == 1 else None
    if line is None or line.subscription_id is not None:
        return _build_proforma_adoption_preview(
            invoice=invoice,
            subscription_id=query.subscription_id,
            disposition=PrepaidProformaAdoptionDisposition.manual_review,
            reason="adoption requires one exact positive unlinked proforma line",
            line=line,
        )

    subscription = db.get(Subscription, query.subscription_id)
    if (
        subscription is None
        or subscription.subscriber_id != invoice.account_id
        or subscription.billing_mode is not BillingMode.prepaid
        or subscription.status is not SubscriptionStatus.active
        or subscription.next_billing_at is not None
    ):
        return _build_proforma_adoption_preview(
            invoice=invoice,
            subscription_id=query.subscription_id,
            disposition=PrepaidProformaAdoptionDisposition.manual_review,
            reason=(
                "subscription is not the matching active, unanchored prepaid contract"
            ),
            line=line,
        )

    contracted_price = round_money(to_decimal(subscription.unit_price))
    line_amount = round_money(to_decimal(line.amount))
    invoice_subtotal = round_money(to_decimal(invoice.subtotal))
    invoice_tax = round_money(to_decimal(invoice.tax_total))
    invoice_total = round_money(to_decimal(invoice.total))
    if (
        contracted_price <= Decimal("0.00")
        or round_money(to_decimal(line.quantity)) != Decimal("1.00")
        or round_money(to_decimal(line.unit_price)) != contracted_price
        or line_amount != contracted_price
        or invoice_subtotal != line_amount
        or invoice_tax < Decimal("0.00")
        or round_money(invoice_subtotal + invoice_tax) != invoice_total
        or round_money(to_decimal(invoice.balance_due)) != invoice_total
    ):
        return _build_proforma_adoption_preview(
            invoice=invoice,
            subscription_id=query.subscription_id,
            disposition=PrepaidProformaAdoptionDisposition.manual_review,
            reason="proforma charge does not exactly match the contracted base charge",
            line=line,
        )

    has_financial_activity = (
        db.scalar(
            select(PaymentAllocation.id)
            .where(PaymentAllocation.invoice_id == invoice.id)
            .limit(1)
        )
        is not None
        or db.scalar(
            select(CreditNoteApplication.id)
            .where(CreditNoteApplication.invoice_id == invoice.id)
            .limit(1)
        )
        is not None
        or db.scalar(
            select(LedgerEntry.id).where(LedgerEntry.invoice_id == invoice.id).limit(1)
        )
        is not None
    )
    has_coverage = (
        db.scalar(
            select(ServiceEntitlement.id)
            .where(
                ServiceEntitlement.subscription_id == subscription.id,
                ServiceEntitlement.status == ServiceEntitlementStatus.active,
            )
            .limit(1)
        )
        is not None
    )
    has_other_draft = (
        db.scalar(
            select(Invoice.id)
            .join(InvoiceLine, InvoiceLine.invoice_id == Invoice.id)
            .where(
                Invoice.id != invoice.id,
                Invoice.account_id == invoice.account_id,
                Invoice.is_active.is_(True),
                Invoice.status == InvoiceStatus.draft,
                Invoice.is_proforma.is_(False),
                Invoice.balance_due > Decimal("0.00"),
                InvoiceLine.subscription_id == subscription.id,
                InvoiceLine.is_active.is_(True),
                InvoiceLine.amount > Decimal("0.00"),
            )
            .limit(1)
        )
        is not None
    )
    if has_financial_activity or has_coverage or has_other_draft:
        return _build_proforma_adoption_preview(
            invoice=invoice,
            subscription_id=query.subscription_id,
            disposition=PrepaidProformaAdoptionDisposition.manual_review,
            reason="existing financial, coverage, or draft evidence blocks adoption",
            line=line,
        )

    funding = _funding_preview(db, invoice)
    opening = _reviewed_opening_funding_preview(
        db,
        invoice=invoice,
        payment_funding=funding,
    )
    if (
        not funding.fully_funded
        or funding.payment_backed_credit != invoice_total
        or funding.spendable_credit != invoice_total
        or funding.unbacked_credit != Decimal("0.00")
        or len(funding.source_payment_ids) != 1
        or opening.authoritative_funding < invoice_total
    ):
        return _build_proforma_adoption_preview(
            invoice=invoice,
            subscription_id=query.subscription_id,
            disposition=PrepaidProformaAdoptionDisposition.manual_review,
            reason="adoption requires one exact native payment-backed funding source",
            line=line,
            funding=funding,
        )
    payment = db.get(Payment, funding.source_payment_ids[0])
    if payment is None or payment.paid_at is None:
        return _build_proforma_adoption_preview(
            invoice=invoice,
            subscription_id=query.subscription_id,
            disposition=PrepaidProformaAdoptionDisposition.manual_review,
            reason="exact payment source has no authoritative settlement timestamp",
            line=line,
            funding=funding,
        )

    cycle = (
        subscription.billing_cycle
        or (
            subscription.offer_version.billing_cycle
            if subscription.offer_version is not None
            else None
        )
        or (
            subscription.offer.billing_cycle if subscription.offer is not None else None
        )
    )
    if cycle is None:
        return _build_proforma_adoption_preview(
            invoice=invoice,
            subscription_id=query.subscription_id,
            disposition=PrepaidProformaAdoptionDisposition.manual_review,
            reason="subscription has no authoritative billing cadence",
            line=line,
            payment=payment,
            funding=funding,
        )
    from app.services.prepaid_service_renewals import (
        PrepaidSettlementPeriodQuery,
        resolve_prepaid_settlement_period,
    )

    period = resolve_prepaid_settlement_period(
        PrepaidSettlementPeriodQuery(
            effective_at=payment.paid_at,
            billing_cycle=cycle,
        )
    )
    return _build_proforma_adoption_preview(
        invoice=invoice,
        subscription_id=query.subscription_id,
        disposition=(
            PrepaidProformaAdoptionDisposition.exact_funded_onboarding_proforma
        ),
        reason=("one exact payment funds the matching unanchored prepaid contract"),
        line=line,
        payment=payment,
        period_start=period.starts_at,
        period_end=period.ends_at,
        funding=funding,
    )


def _build_paid_invoice_repair_preview(
    *,
    invoice: Invoice,
    subscription_id: UUID,
    disposition: PaidPrepaidInvoiceRepairDisposition,
    reason: str,
    line: InvoiceLine | None = None,
    allocation: PaymentAllocation | None = None,
    settlement: PaymentSettlement | None = None,
    payment: Payment | None = None,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
) -> PaidPrepaidInvoiceRepairPreview:
    payload = {
        "invoice_id": invoice.id,
        "account_id": invoice.account_id,
        "invoice_number": invoice.invoice_number,
        "invoice_status": invoice.status.value,
        "invoice_is_active": invoice.is_active,
        "invoice_is_proforma": invoice.is_proforma,
        "invoice_updated_at": invoice.updated_at,
        "invoice_subtotal": round_money(to_decimal(invoice.subtotal)),
        "invoice_tax": round_money(to_decimal(invoice.tax_total)),
        "invoice_total": round_money(to_decimal(invoice.total)),
        "invoice_balance_due": round_money(to_decimal(invoice.balance_due)),
        "invoice_period_start": invoice.billing_period_start,
        "invoice_period_end": invoice.billing_period_end,
        "subscription_id": subscription_id,
        "line_id": line.id if line is not None else None,
        "line_subscription_id": line.subscription_id if line is not None else None,
        "line_quantity": line.quantity if line is not None else None,
        "line_unit_price": line.unit_price if line is not None else None,
        "line_amount": line.amount if line is not None else None,
        "line_updated_at": line.updated_at if line is not None else None,
        "allocation_id": allocation.id if allocation is not None else None,
        "allocation_payment_id": (
            allocation.payment_id if allocation is not None else None
        ),
        "allocation_amount": allocation.amount if allocation is not None else None,
        "allocation_is_active": (
            allocation.is_active if allocation is not None else None
        ),
        "allocation_ledger_entry_id": (
            allocation.ledger_entry_id if allocation is not None else None
        ),
        "allocation_consumption_ledger_entry_id": (
            allocation.consumption_ledger_entry_id if allocation is not None else None
        ),
        "settlement_id": settlement.id if settlement is not None else None,
        "settlement_amount": settlement.amount if settlement is not None else None,
        "settlement_currency": (
            settlement.currency if settlement is not None else None
        ),
        "settlement_created_at": (
            settlement.created_at if settlement is not None else None
        ),
        "payment_id": payment.id if payment is not None else None,
        "payment_status": payment.status.value if payment is not None else None,
        "payment_is_active": payment.is_active if payment is not None else None,
        "payment_amount": payment.amount if payment is not None else None,
        "payment_currency": payment.currency if payment is not None else None,
        "payment_paid_at": payment.paid_at if payment is not None else None,
        "payment_updated_at": payment.updated_at if payment is not None else None,
        "period_start": period_start,
        "period_end": period_end,
        "disposition": disposition,
        "reason": reason,
    }
    return PaidPrepaidInvoiceRepairPreview(
        invoice_id=invoice.id,
        account_id=invoice.account_id,
        invoice_number=invoice.invoice_number,
        subscription_id=subscription_id,
        line_id=line.id if line is not None else None,
        allocation_id=allocation.id if allocation is not None else None,
        settlement_id=settlement.id if settlement is not None else None,
        payment_id=payment.id if payment is not None else None,
        settlement_effective_at=(
            _utc(payment.paid_at)
            if payment is not None and payment.paid_at is not None
            else None
        ),
        billing_period_start=period_start,
        billing_period_end=period_end,
        disposition=disposition,
        currency=(invoice.currency or "NGN").upper(),
        invoice_total=round_money(to_decimal(invoice.total)),
        allocated_amount=(
            round_money(to_decimal(allocation.amount))
            if allocation is not None
            else Decimal("0.00")
        ),
        reason=reason,
        fingerprint=_hash(payload),
    )


def _paid_invoice_repair_access_key(idempotency_key: str) -> str:
    return (
        "paid-prepaid-repair-access:"
        + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:64]
    )


def _paid_invoice_repair_structural_evidence(
    db: Session,
    *,
    invoice: Invoice,
    subscription_id: UUID,
) -> _PaidPrepaidInvoiceRepairEvidence | None:
    """Resolve completed repair identity only through typed relationships."""

    if invoice.billing_period_start is None or invoice.billing_period_end is None:
        return None
    lines = _active_positive_lines(db, invoice.id)
    if len(lines) != 1 or lines[0].subscription_id != subscription_id:
        return None
    line = lines[0]
    allocations = tuple(
        db.scalars(
            select(PaymentAllocation)
            .where(PaymentAllocation.invoice_id == invoice.id)
            .order_by(PaymentAllocation.id)
        ).all()
    )
    if len(allocations) != 1 or not allocations[0].is_active:
        return None
    allocation = allocations[0]
    payment = db.get(Payment, allocation.payment_id)
    if payment is None:
        return None
    settlement = db.scalar(
        select(PaymentSettlement).where(
            PaymentSettlement.payment_id == payment.id,
        )
    )
    entitlements = tuple(
        db.scalars(
            select(ServiceEntitlement)
            .where(
                ServiceEntitlement.account_id == invoice.account_id,
                ServiceEntitlement.subscription_id == subscription_id,
                ServiceEntitlement.source_invoice_id == invoice.id,
                ServiceEntitlement.source_invoice_line_id == line.id,
                ServiceEntitlement.status == ServiceEntitlementStatus.active,
            )
            .order_by(ServiceEntitlement.id)
        ).all()
    )
    if (
        settlement is None
        or len(entitlements) != 1
        or _utc(entitlements[0].starts_at) != _utc(invoice.billing_period_start)
        or _utc(entitlements[0].ends_at) != _utc(invoice.billing_period_end)
    ):
        return None
    return _PaidPrepaidInvoiceRepairEvidence(
        line=line,
        allocation=allocation,
        payment=payment,
        settlement=settlement,
        entitlement=entitlements[0],
    )


def preview_historical_paid_prepaid_invoice_repair(
    db: Session,
    query: PaidPrepaidInvoiceRepairQuery,
) -> PaidPrepaidInvoiceRepairPreview:
    """Preview exact identity/coverage repair for one already-paid invoice."""

    invoice = db.get(Invoice, query.invoice_id)
    if invoice is None:
        _error(
            "invoice_not_found",
            "Invoice was not found.",
            invoice_id=str(query.invoice_id),
        )
    repair_evidence = _paid_invoice_repair_structural_evidence(
        db,
        invoice=invoice,
        subscription_id=query.subscription_id,
    )
    if repair_evidence is not None:
        return _build_paid_invoice_repair_preview(
            invoice=invoice,
            subscription_id=query.subscription_id,
            disposition=PaidPrepaidInvoiceRepairDisposition.already_repaired,
            reason="invoice has structural paid prepaid repair evidence",
            line=repair_evidence.line,
            allocation=repair_evidence.allocation,
            settlement=repair_evidence.settlement,
            payment=repair_evidence.payment,
            period_start=_utc(repair_evidence.entitlement.starts_at),
            period_end=_utc(repair_evidence.entitlement.ends_at),
        )

    if (
        not invoice.is_active
        or invoice.status is not InvoiceStatus.paid
        or invoice.is_proforma
        or round_money(to_decimal(invoice.balance_due)) != Decimal("0.00")
        or invoice.billing_period_start is not None
        or invoice.billing_period_end is not None
    ):
        return _build_paid_invoice_repair_preview(
            invoice=invoice,
            subscription_id=query.subscription_id,
            disposition=PaidPrepaidInvoiceRepairDisposition.manual_review,
            reason=(
                "invoice is not an active paid non-proforma with missing period identity"
            ),
        )

    lines = _active_positive_lines(db, invoice.id)
    line = lines[0] if len(lines) == 1 else None
    if line is None or line.subscription_id is not None:
        return _build_paid_invoice_repair_preview(
            invoice=invoice,
            subscription_id=query.subscription_id,
            disposition=PaidPrepaidInvoiceRepairDisposition.manual_review,
            reason="repair requires one exact positive unlinked invoice line",
            line=line,
        )

    subscription = db.get(Subscription, query.subscription_id)
    if (
        subscription is None
        or subscription.subscriber_id != invoice.account_id
        or subscription.billing_mode is not BillingMode.prepaid
        or subscription.status
        not in {
            SubscriptionStatus.active,
            SubscriptionStatus.suspended,
            SubscriptionStatus.blocked,
        }
        or subscription.next_billing_at is not None
    ):
        return _build_paid_invoice_repair_preview(
            invoice=invoice,
            subscription_id=query.subscription_id,
            disposition=PaidPrepaidInvoiceRepairDisposition.manual_review,
            reason=(
                "subscription is not the matching unanchored prepaid service contract"
            ),
            line=line,
        )

    allocation_rows = tuple(
        db.scalars(
            select(PaymentAllocation)
            .where(PaymentAllocation.invoice_id == invoice.id)
            .order_by(PaymentAllocation.id)
        ).all()
    )
    allocation = allocation_rows[0] if len(allocation_rows) == 1 else None
    payment = db.get(Payment, allocation.payment_id) if allocation is not None else None
    settlement = payment.settlement if payment is not None else None
    invoice_currency = (invoice.currency or "NGN").upper()
    invoice_total = round_money(to_decimal(invoice.total))
    has_return = bool(
        payment is not None
        and (
            db.scalar(
                select(PaymentRefund.id)
                .where(PaymentRefund.payment_id == payment.id)
                .limit(1)
            )
            is not None
            or db.scalar(
                select(PaymentReversal.id)
                .where(PaymentReversal.payment_id == payment.id)
                .limit(1)
            )
            is not None
        )
    )
    has_credit_note = (
        db.scalar(
            select(CreditNoteApplication.id)
            .where(CreditNoteApplication.invoice_id == invoice.id)
            .limit(1)
        )
        is not None
    )
    if (
        allocation is None
        or not allocation.is_active
        or round_money(to_decimal(allocation.amount)) != invoice_total
        or payment is None
        or not payment.is_active
        or payment.account_id != invoice.account_id
        or payment.status is not PaymentStatus.succeeded
        or payment.paid_at is None
        or (payment.currency or "NGN").upper() != invoice_currency
        or settlement is None
        or settlement.payment_id != payment.id
        or settlement.currency.upper() != invoice_currency
        or round_money(to_decimal(settlement.amount))
        < round_money(to_decimal(allocation.amount))
        or has_return
        or has_credit_note
    ):
        return _build_paid_invoice_repair_preview(
            invoice=invoice,
            subscription_id=query.subscription_id,
            disposition=PaidPrepaidInvoiceRepairDisposition.manual_review,
            reason=(
                "repair requires one exact active allocation from successful "
                "unreturned settlement evidence"
            ),
            line=line,
            allocation=allocation,
            settlement=settlement,
            payment=payment,
        )

    from app.services.prepaid_service_renewals import (
        PrepaidSettlementPeriodQuery,
        resolve_prepaid_monthly_charge,
        resolve_prepaid_settlement_period,
    )

    resolved_charge = resolve_prepaid_monthly_charge(db, subscription, payment.paid_at)
    contracted_price = round_money(to_decimal(subscription.unit_price))
    line_amount = round_money(to_decimal(line.amount))
    invoice_subtotal = round_money(to_decimal(invoice.subtotal))
    invoice_tax = round_money(to_decimal(invoice.tax_total))
    if (
        resolved_charge is None
        or resolved_charge[0] != invoice_total
        or resolved_charge[1].upper() != invoice_currency
        or contracted_price <= Decimal("0.00")
        or round_money(to_decimal(line.quantity)) != Decimal("1.00")
        or round_money(to_decimal(line.unit_price)) != contracted_price
        or line_amount != contracted_price
        or invoice_subtotal != line_amount
        or invoice_tax < Decimal("0.00")
        or round_money(invoice_subtotal + invoice_tax) != invoice_total
    ):
        return _build_paid_invoice_repair_preview(
            invoice=invoice,
            subscription_id=query.subscription_id,
            disposition=PaidPrepaidInvoiceRepairDisposition.manual_review,
            reason="paid invoice charge does not match canonical prepaid renewal terms",
            line=line,
            allocation=allocation,
            settlement=settlement,
            payment=payment,
        )

    existing_entitlements = tuple(
        db.scalars(
            select(ServiceEntitlement)
            .where(
                ServiceEntitlement.subscription_id == subscription.id,
                ServiceEntitlement.status == ServiceEntitlementStatus.active,
            )
            .order_by(ServiceEntitlement.id)
        ).all()
    )
    competing_document_id = db.scalar(
        select(Invoice.id)
        .join(InvoiceLine, InvoiceLine.invoice_id == Invoice.id)
        .where(
            Invoice.id != invoice.id,
            Invoice.account_id == invoice.account_id,
            Invoice.is_active.is_(True),
            Invoice.status != InvoiceStatus.void,
            InvoiceLine.subscription_id == subscription.id,
            InvoiceLine.is_active.is_(True),
            InvoiceLine.amount > Decimal("0.00"),
        )
        .limit(1)
    )
    if existing_entitlements or competing_document_id is not None:
        return _build_paid_invoice_repair_preview(
            invoice=invoice,
            subscription_id=query.subscription_id,
            disposition=PaidPrepaidInvoiceRepairDisposition.manual_review,
            reason=(
                "existing service entitlement or competing document blocks "
                "historical repair"
            ),
            line=line,
            allocation=allocation,
            settlement=settlement,
            payment=payment,
        )

    cycle = resolved_charge[2]
    period = resolve_prepaid_settlement_period(
        PrepaidSettlementPeriodQuery(
            effective_at=payment.paid_at,
            billing_cycle=cycle,
        )
    )
    return _build_paid_invoice_repair_preview(
        invoice=invoice,
        subscription_id=query.subscription_id,
        disposition=PaidPrepaidInvoiceRepairDisposition.exact_paid_unlinked_invoice,
        reason=(
            "one exact successful settlement allocation funds the matching prepaid service"
        ),
        line=line,
        allocation=allocation,
        settlement=settlement,
        payment=payment,
        period_start=period.starts_at,
        period_end=period.ends_at,
    )


def _direct_renewal_evidence(
    db: Session,
    *,
    invoice: Invoice,
    line: InvoiceLine,
    subscription: Subscription,
) -> tuple[tuple[ServiceEntitlement, AccountAdjustment], ...]:
    if invoice.billing_period_start is None or invoice.billing_period_end is None:
        return ()
    entitlements = list(
        db.scalars(
            select(ServiceEntitlement)
            .where(
                ServiceEntitlement.account_id == invoice.account_id,
                ServiceEntitlement.subscription_id == subscription.id,
                ServiceEntitlement.status == ServiceEntitlementStatus.active,
                ServiceEntitlement.starts_at < invoice.billing_period_end,
                ServiceEntitlement.ends_at > invoice.billing_period_start,
            )
            .order_by(ServiceEntitlement.id)
        ).all()
    )
    evidence: list[tuple[ServiceEntitlement, AccountAdjustment]] = []
    for entitlement in entitlements:
        if (
            entitlement.source_invoice_id is not None
            or entitlement.source_invoice_line_id is not None
            or entitlement.source_billing_grant_id is not None
            or entitlement.source_ledger_entry_id is None
        ):
            continue
        adjustment = db.scalar(
            select(AccountAdjustment).where(
                AccountAdjustment.ledger_entry_id == entitlement.source_ledger_entry_id,
                AccountAdjustment.account_id == invoice.account_id,
                AccountAdjustment.origin == _RENEWAL_ORIGIN,
                AccountAdjustment.reversed_at.is_(None),
            )
        )
        expected_origin = (
            f"{subscription.id}:{_utc(entitlement.starts_at).isoformat()}:"
            f"{_utc(entitlement.ends_at).isoformat()}"
        )
        if (
            adjustment is None
            or adjustment.origin_ref != expected_origin
            or adjustment.currency.upper() != (invoice.currency or "NGN").upper()
            or round_money(to_decimal(adjustment.amount))
            != round_money(to_decimal(invoice.total))
            or round_money(to_decimal(entitlement.amount_funded))
            != round_money(to_decimal(line.amount))
            or entitlement.currency.upper() != (invoice.currency or "NGN").upper()
        ):
            continue
        evidence.append((entitlement, adjustment))
    return tuple(evidence)


def _build_preview(
    *,
    invoice: Invoice,
    disposition: PrepaidDraftDisposition,
    action: PrepaidDraftAction,
    funding: AccountCreditInvoiceFundingPreview,
    subscription_ids: tuple[UUID, ...],
    entitlement_ids: tuple[UUID, ...] = (),
    adjustment_ids: tuple[UUID, ...] = (),
    opening: ReviewedOpeningFundingPreview | None = None,
    reason: str,
) -> PrepaidDraftReconciliationPreview:
    opening_available = (
        opening.available_amount if opening is not None else Decimal("0.00")
    )
    opening_required = min(funding.shortfall, opening_available)
    authoritative_funding = (
        opening.authoritative_funding
        if opening is not None
        else funding.payment_backed_credit
    )
    payload = {
        "invoice_id": invoice.id,
        "account_id": invoice.account_id,
        "status": invoice.status.value,
        "is_active": invoice.is_active,
        "is_proforma": invoice.is_proforma,
        "updated_at": invoice.updated_at,
        "currency": (invoice.currency or "NGN").upper(),
        "total": round_money(to_decimal(invoice.total)),
        "balance_due": round_money(to_decimal(invoice.balance_due)),
        "period_start": invoice.billing_period_start,
        "period_end": invoice.billing_period_end,
        "disposition": disposition,
        "action": action,
        "funding_fingerprint": funding.fingerprint,
        "authoritative_funding": authoritative_funding,
        "opening_funding_baseline_id": (
            opening.baseline_id if opening is not None else None
        ),
        "opening_funding_available": opening_available,
        "opening_funding_required": opening_required,
        "subscription_ids": subscription_ids,
        "entitlement_ids": entitlement_ids,
        "adjustment_ids": adjustment_ids,
        "reason": reason,
    }
    return PrepaidDraftReconciliationPreview(
        invoice_id=invoice.id,
        account_id=invoice.account_id,
        invoice_number=invoice.invoice_number,
        disposition=disposition,
        recommended_action=action,
        currency=(invoice.currency or "NGN").upper(),
        invoice_total=round_money(to_decimal(invoice.total)),
        balance_due=round_money(to_decimal(invoice.balance_due)),
        payment_backed_credit=funding.payment_backed_credit,
        authoritative_funding=authoritative_funding,
        opening_funding_available=opening_available,
        opening_funding_required=opening_required,
        opening_funding_baseline_id=(
            opening.baseline_id if opening is not None else None
        ),
        unbacked_credit=funding.unbacked_credit,
        shortfall=funding.shortfall,
        subscription_ids=subscription_ids,
        entitlement_ids=entitlement_ids,
        renewal_adjustment_ids=adjustment_ids,
        reason=reason,
        fingerprint=_hash(payload),
    )


def preview_prepaid_draft_reconciliation(
    db: Session,
    invoice_id: UUID,
) -> PrepaidDraftReconciliationPreview:
    """Classify one invoice from canonical invoice, funding, and coverage facts."""

    invoice = db.get(Invoice, invoice_id)
    if invoice is None:
        _error(
            "invoice_not_found", "Invoice was not found.", invoice_id=str(invoice_id)
        )
    funding = _funding_preview(db, invoice)
    opening = _reviewed_opening_funding_preview(
        db,
        invoice=invoice,
        payment_funding=funding,
    )
    metadata = dict(invoice.metadata_ or {}).get(_METADATA_KEY)
    if isinstance(metadata, dict) and invoice.status in {
        InvoiceStatus.paid,
        InvoiceStatus.void,
    }:
        action_value = str(metadata.get("action") or PrepaidDraftAction.none.value)
        try:
            action = PrepaidDraftAction(action_value)
        except ValueError:
            action = PrepaidDraftAction.none
        return _build_preview(
            invoice=invoice,
            opening=opening,
            disposition=PrepaidDraftDisposition.already_reconciled,
            action=action,
            funding=funding,
            subscription_ids=(),
            reason="invoice carries durable prepaid draft reconciliation evidence",
        )
    if (
        not invoice.is_active
        or invoice.status != InvoiceStatus.draft
        or invoice.is_proforma
        or round_money(to_decimal(invoice.balance_due)) <= Decimal("0.00")
    ):
        return _build_preview(
            invoice=invoice,
            opening=opening,
            disposition=PrepaidDraftDisposition.manual_review,
            action=PrepaidDraftAction.none,
            funding=funding,
            subscription_ids=(),
            reason="invoice is not an active financial prepaid draft",
        )
    if (
        invoice.billing_period_start is None
        or invoice.billing_period_end is None
        or _utc(invoice.billing_period_end) <= _utc(invoice.billing_period_start)
    ):
        return _build_preview(
            invoice=invoice,
            opening=opening,
            disposition=PrepaidDraftDisposition.manual_review,
            action=PrepaidDraftAction.none,
            funding=funding,
            subscription_ids=(),
            reason="invoice has no exact positive billing period",
        )

    lines = _active_positive_lines(db, invoice.id)
    if len(lines) != 1 or lines[0].subscription_id is None:
        return _build_preview(
            invoice=invoice,
            opening=opening,
            disposition=PrepaidDraftDisposition.manual_review,
            action=PrepaidDraftAction.none,
            funding=funding,
            subscription_ids=tuple(
                sorted(
                    {
                        line.subscription_id
                        for line in lines
                        if line.subscription_id is not None
                    },
                    key=str,
                )
            ),
            reason="automatic repair requires one exact positive subscription line",
        )
    line = lines[0]
    subscription_id = line.subscription_id
    assert subscription_id is not None
    subscription = db.get(Subscription, subscription_id)
    if (
        subscription is None
        or subscription.subscriber_id != invoice.account_id
        or subscription.billing_mode != BillingMode.prepaid
    ):
        return _build_preview(
            invoice=invoice,
            opening=opening,
            disposition=PrepaidDraftDisposition.manual_review,
            action=PrepaidDraftAction.none,
            funding=funding,
            subscription_ids=(subscription_id,),
            reason="invoice line is not owned by one matching prepaid subscription",
        )

    has_activity = (
        db.scalar(
            select(PaymentAllocation.id)
            .where(
                PaymentAllocation.invoice_id == invoice.id,
                PaymentAllocation.is_active.is_(True),
            )
            .limit(1)
        )
        is not None
        or db.scalar(
            select(CreditNoteApplication.id)
            .where(CreditNoteApplication.invoice_id == invoice.id)
            .limit(1)
        )
        is not None
        or db.scalar(
            select(LedgerEntry.id).where(LedgerEntry.invoice_id == invoice.id).limit(1)
        )
        is not None
    )
    if has_activity:
        return _build_preview(
            invoice=invoice,
            opening=opening,
            disposition=PrepaidDraftDisposition.manual_review,
            action=PrepaidDraftAction.none,
            funding=funding,
            subscription_ids=(subscription.id,),
            reason="draft already has financial activity",
        )

    direct_evidence = _direct_renewal_evidence(
        db,
        invoice=invoice,
        line=line,
        subscription=subscription,
    )
    if len(direct_evidence) == 1:
        entitlement, adjustment = direct_evidence[0]
        return _build_preview(
            invoice=invoice,
            opening=opening,
            disposition=PrepaidDraftDisposition.already_renewed,
            action=PrepaidDraftAction.void_duplicate,
            funding=funding,
            subscription_ids=(subscription.id,),
            entitlement_ids=(entitlement.id,),
            adjustment_ids=(adjustment.id,),
            reason="exact direct-renewal debit and entitlement already fund this cycle",
        )
    if len(direct_evidence) > 1:
        return _build_preview(
            invoice=invoice,
            opening=opening,
            disposition=PrepaidDraftDisposition.manual_review,
            action=PrepaidDraftAction.none,
            funding=funding,
            subscription_ids=(subscription.id,),
            entitlement_ids=tuple(item[0].id for item in direct_evidence),
            adjustment_ids=tuple(item[1].id for item in direct_evidence),
            reason="multiple direct-renewal evidence pairs overlap the draft",
        )

    other_overlap = db.scalar(
        select(ServiceEntitlement.id)
        .where(
            ServiceEntitlement.subscription_id == subscription.id,
            ServiceEntitlement.status == ServiceEntitlementStatus.active,
            ServiceEntitlement.starts_at < invoice.billing_period_end,
            ServiceEntitlement.ends_at > invoice.billing_period_start,
        )
        .limit(1)
    )
    if other_overlap is not None:
        return _build_preview(
            invoice=invoice,
            opening=opening,
            disposition=PrepaidDraftDisposition.manual_review,
            action=PrepaidDraftAction.none,
            funding=funding,
            subscription_ids=(subscription.id,),
            entitlement_ids=(other_overlap,),
            reason="overlapping coverage is not exact direct-renewal evidence",
        )
    if funding.fully_funded:
        return _build_preview(
            invoice=invoice,
            opening=opening,
            disposition=PrepaidDraftDisposition.exact_payment_fundable,
            action=PrepaidDraftAction.settle_paid,
            funding=funding,
            subscription_ids=(subscription.id,),
            reason="exact native payment-backed credit fully covers the draft",
        )
    if (
        funding.shortfall > Decimal("0.00")
        and opening.baseline_id is not None
        and opening.available_amount >= funding.shortfall
        and opening.authoritative_funding >= funding.invoice_remaining
        and funding.unbacked_credit == Decimal("0.00")
    ):
        return _build_preview(
            invoice=invoice,
            opening=opening,
            disposition=PrepaidDraftDisposition.reviewed_opening_fundable,
            action=PrepaidDraftAction.settle_paid,
            funding=funding,
            subscription_ids=(subscription.id,),
            reason=(
                "settlement-backed payments plus reviewed opening funding "
                "fully cover the draft"
            ),
        )
    if funding.unbacked_credit > Decimal("0.00"):
        return _build_preview(
            invoice=invoice,
            opening=opening,
            disposition=PrepaidDraftDisposition.legacy_unbacked_funding,
            action=PrepaidDraftAction.none,
            funding=funding,
            subscription_ids=(subscription.id,),
            reason="visible credit is not fully backed by canonical payment evidence",
        )
    return _build_preview(
        invoice=invoice,
        opening=opening,
        disposition=PrepaidDraftDisposition.insufficient_funding,
        action=PrepaidDraftAction.none,
        funding=funding,
        subscription_ids=(subscription.id,),
        reason="exact payment-backed credit is below the full invoice balance",
    )


def preview_prepaid_draft_cohort(
    db: Session,
    *,
    account_id: UUID | None = None,
    limit: int | None = None,
) -> tuple[PrepaidDraftReconciliationPreview, ...]:
    """Return the deterministic active prepaid-draft cohort without writes."""

    statement = (
        select(Invoice.id)
        .join(InvoiceLine, InvoiceLine.invoice_id == Invoice.id)
        .join(Subscription, Subscription.id == InvoiceLine.subscription_id)
        .where(
            Invoice.is_active.is_(True),
            Invoice.status == InvoiceStatus.draft,
            Invoice.is_proforma.is_(False),
            Invoice.balance_due > Decimal("0.00"),
            InvoiceLine.is_active.is_(True),
            InvoiceLine.amount > Decimal("0.00"),
            Subscription.billing_mode == BillingMode.prepaid,
        )
        .group_by(Invoice.id, Invoice.created_at)
        .order_by(Invoice.created_at, Invoice.id)
    )
    if account_id is not None:
        statement = statement.where(Invoice.account_id == account_id)
    if limit is not None:
        statement = statement.limit(limit)
    invoice_ids = tuple(db.scalars(statement).all())
    return tuple(
        preview_prepaid_draft_reconciliation(db, invoice_id)
        for invoice_id in invoice_ids
    )


def _record_metadata(
    invoice: Invoice,
    *,
    preview: PrepaidDraftReconciliationPreview,
    action: PrepaidDraftAction,
    context: CommandContext | None,
    effective_at: datetime,
) -> None:
    metadata = dict(invoice.metadata_ or {})
    metadata[_METADATA_KEY] = {
        "action": action.value,
        "source_disposition": preview.disposition.value,
        "preview_fingerprint": preview.fingerprint,
        "idempotency_key": context.idempotency_key if context else None,
        "command_id": str(context.command_id) if context else None,
        "reconciled_at": _utc(effective_at).isoformat(),
        "entitlement_ids": [str(value) for value in preview.entitlement_ids],
        "renewal_adjustment_ids": [
            str(value) for value in preview.renewal_adjustment_ids
        ],
    }
    invoice.metadata_ = metadata


def _stage_opening_funding_consumption(
    db: Session,
    *,
    invoice: Invoice,
    preview: PrepaidDraftReconciliationPreview,
    amount: Decimal,
    effective_at: datetime,
    context: CommandContext,
) -> PrepaidOpeningFundingConsumption:
    baseline_id = preview.opening_funding_baseline_id
    if baseline_id is None or amount <= Decimal("0.00"):
        _error(
            "opening_funding_unavailable",
            "Reviewed opening funding is not available for this invoice.",
        )
    baseline = db.scalar(
        select(PrepaidFundingBaseline)
        .where(PrepaidFundingBaseline.id == baseline_id)
        .with_for_update()
    )
    if (
        baseline is None
        or not baseline.is_active
        or baseline.account_id != invoice.account_id
        or baseline.currency != preview.currency
    ):
        _error(
            "opening_funding_changed",
            "Reviewed opening funding changed after preview; preview again.",
        )
    consumed = round_money(
        to_decimal(
            db.query(
                func.coalesce(
                    func.sum(PrepaidOpeningFundingConsumption.amount),
                    0,
                )
            )
            .filter(PrepaidOpeningFundingConsumption.baseline_id == baseline.id)
            .scalar()
        )
    )
    source_remaining = round_money(to_decimal(baseline.amount) - consumed)
    if source_remaining < amount:
        _error(
            "opening_funding_changed",
            "Reviewed opening funding was already consumed; preview again.",
            available_amount=str(max(Decimal("0.00"), source_remaining)),
        )
    command_key = context.idempotency_key or ""
    opening_key = (
        "prepaid-opening:" + hashlib.sha256(command_key.encode("utf-8")).hexdigest()
    )
    existing = db.scalar(
        select(PrepaidOpeningFundingConsumption).where(
            PrepaidOpeningFundingConsumption.idempotency_key == opening_key
        )
    )
    if existing is not None:
        if (
            existing.invoice_id != invoice.id
            or round_money(to_decimal(existing.amount)) != amount
            or existing.reconciliation_fingerprint != preview.fingerprint
        ):
            _error(
                "idempotency_conflict",
                "Opening-funding idempotency evidence belongs to another request.",
            )
        return existing
    ledger_entry = LedgerEntries.create(
        db,
        LedgerEntryCreate(
            account_id=invoice.account_id,
            invoice_id=invoice.id,
            entry_type=LedgerEntryType.debit,
            source=LedgerSource.adjustment,
            category=LedgerCategory.internet_service,
            amount=amount,
            currency=preview.currency,
            memo=(
                "Reviewed opening funding consumed for prepaid invoice "
                f"{invoice.invoice_number or invoice.id}"
            ),
            effective_date=_utc(effective_at),
        ),
        affects_customer_position=False,
        commit=False,
    )
    consumption = PrepaidOpeningFundingConsumption(
        baseline_id=baseline.id,
        account_id=invoice.account_id,
        invoice_id=invoice.id,
        ledger_entry_id=ledger_entry.id,
        amount=amount,
        currency=preview.currency,
        approval_evidence_ref=baseline.batch.evidence_ref,
        approval_actor=baseline.batch.approved_by,
        reconciliation_fingerprint=preview.fingerprint,
        idempotency_key=opening_key,
        consumed_at=_utc(effective_at),
    )
    db.add(consumption)
    db.flush()
    _stage_consumption_posting(db, consumption)
    return consumption


def _stage_consumption_posting(db: Session, consumption) -> None:
    """One shadow posting group per opening-funding consumption.

    Staged at the deciding owner root inside its own command; the
    renewals-driven participant path carries no owner command and skips —
    the verifier owns that gap (producer_not_owner_wrapped), never this
    seam.
    """
    from app.services.owner_commands import (
        current_command_context,
        owner_command_active,
    )

    if not owner_command_active(db):
        return
    from app.models.customer_subledger import (
        PositionEffectKind,
        PostingCommandKind,
        PostingProducer,
        PostingSourceKind,
    )
    from app.services.billing.customer_subledger import (
        EffectInput,
        StagePostingGroupCommand,
        stage_posting_group,
    )

    stage_posting_group(
        db,
        StagePostingGroupCommand(
            account_id=consumption.account_id,
            currency=consumption.currency,
            command_kind=PostingCommandKind.prepaid_consumption,
            producer_owner=PostingProducer.prepaid_draft_reconciliation,
            source_kind=PostingSourceKind.prepaid_opening_funding_consumption,
            source_id=consumption.id,
            occurred_at=(
                consumption.consumed_at.replace(tzinfo=UTC)
                if consumption.consumed_at.tzinfo is None
                else consumption.consumed_at
            ),
            effects=(
                EffectInput(
                    effect=PositionEffectKind.prepaid_funding_consumed,
                    amount=Decimal(str(consumption.amount)),
                    invoice_id=consumption.invoice_id,
                ),
            ),
            idempotency_key=(f"posting:prepaid_opening_consumption:{consumption.id}"),
        ),
        context=current_command_context(db),
    )


def _exception_alert_fingerprint(invoice_id: UUID) -> str:
    return f"prepaid-draft-reconciliation:{invoice_id}"


def _exception_event_type(invoice_id: UUID) -> str:
    return f"prepaid_draft_reconciliation_review:{invoice_id}"


def _stage_review_exception(
    db: Session,
    *,
    preview: PrepaidDraftReconciliationPreview,
) -> PrepaidDraftReconciliationException:
    alert_fingerprint = _exception_alert_fingerprint(preview.invoice_id)
    exception = db.scalar(
        select(PrepaidDraftReconciliationException).where(
            PrepaidDraftReconciliationException.invoice_id == preview.invoice_id
        )
    )
    created = exception is None
    if exception is None:
        exception = PrepaidDraftReconciliationException(
            account_id=preview.account_id,
            invoice_id=preview.invoice_id,
            status="open",
            reason="reviewed_opening_funding_required",
            currency=preview.currency,
            required_amount=preview.balance_due,
            payment_backed_amount=preview.payment_backed_credit,
            opening_funding_amount=preview.opening_funding_required,
            preview_fingerprint=preview.fingerprint,
            alert_fingerprint=alert_fingerprint,
        )
        db.add(exception)
    else:
        same_evidence = exception.preview_fingerprint == preview.fingerprint
        exception.status = "open"
        exception.reason = "reviewed_opening_funding_required"
        exception.required_amount = preview.balance_due
        exception.payment_backed_amount = preview.payment_backed_credit
        exception.opening_funding_amount = preview.opening_funding_required
        exception.preview_fingerprint = preview.fingerprint
        exception.resolved_at = None
        if not same_evidence:
            exception.attempt_count = int(exception.attempt_count or 0) + 1
    db.flush()

    from app.services import staff_notifications

    staff_notifications.queue_permission_review_request(
        db,
        permission_key="billing:write",
        fingerprint=alert_fingerprint,
        event_type=_exception_event_type(preview.invoice_id),
        title="Prepaid invoice needs funding review",
        body=(
            f"Invoice {preview.invoice_number or preview.invoice_id} has "
            f"{preview.currency} {preview.payment_backed_credit:.2f} in "
            "settlement-backed payments and requires "
            f"{preview.currency} {preview.opening_funding_required:.2f} from "
            "approved opening funding."
        ),
        target_url=(
            f"/admin/billing/invoices/{preview.invoice_id}#prepaid-reconciliation"
        ),
        category="billing",
        source="prepaid_draft_reconciliation",
    )
    if created:
        AuditEvents.stage(
            db,
            AuditEventCreate(
                action="record_prepaid_draft_reconciliation_exception",
                entity_type="prepaid_draft_reconciliation_exception",
                entity_id=str(exception.id),
                metadata_={
                    "invoice_id": str(preview.invoice_id),
                    "account_id": str(preview.account_id),
                    "currency": preview.currency,
                    "required_amount": str(preview.balance_due),
                    "payment_backed_amount": str(preview.payment_backed_credit),
                    "opening_funding_amount": str(preview.opening_funding_required),
                    "preview_fingerprint": preview.fingerprint,
                },
            ),
        )
    db.flush()
    return exception


def _resolve_review_exception(db: Session, invoice_id: UUID) -> None:
    exception = db.scalar(
        select(PrepaidDraftReconciliationException).where(
            PrepaidDraftReconciliationException.invoice_id == invoice_id
        )
    )
    if exception is None or exception.status == "resolved":
        return
    exception.status = "resolved"
    exception.resolved_at = datetime.now(UTC)
    from app.services import staff_notifications

    staff_notifications.resolve_permission_review_request(
        db,
        fingerprint=_exception_alert_fingerprint(invoice_id),
        event_type=_exception_event_type(invoice_id),
    )
    db.flush()


def _stage_action(
    db: Session,
    *,
    preview: PrepaidDraftReconciliationPreview,
    effective_at: datetime,
    context: CommandContext | None,
) -> tuple[
    Invoice,
    Decimal,
    Decimal,
    PrepaidOpeningFundingConsumption | None,
]:
    invoice = lock_for_update(db, Invoice, str(preview.invoice_id))
    if invoice is None:
        _error("invoice_not_found", "Invoice was not found.")
    # Only the operator-confirmed opening-funding branch is a reviewed
    # correction of the record; every other path here is an ordinary settlement
    # consequence. See the anchor-authority note at the projection call below.
    reviewed_opening_correction = False
    if preview.recommended_action is PrepaidDraftAction.settle_paid:
        try:
            Invoices.issue_draft_for_owner(
                db,
                str(invoice.id),
                issued_at=_utc(effective_at),
                due_at=_utc(effective_at),
                reason="reconcile_exactly_funded_prepaid_draft",
                apply_available_credit=False,
            )
            funding = _funding_preview(db, invoice)
            if preview.disposition is PrepaidDraftDisposition.reviewed_opening_fundable:
                if context is None:
                    _error(
                        "review_required",
                        "Reviewed opening funding requires operator confirmation.",
                    )
                result = AccountCreditApplications.apply_invoice_available(
                    db,
                    invoice,
                    preview_fingerprint=funding.fingerprint,
                    funding_position_at=funding.funding_position_at,
                )
                opening_amount = result.invoice_remaining
                if opening_amount != preview.opening_funding_required:
                    _error(
                        "stale_preview",
                        "Invoice remainder changed while applying payment sources.",
                    )
                opening_consumption = _stage_opening_funding_consumption(
                    db,
                    invoice=invoice,
                    preview=preview,
                    amount=opening_amount,
                    effective_at=effective_at,
                    context=context,
                )
                finalize_invoice_application_for_owner(
                    db,
                    invoice,
                    effective_at=_utc(effective_at),
                )
                reviewed_opening_correction = True
            else:
                result = AccountCreditApplications.apply_invoice_fully(
                    db,
                    invoice,
                    preview_fingerprint=funding.fingerprint,
                    funding_position_at=funding.funding_position_at,
                )
                opening_consumption = None
        except (AccountCreditApplicationError, InvoiceOwnerError) as exc:
            _error(
                "participant_rejected",
                "Invoice or account-credit owner rejected the reviewed settlement.",
                participant_error=getattr(exc, "code", type(exc).__name__),
            )
        applied = result.applied
        if opening_consumption is not None:
            applied = round_money(applied + opening_consumption.amount)
        payment_applied = result.applied
    elif preview.recommended_action is PrepaidDraftAction.void_duplicate:
        try:
            Invoices.void_pristine_draft_for_owner(
                db,
                str(invoice.id),
                reason="Duplicate of exact direct-renewal service evidence",
                idempotency_key=f"prepaid-draft-overlap-{invoice.id}",
            )
        except InvoiceOwnerError as exc:
            _error(
                "participant_rejected",
                "Invoice owner rejected the reviewed duplicate closure.",
                participant_error=type(exc).__name__,
            )
        applied = Decimal("0.00")
        payment_applied = Decimal("0.00")
        opening_consumption = None
    else:
        _error(
            "not_actionable",
            "This draft requires more funding or manual evidence review.",
            disposition=preview.disposition.value,
        )
    if preview.recommended_action is PrepaidDraftAction.settle_paid:
        # This owner settles the documentary period; it never writes
        # `Subscription.next_billing_at` itself. Billing-anchor advancement is
        # owned by `financial.prepaid_service_renewals`, so the committed
        # entitlement evidence is handed to that owner to project. See
        # docs/SOT_RELATIONSHIP_MAP.md, "Prepaid renewal boundary".
        #
        # Authority mirrors the two finalizers this replaced. The reviewed
        # opening-funding branch is where `finalize_invoice_application_for_owner`
        # ran, and that finalizer projected the anchor unconditionally: an
        # operator has confirmed a fingerprint-bound preview and this owner has
        # just rewritten the invoice's period, so a stale anchor left by a
        # long-lapsed period is corrected down to the effective date. Every
        # other settlement here went through `_finalize_invoice_payment_effects`,
        # which never wrote the anchor backwards, so it stays observational and
        # cannot claw back a lead another owner granted.
        from app.services.prepaid_service_renewals import (
            BillingAnchorAuthority,
            project_prepaid_billing_anchor_for_invoice,
        )

        project_prepaid_billing_anchor_for_invoice(
            db,
            invoice,
            evidence_ref=f"prepaid_draft_reconciliation:{invoice.id}",
            authority=(
                BillingAnchorAuthority.reviewed_reconciliation
                if reviewed_opening_correction
                else BillingAnchorAuthority.funding_observation
            ),
        )
    _record_metadata(
        invoice,
        preview=preview,
        action=preview.recommended_action,
        context=context,
        effective_at=effective_at,
    )
    AuditEvents.stage(
        db,
        AuditEventCreate(
            action="reconcile_prepaid_draft_invoice",
            entity_type="invoice",
            entity_id=str(invoice.id),
            metadata_={
                "action": preview.recommended_action.value,
                "source_disposition": preview.disposition.value,
                "preview_fingerprint": preview.fingerprint,
                "applied_amount": str(applied),
                "payment_applied_amount": str(payment_applied),
                "opening_funding_applied_amount": str(
                    opening_consumption.amount
                    if opening_consumption is not None
                    else Decimal("0.00")
                ),
                "opening_funding_consumption_id": (
                    str(opening_consumption.id)
                    if opening_consumption is not None
                    else None
                ),
                "opening_funding_baseline_id": (
                    str(opening_consumption.baseline_id)
                    if opening_consumption is not None
                    else None
                ),
                "opening_funding_ledger_entry_id": (
                    str(opening_consumption.ledger_entry_id)
                    if opening_consumption is not None
                    else None
                ),
                "economic_delta": (
                    str(applied)
                    if preview.recommended_action is PrepaidDraftAction.settle_paid
                    else "0.00"
                ),
                "entitlement_ids": [str(value) for value in preview.entitlement_ids],
                "renewal_adjustment_ids": [
                    str(value) for value in preview.renewal_adjustment_ids
                ],
            },
        ),
    )
    emit_event(
        db,
        EventType.prepaid_draft_reconciled,
        {
            "invoice_id": str(invoice.id),
            "invoice_number": invoice.invoice_number,
            "action": preview.recommended_action.value,
            "source_disposition": preview.disposition.value,
            "final_status": invoice.status.value,
            "applied_amount": str(applied),
            "payment_applied_amount": str(payment_applied),
            "opening_funding_applied_amount": str(
                opening_consumption.amount
                if opening_consumption is not None
                else Decimal("0.00")
            ),
            "currency": invoice.currency,
            "preview_fingerprint": preview.fingerprint,
        },
        account_id=invoice.account_id,
        invoice_id=invoice.id,
    )
    db.flush()
    if preview.recommended_action is PrepaidDraftAction.settle_paid and (
        invoice.status != InvoiceStatus.paid or invoice.balance_due != Decimal("0.00")
    ):
        _error(
            "incomplete_repair",
            "Reconciliation did not produce a fully paid invoice.",
        )
    return invoice, applied, payment_applied, opening_consumption


def _replay_result(
    db: Session,
    *,
    command: ReconcilePrepaidDraftCommand,
    reservation: IdempotencyKey,
) -> PrepaidDraftReconciliationResult:
    if reservation.ref_id != str(command.invoice_id):
        _error(
            "idempotency_conflict",
            "Idempotency key belongs to another invoice.",
        )
    invoice = db.get(Invoice, command.invoice_id, populate_existing=True)
    metadata = (
        dict(invoice.metadata_ or {}).get(_METADATA_KEY)
        if invoice is not None
        else None
    )
    if invoice is None or not isinstance(metadata, dict):
        _error(
            "idempotency_conflict",
            "Idempotency evidence is incomplete.",
        )
    if metadata.get("idempotency_key") != command.context.idempotency_key:
        _error(
            "idempotency_conflict",
            "Invoice reconciliation evidence does not match the idempotency key.",
        )
    action = PrepaidDraftAction(str(metadata["action"]))
    source = PrepaidDraftDisposition(str(metadata["source_disposition"]))
    opening_consumption = db.scalar(
        select(PrepaidOpeningFundingConsumption).where(
            PrepaidOpeningFundingConsumption.invoice_id == invoice.id
        )
    )
    opening_applied = round_money(
        to_decimal(
            opening_consumption.amount
            if opening_consumption is not None
            else Decimal("0.00")
        )
    )
    applied_amount = (
        round_money(to_decimal(invoice.total))
        if action is PrepaidDraftAction.settle_paid
        else Decimal("0.00")
    )
    return PrepaidDraftReconciliationResult(
        invoice_id=invoice.id,
        disposition=source,
        action=action,
        final_status=invoice.status,
        applied_amount=applied_amount,
        payment_applied_amount=round_money(applied_amount - opening_applied),
        opening_funding_applied_amount=opening_applied,
        opening_funding_consumption_id=(
            opening_consumption.id if opening_consumption is not None else None
        ),
        preview_fingerprint=str(metadata["preview_fingerprint"]),
        replayed=True,
    )


def _replay_proforma_adoption_result(
    db: Session,
    *,
    command: AdoptFundedPrepaidProformaCommand,
    reservation: IdempotencyKey,
) -> PrepaidProformaAdoptionResult:
    parsed_ref = _parse_proforma_adoption_ref(reservation.ref_id)
    if parsed_ref is None or parsed_ref[0] != command.invoice_id:
        _error(
            "idempotency_conflict",
            "Idempotency key belongs to another invoice.",
        )
    _invoice_id, preview_fingerprint = parsed_ref
    invoice = db.get(Invoice, command.invoice_id, populate_existing=True)
    if invoice is None:
        _error(
            "idempotency_conflict",
            "Proforma adoption evidence is incomplete.",
        )
    matching_lines = tuple(
        line
        for line in _active_positive_lines(db, invoice.id)
        if command.subscription_id == line.subscription_id
    )
    payment = _proforma_adoption_payment(db, invoice)
    if (
        len(matching_lines) != 1
        or payment is None
        or payment.paid_at is None
        or invoice.billing_period_start is None
        or invoice.billing_period_end is None
    ):
        _error(
            "idempotency_conflict",
            "Proforma adoption structural evidence is incomplete.",
        )
    line = matching_lines[0]
    return PrepaidProformaAdoptionResult(
        invoice_id=invoice.id,
        subscription_id=command.subscription_id,
        line_id=line.id,
        settlement_payment_id=payment.id,
        settlement_effective_at=_utc(payment.paid_at),
        billing_period_start=_utc(invoice.billing_period_start),
        billing_period_end=_utc(invoice.billing_period_end),
        preview_fingerprint=preview_fingerprint,
        replayed=True,
    )


def adopt_funded_prepaid_proforma(
    db: Session,
    command: AdoptFundedPrepaidProformaCommand,
) -> PrepaidProformaAdoptionResult:
    """Adopt one reviewed onboarding proforma as a financial prepaid draft."""

    def operation() -> PrepaidProformaAdoptionResult:
        key = (command.context.idempotency_key or "").strip()
        if not key or len(key) > 120:
            _error(
                "missing_idempotency_key",
                "A bounded idempotency key is required.",
            )
        invoice = db.get(Invoice, command.invoice_id)
        if invoice is None:
            _error("invoice_not_found", "Invoice was not found.")
        lock_account(db, str(invoice.account_id))
        reservation = db.scalar(
            select(IdempotencyKey)
            .where(
                IdempotencyKey.scope == _PROFORMA_ADOPTION_IDEMPOTENCY_SCOPE,
                IdempotencyKey.key == key,
            )
            .with_for_update()
        )
        if reservation is not None:
            return _replay_proforma_adoption_result(
                db,
                command=command,
                reservation=reservation,
            )

        locked_invoice = lock_for_update(db, Invoice, str(command.invoice_id))
        locked_subscription = lock_for_update(
            db,
            Subscription,
            str(command.subscription_id),
        )
        if locked_invoice is None:
            _error("invoice_not_found", "Invoice was not found.")
        if locked_subscription is None:
            _error(
                "not_actionable",
                "Subscription was not found for reviewed proforma adoption.",
            )
        current = preview_funded_prepaid_proforma_adoption(
            db,
            PrepaidProformaAdoptionQuery(
                invoice_id=command.invoice_id,
                subscription_id=command.subscription_id,
            ),
        )
        if current.fingerprint != command.preview_fingerprint:
            _error(
                "stale_preview",
                "Proforma evidence changed after preview; preview again.",
            )
        if not current.actionable:
            _error(
                "not_actionable",
                "This proforma requires manual evidence review.",
                disposition=current.disposition.value,
                reason=current.reason,
            )
        if (
            current.line_id is None
            or current.settlement_payment_id is None
            or current.settlement_effective_at is None
            or current.billing_period_start is None
            or current.billing_period_end is None
        ):
            _error(
                "not_actionable",
                "Actionable proforma preview lacks exact documentary evidence.",
            )

        reservation = IdempotencyKey(
            scope=_PROFORMA_ADOPTION_IDEMPOTENCY_SCOPE,
            key=key,
            account_id=current.account_id,
            ref_id=_proforma_adoption_ref(
                current.invoice_id,
                current.fingerprint,
            ),
        )
        db.add(reservation)
        try:
            db.flush()
        except IntegrityError:
            _error(
                "idempotency_conflict",
                "Idempotency key was concurrently reserved by another command.",
            )

        offer_name = (
            locked_subscription.offer.name
            if locked_subscription.offer is not None
            else "Prepaid service"
        )
        try:
            changed_invoice = Invoices.adopt_prepaid_proforma_document_for_owner(
                db,
                PrepaidProformaDocumentAdoption(
                    invoice_id=current.invoice_id,
                    line_id=current.line_id,
                    subscription_id=current.subscription_id,
                    billing_period_start=current.billing_period_start,
                    billing_period_end=current.billing_period_end,
                    line_description=f"{offer_name} prepaid service",
                    adoption_evidence_ref=(f"{_OWNER}:{current.fingerprint}"),
                ),
            )
        except InvoiceOwnerError as exc:
            _error(
                "participant_rejected",
                "Invoice owner rejected the reviewed proforma adoption.",
                participant_error=exc.code,
            )

        metadata = dict(changed_invoice.metadata_ or {})
        metadata[_PROFORMA_ADOPTION_METADATA_KEY] = {
            "subscription_id": str(current.subscription_id),
            "line_id": str(current.line_id),
            "settlement_payment_id": str(current.settlement_payment_id),
            "settlement_effective_at": current.settlement_effective_at.isoformat(),
            "billing_period_start": current.billing_period_start.isoformat(),
            "billing_period_end": current.billing_period_end.isoformat(),
            "preview_fingerprint": current.fingerprint,
            "idempotency_key": key,
            "command_id": str(command.context.command_id),
            "adopted_at": datetime.now(UTC).isoformat(),
        }
        changed_invoice.metadata_ = metadata
        AuditEvents.stage(
            db,
            AuditEventCreate(
                action="adopt_funded_prepaid_proforma",
                entity_type="invoice",
                entity_id=str(changed_invoice.id),
                metadata_={
                    "subscription_id": str(current.subscription_id),
                    "line_id": str(current.line_id),
                    "settlement_payment_id": str(current.settlement_payment_id),
                    "settlement_effective_at": (
                        current.settlement_effective_at.isoformat()
                    ),
                    "billing_period_start": current.billing_period_start.isoformat(),
                    "billing_period_end": current.billing_period_end.isoformat(),
                    "preview_fingerprint": current.fingerprint,
                    "financial_effect": "documentary_identity_only",
                },
            ),
        )
        emit_event(
            db,
            EventType.prepaid_proforma_adopted,
            {
                "invoice_id": str(changed_invoice.id),
                "invoice_number": changed_invoice.invoice_number,
                "subscription_id": str(current.subscription_id),
                "line_id": str(current.line_id),
                "settlement_payment_id": str(current.settlement_payment_id),
                "settlement_effective_at": (
                    current.settlement_effective_at.isoformat()
                ),
                "billing_period_start": current.billing_period_start.isoformat(),
                "billing_period_end": current.billing_period_end.isoformat(),
                "currency": current.currency,
                "invoice_total": str(current.invoice_total),
                "preview_fingerprint": current.fingerprint,
            },
            account_id=changed_invoice.account_id,
            invoice_id=changed_invoice.id,
        )
        db.flush()
        return PrepaidProformaAdoptionResult(
            invoice_id=changed_invoice.id,
            subscription_id=current.subscription_id,
            line_id=current.line_id,
            settlement_payment_id=current.settlement_payment_id,
            settlement_effective_at=current.settlement_effective_at,
            billing_period_start=current.billing_period_start,
            billing_period_end=current.billing_period_end,
            preview_fingerprint=current.fingerprint,
            replayed=False,
        )

    return execute_owner_command(
        db,
        definition=_PROFORMA_ADOPTION_COMMAND,
        context=command.context,
        operation=operation,
    )


def _replay_paid_invoice_repair_result(
    db: Session,
    *,
    command: RepairHistoricalPaidPrepaidInvoiceCommand,
    reservation: IdempotencyKey,
) -> PaidPrepaidInvoiceRepairResult:
    if reservation.ref_id != str(command.invoice_id):
        _error("idempotency_conflict", "Idempotency key belongs to another invoice.")
    invoice = db.get(Invoice, command.invoice_id, populate_existing=True)
    if (
        invoice is None
        or reservation.account_id != invoice.account_id
        or invoice.billing_period_start is None
        or invoice.billing_period_end is None
    ):
        _error("idempotency_conflict", "Paid invoice repair evidence is incomplete.")
    evidence = _paid_invoice_repair_structural_evidence(
        db,
        invoice=invoice,
        subscription_id=command.subscription_id,
    )
    consequence = db.scalar(
        select(FinancialAccessConsequence).where(
            FinancialAccessConsequence.idempotency_key
            == _paid_invoice_repair_access_key(command.context.idempotency_key or "")
        )
    )
    if (
        evidence is None
        or consequence is None
        or consequence.account_id != invoice.account_id
        or consequence.action is not FinancialAccessAction.restore
        or consequence.origin is not FinancialAccessOrigin.prepaid_enforcement
    ):
        _error("idempotency_conflict", "Paid invoice repair evidence is incomplete.")
    provenance = dict(evidence.entitlement.metadata_ or {})
    original_fingerprint = provenance.get("reconciliation_fingerprint")
    subscriptions_restored = consequence.result.get("subscriptions_changed")
    if (
        not isinstance(original_fingerprint, str)
        or original_fingerprint != command.preview_fingerprint
        or not isinstance(subscriptions_restored, int)
        or subscriptions_restored < 0
    ):
        _error(
            "idempotency_conflict",
            "Paid invoice repair evidence cannot be replayed.",
        )
    return PaidPrepaidInvoiceRepairResult(
        invoice_id=invoice.id,
        subscription_id=command.subscription_id,
        line_id=evidence.line.id,
        allocation_id=evidence.allocation.id,
        settlement_id=evidence.settlement.id,
        payment_id=evidence.payment.id,
        entitlement_id=evidence.entitlement.id,
        access_consequence_id=consequence.id,
        billing_period_start=_utc(invoice.billing_period_start),
        billing_period_end=_utc(invoice.billing_period_end),
        preview_fingerprint=original_fingerprint,
        subscriptions_restored=subscriptions_restored,
        replayed=True,
    )


def repair_historical_paid_prepaid_invoice(
    db: Session,
    command: RepairHistoricalPaidPrepaidInvoiceCommand,
) -> PaidPrepaidInvoiceRepairResult:
    """Repair one exact already-paid prepaid invoice and its access projection."""

    def operation() -> PaidPrepaidInvoiceRepairResult:
        key = (command.context.idempotency_key or "").strip()
        if not key or len(key) > 120:
            _error("missing_idempotency_key", "A bounded idempotency key is required.")
        invoice = db.get(Invoice, command.invoice_id)
        if invoice is None:
            _error("invoice_not_found", "Invoice was not found.")
        lock_account(db, str(invoice.account_id))
        reservation = db.scalar(
            select(IdempotencyKey)
            .where(
                IdempotencyKey.scope == _PAID_INVOICE_REPAIR_IDEMPOTENCY_SCOPE,
                IdempotencyKey.key == key,
            )
            .with_for_update()
        )
        if reservation is not None:
            return _replay_paid_invoice_repair_result(
                db,
                command=command,
                reservation=reservation,
            )

        locked_invoice = lock_for_update(db, Invoice, str(command.invoice_id))
        locked_subscription = lock_for_update(
            db,
            Subscription,
            str(command.subscription_id),
        )
        if locked_invoice is None:
            _error("invoice_not_found", "Invoice was not found.")
        if locked_subscription is None:
            _error("not_actionable", "Reviewed subscription was not found.")

        allocations = list(
            db.scalars(
                select(PaymentAllocation)
                .where(PaymentAllocation.invoice_id == command.invoice_id)
                .order_by(PaymentAllocation.id)
                .with_for_update()
            ).all()
        )
        payment_ids = sorted({row.payment_id for row in allocations}, key=str)
        if payment_ids:
            list(
                db.scalars(
                    select(Payment)
                    .where(Payment.id.in_(payment_ids))
                    .order_by(Payment.id)
                    .with_for_update()
                ).all()
            )
            list(
                db.scalars(
                    select(PaymentSettlement)
                    .where(PaymentSettlement.payment_id.in_(payment_ids))
                    .order_by(PaymentSettlement.id)
                    .with_for_update()
                ).all()
            )
            list(
                db.scalars(
                    select(PaymentRefund)
                    .where(PaymentRefund.payment_id.in_(payment_ids))
                    .order_by(PaymentRefund.id)
                    .with_for_update()
                ).all()
            )
            list(
                db.scalars(
                    select(PaymentReversal)
                    .where(PaymentReversal.payment_id.in_(payment_ids))
                    .order_by(PaymentReversal.id)
                    .with_for_update()
                ).all()
            )

        current = preview_historical_paid_prepaid_invoice_repair(
            db,
            PaidPrepaidInvoiceRepairQuery(
                invoice_id=command.invoice_id,
                subscription_id=command.subscription_id,
            ),
        )
        if current.fingerprint != command.preview_fingerprint:
            _error(
                "stale_preview",
                "Paid invoice evidence changed after preview; preview again.",
            )
        if not current.actionable:
            _error(
                "not_actionable",
                "This paid invoice requires manual evidence review.",
                disposition=current.disposition.value,
                reason=current.reason,
            )
        if (
            current.line_id is None
            or current.allocation_id is None
            or current.settlement_id is None
            or current.payment_id is None
            or current.settlement_effective_at is None
            or current.billing_period_start is None
            or current.billing_period_end is None
        ):
            _error("not_actionable", "Actionable repair lacks exact evidence IDs.")

        reservation = IdempotencyKey(
            scope=_PAID_INVOICE_REPAIR_IDEMPOTENCY_SCOPE,
            key=key,
            account_id=current.account_id,
            ref_id=str(current.invoice_id),
        )
        db.add(reservation)
        try:
            db.flush()
        except IntegrityError:
            _error(
                "idempotency_conflict",
                "Idempotency key was concurrently reserved by another command.",
            )

        offer_name = (
            locked_subscription.offer.name
            if locked_subscription.offer is not None
            else "Prepaid service"
        )
        try:
            changed_invoice = Invoices.repair_paid_prepaid_document_for_owner(
                db,
                PaidPrepaidInvoiceDocumentRepair(
                    invoice_id=current.invoice_id,
                    line_id=current.line_id,
                    subscription_id=current.subscription_id,
                    billing_period_start=current.billing_period_start,
                    billing_period_end=current.billing_period_end,
                    line_description=f"{offer_name} prepaid service",
                    repair_evidence_ref=f"{_OWNER}:{current.fingerprint}",
                ),
            )
        except InvoiceOwnerError as exc:
            _error(
                "participant_rejected",
                "Invoice owner rejected the reviewed paid-document repair.",
                participant_error=exc.code,
            )

        line = db.get(InvoiceLine, current.line_id)
        if line is None:
            _error("incomplete_repair", "Repaired invoice line disappeared.")
        from app.services.service_entitlements import (
            ensure_prepaid_entitlement_for_paid_invoice_line,
        )

        entitlement = ensure_prepaid_entitlement_for_paid_invoice_line(
            db,
            invoice=changed_invoice,
            line=line,
            reconciliation_fingerprint=current.fingerprint,
        )
        if entitlement is None:
            _error(
                "incomplete_repair",
                "Reviewed paid invoice did not produce an exact entitlement.",
            )
        from app.services.prepaid_service_renewals import (
            BillingAnchorAuthority,
            project_prepaid_billing_anchor_for_invoice,
        )

        projections = project_prepaid_billing_anchor_for_invoice(
            db,
            changed_invoice,
            evidence_ref=f"paid_prepaid_invoice_repair:{changed_invoice.id}",
            authority=BillingAnchorAuthority.reviewed_reconciliation,
        )
        projection = next(
            (
                item
                for item in projections
                if item.subscription_id == current.subscription_id
            ),
            None,
        )
        if (
            projection is None
            or projection.next_billing_at != current.billing_period_end
        ):
            _error(
                "incomplete_repair",
                "Billing anchor does not match repaired entitlement evidence.",
            )

        from app.services.collections import (
            FinancialAccessRestorationParticipantCommand,
            FinancialAccessRestorationParticipantError,
            confirm_financial_access_restoration_for_owner,
        )

        try:
            restoration = confirm_financial_access_restoration_for_owner(
                db,
                FinancialAccessRestorationParticipantCommand(
                    account_id=current.account_id,
                    origin=FinancialAccessOrigin.prepaid_enforcement,
                    idempotency_key=_paid_invoice_repair_access_key(key),
                    invoice_id=changed_invoice.id,
                    resolved_by=f"{_OWNER}:{command.context.actor}",
                ),
            )
        except FinancialAccessRestorationParticipantError as exc:
            _error(
                "participant_rejected",
                "Financial access owner rejected restoration after repair.",
                participant_error=exc.code,
            )

        metadata = dict(changed_invoice.metadata_ or {})
        metadata[_PAID_INVOICE_REPAIR_METADATA_KEY] = {
            "subscription_id": str(current.subscription_id),
            "line_id": str(current.line_id),
            "allocation_id": str(current.allocation_id),
            "settlement_id": str(current.settlement_id),
            "payment_id": str(current.payment_id),
            "entitlement_id": str(entitlement.id),
            "access_consequence_id": str(restoration.consequence.id),
            "subscriptions_restored": restoration.subscriptions_changed,
            "settlement_effective_at": current.settlement_effective_at.isoformat(),
            "billing_period_start": current.billing_period_start.isoformat(),
            "billing_period_end": current.billing_period_end.isoformat(),
            "preview_fingerprint": current.fingerprint,
            "idempotency_key": key,
            "command_id": str(command.context.command_id),
            "repaired_at": datetime.now(UTC).isoformat(),
        }
        changed_invoice.metadata_ = metadata
        AuditEvents.stage(
            db,
            AuditEventCreate(
                action="repair_historical_paid_prepaid_invoice",
                entity_type="invoice",
                entity_id=str(changed_invoice.id),
                metadata_={
                    "subscription_id": str(current.subscription_id),
                    "line_id": str(current.line_id),
                    "allocation_id": str(current.allocation_id),
                    "settlement_id": str(current.settlement_id),
                    "payment_id": str(current.payment_id),
                    "entitlement_id": str(entitlement.id),
                    "access_consequence_id": str(restoration.consequence.id),
                    "subscriptions_restored": restoration.subscriptions_changed,
                    "billing_period_start": current.billing_period_start.isoformat(),
                    "billing_period_end": current.billing_period_end.isoformat(),
                    "preview_fingerprint": current.fingerprint,
                    "economic_delta": "0.00",
                },
            ),
        )
        emit_event(
            db,
            EventType.prepaid_paid_invoice_repaired,
            {
                "invoice_id": str(changed_invoice.id),
                "invoice_number": changed_invoice.invoice_number,
                "subscription_id": str(current.subscription_id),
                "line_id": str(current.line_id),
                "allocation_id": str(current.allocation_id),
                "settlement_id": str(current.settlement_id),
                "payment_id": str(current.payment_id),
                "entitlement_id": str(entitlement.id),
                "access_consequence_id": str(restoration.consequence.id),
                "subscriptions_restored": restoration.subscriptions_changed,
                "billing_period_start": current.billing_period_start.isoformat(),
                "billing_period_end": current.billing_period_end.isoformat(),
                "currency": current.currency,
                "invoice_total": str(current.invoice_total),
                "economic_delta": "0.00",
                "preview_fingerprint": current.fingerprint,
            },
            account_id=changed_invoice.account_id,
            invoice_id=changed_invoice.id,
        )
        db.flush()
        return PaidPrepaidInvoiceRepairResult(
            invoice_id=changed_invoice.id,
            subscription_id=current.subscription_id,
            line_id=current.line_id,
            allocation_id=current.allocation_id,
            settlement_id=current.settlement_id,
            payment_id=current.payment_id,
            entitlement_id=entitlement.id,
            access_consequence_id=restoration.consequence.id,
            billing_period_start=current.billing_period_start,
            billing_period_end=current.billing_period_end,
            preview_fingerprint=current.fingerprint,
            subscriptions_restored=restoration.subscriptions_changed,
            replayed=False,
        )

    return execute_owner_command(
        db,
        definition=_PAID_INVOICE_REPAIR_COMMAND,
        context=command.context,
        operation=operation,
    )


def reconcile_prepaid_draft_invoice(
    db: Session,
    command: ReconcilePrepaidDraftCommand,
) -> PrepaidDraftReconciliationResult:
    """Confirm one reviewed, actionable draft reconciliation atomically."""

    def operation() -> PrepaidDraftReconciliationResult:
        key = (command.context.idempotency_key or "").strip()
        if not key or len(key) > 120:
            _error(
                "missing_idempotency_key",
                "A bounded idempotency key is required.",
            )
        invoice = db.get(Invoice, command.invoice_id)
        if invoice is None:
            _error("invoice_not_found", "Invoice was not found.")
        lock_account(db, str(invoice.account_id))
        reservation = db.scalar(
            select(IdempotencyKey)
            .where(
                IdempotencyKey.scope == _IDEMPOTENCY_SCOPE,
                IdempotencyKey.key == key,
            )
            .with_for_update()
        )
        if reservation is not None:
            return _replay_result(db, command=command, reservation=reservation)

        locked = lock_for_update(db, Invoice, str(invoice.id))
        if locked is None:
            _error("invoice_not_found", "Invoice was not found.")
        current = preview_prepaid_draft_reconciliation(db, locked.id)
        if current.fingerprint != command.preview_fingerprint:
            _error(
                "stale_preview",
                "Draft evidence changed after preview; preview again.",
            )
        if not current.actionable:
            _error(
                "not_actionable",
                "This draft requires more funding or manual evidence review.",
                disposition=current.disposition.value,
                shortfall=str(current.shortfall),
            )

        reservation = IdempotencyKey(
            scope=_IDEMPOTENCY_SCOPE,
            key=key,
            account_id=current.account_id,
            ref_id=str(current.invoice_id),
        )
        db.add(reservation)
        try:
            db.flush()
        except IntegrityError:
            _error(
                "idempotency_conflict",
                "Idempotency key was concurrently reserved by another command.",
            )
        changed_invoice, applied, payment_applied, opening_consumption = _stage_action(
            db,
            preview=current,
            effective_at=command.effective_at,
            context=command.context,
        )
        _resolve_review_exception(db, changed_invoice.id)
        return PrepaidDraftReconciliationResult(
            invoice_id=changed_invoice.id,
            disposition=current.disposition,
            action=current.recommended_action,
            final_status=changed_invoice.status,
            applied_amount=applied,
            payment_applied_amount=payment_applied,
            opening_funding_applied_amount=round_money(
                to_decimal(
                    opening_consumption.amount
                    if opening_consumption is not None
                    else Decimal("0.00")
                )
            ),
            opening_funding_consumption_id=(
                opening_consumption.id if opening_consumption is not None else None
            ),
            preview_fingerprint=current.fingerprint,
            replayed=False,
        )

    return execute_owner_command(
        db,
        definition=_COMMAND,
        context=command.context,
        operation=operation,
    )


def stage_prepaid_draft_after_funding_change(
    db: Session,
    *,
    account_id: UUID,
    currency: str,
    effective_at: datetime,
) -> FundingChangeDraftResult:
    """Settle one exact existing draft before any invoice-less renewal.

    This is a flush-only participant for the existing funding-change
    transaction. Any draft, including an underfunded one, blocks the parallel
    direct-renewal path. Multiple drafts are intentionally left for reviewed
    reconciliation.
    """

    lock_account(db, str(account_id))
    invoice_ids = tuple(
        dict.fromkeys(
            db.scalars(
                select(Invoice.id)
                .join(InvoiceLine, InvoiceLine.invoice_id == Invoice.id)
                .join(Subscription, Subscription.id == InvoiceLine.subscription_id)
                .where(
                    Invoice.account_id == account_id,
                    Invoice.is_active.is_(True),
                    Invoice.status == InvoiceStatus.draft,
                    Invoice.is_proforma.is_(False),
                    Invoice.balance_due > Decimal("0.00"),
                    Invoice.currency == currency,
                    InvoiceLine.is_active.is_(True),
                    InvoiceLine.amount > Decimal("0.00"),
                    Subscription.billing_mode == BillingMode.prepaid,
                )
                .order_by(Invoice.created_at, Invoice.id)
            ).all()
        )
    )
    if not invoice_ids:
        return FundingChangeDraftResult(0, 0, 0, 0, ())
    if len(invoice_ids) != 1:
        return FundingChangeDraftResult(
            len(invoice_ids),
            0,
            len(invoice_ids),
            0,
            invoice_ids,
        )

    preview = preview_prepaid_draft_reconciliation(db, invoice_ids[0])
    if preview.disposition is PrepaidDraftDisposition.reviewed_opening_fundable:
        _stage_review_exception(db, preview=preview)
        return FundingChangeDraftResult(1, 0, 1, 1, invoice_ids)
    if preview.recommended_action is not PrepaidDraftAction.settle_paid:
        return FundingChangeDraftResult(1, 0, 1, 0, invoice_ids)
    invoice, _applied, _payment_applied, _opening_consumption = _stage_action(
        db,
        preview=preview,
        effective_at=effective_at,
        context=None,
    )
    if invoice.status != InvoiceStatus.paid:
        _error(
            "incomplete_repair",
            "Funding-change draft settlement did not produce a paid invoice.",
        )
    return FundingChangeDraftResult(1, 1, 0, 0, invoice_ids)


__all__ = [
    "AdoptFundedPrepaidProformaCommand",
    "FundingChangeDraftResult",
    "PrepaidDraftAction",
    "PrepaidDraftDisposition",
    "PrepaidDraftReconciliationError",
    "PrepaidDraftReconciliationPreview",
    "PrepaidDraftReconciliationResult",
    "PaidPrepaidInvoiceRepairDisposition",
    "PaidPrepaidInvoiceRepairPreview",
    "PaidPrepaidInvoiceRepairQuery",
    "PaidPrepaidInvoiceRepairResult",
    "PrepaidProformaAdoptionDisposition",
    "PrepaidProformaAdoptionPreview",
    "PrepaidProformaAdoptionQuery",
    "PrepaidProformaAdoptionResult",
    "ReconcilePrepaidDraftCommand",
    "RepairHistoricalPaidPrepaidInvoiceCommand",
    "adopt_funded_prepaid_proforma",
    "preview_prepaid_draft_cohort",
    "preview_prepaid_draft_reconciliation",
    "preview_funded_prepaid_proforma_adoption",
    "preview_historical_paid_prepaid_invoice_repair",
    "reconcile_prepaid_draft_invoice",
    "repair_historical_paid_prepaid_invoice",
    "stage_prepaid_draft_after_funding_change",
]
