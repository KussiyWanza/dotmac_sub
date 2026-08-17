"""Evidence-first owner for stranded prepaid draft invoices.

Preview is read-only and classifies one exact invoice. Confirmation locks the
account and invoice, recomputes the preview fingerprint, and performs one safe
repair:

* exact native payment-backed funding issues and fully settles the draft; or
* settlement-backed payments plus reviewed opening funding settle the exact
  remainder without representing that opening source as a Payment; or
* one entity-scoped reviewed correction creates and settles a missing prepaid
  invoice from exact contract, payment, date, and residual-credit evidence; or
* an exact direct-renewal debit/entitlement voids the duplicate draft without
  charging the customer again.

Automatic mixed-source discovery creates a durable operator exception.
Insufficient funding, legacy/unbacked credit, multiple drafts, mixed invoices,
and ambiguous coverage otherwise remain unchanged and fail closed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time
from decimal import Decimal
from enum import StrEnum
from typing import NoReturn
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.billing import (
    AccountAdjustment,
    CreditNoteApplication,
    Invoice,
    InvoiceDueDateBasis,
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
from app.models.customer_subledger import (
    CustomerPositionEffect,
    CustomerPostingGroup,
    CustomerSubledgerAuthorityCutover,
    CustomerSubledgerOpeningPosition,
    PositionEffectKind,
    PostingCommandKind,
    PostingProducer,
    PostingSourceKind,
)
from app.models.idempotency import IdempotencyKey
from app.models.prepaid_funding import (
    PrepaidDraftReconciliationException,
    PrepaidFundingBaseline,
    PrepaidOpeningFundingConsumption,
)
from app.schemas.audit import AuditEventCreate
from app.schemas.billing import (
    InvoiceCreate,
    LedgerEntryCreate,
    SystemInvoiceLineCreate,
)
from app.services.audit import AuditEvents
from app.services.billing._common import get_account_credit_balance, lock_account
from app.services.billing.account_credit import (
    AccountCreditApplicationError,
    AccountCreditApplications,
    AccountCreditInvoiceFundingPreview,
    ReviewedOpeningSettlementAllocationRelease,
)
from app.services.billing.adjustments import AccountAdjustmentOrigin
from app.services.billing.customer_subledger import (
    StageReversalCommand,
    resolve_position,
    stage_reversal,
)
from app.services.billing.invoices import (
    InvoiceIssuanceInput,
    InvoiceLines,
    InvoiceOwnerError,
    Invoices,
    PaidPrepaidInvoiceDocumentRepair,
    PrepaidProformaDocumentAdoption,
)
from app.services.billing.ledger import LedgerEntries
from app.services.billing.payments import (
    PaymentAllocations,
    finalize_invoice_application_for_owner,
    finalize_reviewed_document_settlement_for_owner,
)
from app.services.common import round_money, to_decimal
from app.services.customer_financial_ledger import (
    native_customer_financial_balances_by_currency,
)
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
from app.timezone import APP_TIMEZONE_NAME

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
_MISSING_PAID_INVOICE_REPAIR_COMMAND = OwnerCommandDefinition(
    owner=_OWNER,
    concern="reviewed missing prepaid paid-invoice repair",
    name="create_reviewed_paid_prepaid_invoice",
)
_OPENING_SETTLEMENT_CORRECTION_COMMAND = OwnerCommandDefinition(
    owner=_OWNER,
    concern="reviewed pre-opening invoice settlement correction",
    name="reconcile_preopening_invoice_settlement",
)
_IDEMPOTENCY_SCOPE = "prepaid_draft_reconcile"
_PROFORMA_ADOPTION_IDEMPOTENCY_SCOPE = "prepaid_proforma_adoption"
_PAID_INVOICE_REPAIR_IDEMPOTENCY_SCOPE = "paid_prepaid_invoice_repair"
_MISSING_PAID_INVOICE_REPAIR_IDEMPOTENCY_SCOPE = "missing_paid_prepaid_invoice_repair"
_OPENING_SETTLEMENT_IDEMPOTENCY_SCOPE = "preopening_invoice_settlement_correction"
_METADATA_KEY = "prepaid_draft_reconciliation"
_PROFORMA_ADOPTION_METADATA_KEY = "prepaid_proforma_adoption"
_PAID_INVOICE_REPAIR_METADATA_KEY = "paid_prepaid_invoice_repair"
_MISSING_PAID_INVOICE_REPAIR_METADATA_KEY = "missing_paid_prepaid_invoice_repair"
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


class MissingPaidPrepaidInvoiceRepairDisposition(StrEnum):
    exact_missing_invoice = "exact_missing_invoice"
    manual_review = "manual_review"
    already_repaired = "already_repaired"


class OpeningSettlementCorrectionDisposition(StrEnum):
    exact_preopening_double_application = "exact_preopening_double_application"
    manual_review = "manual_review"
    already_reconciled = "already_reconciled"


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
class FundingChangeDraftCommand:
    """Typed funding observation for automatic prepaid-draft settlement."""

    account_id: UUID
    currency: str
    effective_at: datetime
    evidence_ref: str


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
class MissingPaidPrepaidInvoiceRepairQuery:
    account_id: UUID
    subscription_id: UUID
    payment_id: UUID
    issued_on: date
    due_on: date
    next_billing_on: date
    expected_total: Decimal
    expected_remaining_credit: Decimal


@dataclass(frozen=True, slots=True)
class MissingPaidPrepaidInvoiceRepairPreview:
    account_id: UUID
    subscription_id: UUID
    payment_id: UUID
    disposition: MissingPaidPrepaidInvoiceRepairDisposition
    issued_at: datetime | None
    paid_at: datetime | None
    due_at: datetime | None
    billing_period_start: datetime | None
    billing_period_end: datetime | None
    subtotal: Decimal
    tax_total: Decimal
    total: Decimal
    currency: str
    account_credit_before: Decimal
    expected_remaining_credit: Decimal
    selected_payment_available: Decimal
    existing_invoice_id: UUID | None
    reason: str
    fingerprint: str

    @property
    def actionable(self) -> bool:
        return (
            self.disposition
            is MissingPaidPrepaidInvoiceRepairDisposition.exact_missing_invoice
        )


@dataclass(frozen=True, slots=True)
class CreateReviewedPaidPrepaidInvoiceCommand:
    context: CommandContext
    query: MissingPaidPrepaidInvoiceRepairQuery
    preview_fingerprint: str


@dataclass(frozen=True, slots=True)
class MissingPaidPrepaidInvoiceRepairResult:
    invoice_id: UUID
    invoice_number: str | None
    subscription_id: UUID
    payment_id: UUID
    entitlement_id: UUID
    issued_at: datetime
    paid_at: datetime
    due_at: datetime
    billing_period_start: datetime
    billing_period_end: datetime
    total: Decimal
    remaining_credit: Decimal
    preview_fingerprint: str
    replayed: bool


@dataclass(frozen=True, slots=True)
class OpeningSettlementCorrectionQuery:
    invoice_id: UUID
    allocation_id: UUID
    expected_confirmed_balance: Decimal


@dataclass(frozen=True, slots=True)
class OpeningSettlementCorrectionPreview:
    invoice_id: UUID
    account_id: UUID
    invoice_number: str | None
    allocation_id: UUID
    payment_id: UUID | None
    opening_position_id: UUID | None
    baseline_id: UUID | None
    original_posting_group_id: UUID | None
    disposition: OpeningSettlementCorrectionDisposition
    currency: str
    invoice_total: Decimal
    balance_due: Decimal
    allocated_amount: Decimal
    confirmed_balance: Decimal
    subledger_credit_before: Decimal
    subledger_receivable_before: Decimal
    opening_settlement_amount: Decimal
    reason: str
    fingerprint: str

    @property
    def actionable(self) -> bool:
        return (
            self.disposition
            is OpeningSettlementCorrectionDisposition.exact_preopening_double_application
        )


@dataclass(frozen=True, slots=True)
class ReconcileOpeningSettlementCorrectionCommand:
    context: CommandContext
    query: OpeningSettlementCorrectionQuery
    preview_fingerprint: str
    effective_at: datetime


@dataclass(frozen=True, slots=True)
class OpeningSettlementCorrectionResult:
    invoice_id: UUID
    allocation_id: UUID
    opening_funding_consumption_id: UUID
    customer_posting_reversal_id: UUID
    ledger_reversal_ids: tuple[UUID, ...]
    final_status: InvoiceStatus
    final_balance_due: Decimal
    confirmed_balance: Decimal
    subledger_credit_after: Decimal
    subledger_receivable_after: Decimal
    preview_fingerprint: str
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


def _business_midnight(value: date) -> datetime:
    return datetime.combine(
        value,
        time.min,
        tzinfo=ZoneInfo(APP_TIMEZONE_NAME),
    ).astimezone(UTC)


def _build_missing_paid_invoice_preview(
    *,
    query: MissingPaidPrepaidInvoiceRepairQuery,
    disposition: MissingPaidPrepaidInvoiceRepairDisposition,
    reason: str,
    issued_at: datetime | None = None,
    paid_at: datetime | None = None,
    due_at: datetime | None = None,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
    subtotal: Decimal = Decimal("0.00"),
    tax_total: Decimal = Decimal("0.00"),
    total: Decimal = Decimal("0.00"),
    currency: str = "NGN",
    account_credit_before: Decimal = Decimal("0.00"),
    selected_payment_available: Decimal = Decimal("0.00"),
    existing_invoice_id: UUID | None = None,
    evidence: dict[str, object] | None = None,
    fingerprint_override: str | None = None,
) -> MissingPaidPrepaidInvoiceRepairPreview:
    payload: dict[str, object] = {
        "account_id": query.account_id,
        "subscription_id": query.subscription_id,
        "payment_id": query.payment_id,
        "issued_on": query.issued_on,
        "due_on": query.due_on,
        "next_billing_on": query.next_billing_on,
        "expected_total": round_money(query.expected_total),
        "expected_remaining_credit": round_money(query.expected_remaining_credit),
        "issued_at": issued_at,
        "paid_at": paid_at,
        "due_at": due_at,
        "period_start": period_start,
        "period_end": period_end,
        "subtotal": round_money(subtotal),
        "tax_total": round_money(tax_total),
        "total": round_money(total),
        "currency": currency,
        "account_credit_before": round_money(account_credit_before),
        "selected_payment_available": round_money(selected_payment_available),
        "existing_invoice_id": existing_invoice_id,
        "disposition": disposition,
        "reason": reason,
        "evidence": evidence or {},
    }
    return MissingPaidPrepaidInvoiceRepairPreview(
        account_id=query.account_id,
        subscription_id=query.subscription_id,
        payment_id=query.payment_id,
        disposition=disposition,
        issued_at=issued_at,
        paid_at=paid_at,
        due_at=due_at,
        billing_period_start=period_start,
        billing_period_end=period_end,
        subtotal=round_money(subtotal),
        tax_total=round_money(tax_total),
        total=round_money(total),
        currency=currency,
        account_credit_before=round_money(account_credit_before),
        expected_remaining_credit=round_money(query.expected_remaining_credit),
        selected_payment_available=round_money(selected_payment_available),
        existing_invoice_id=existing_invoice_id,
        reason=reason,
        fingerprint=fingerprint_override or _hash(payload),
    )


def _existing_missing_paid_invoice_repair(
    db: Session,
    query: MissingPaidPrepaidInvoiceRepairQuery,
) -> tuple[Invoice, str] | None:
    invoices = tuple(
        db.scalars(
            select(Invoice)
            .join(InvoiceLine, InvoiceLine.invoice_id == Invoice.id)
            .join(
                PaymentAllocation,
                PaymentAllocation.invoice_id == Invoice.id,
            )
            .where(
                Invoice.account_id == query.account_id,
                Invoice.is_active.is_(True),
                InvoiceLine.subscription_id == query.subscription_id,
                InvoiceLine.is_active.is_(True),
                PaymentAllocation.payment_id == query.payment_id,
                PaymentAllocation.is_active.is_(True),
            )
            .distinct()
            .order_by(Invoice.created_at, Invoice.id)
        ).all()
    )
    for invoice in invoices:
        metadata = dict(invoice.metadata_ or {}).get(
            _MISSING_PAID_INVOICE_REPAIR_METADATA_KEY
        )
        if not isinstance(metadata, dict):
            continue
        if (
            str(metadata.get("issued_on")) == query.issued_on.isoformat()
            and str(metadata.get("due_on")) == query.due_on.isoformat()
            and str(metadata.get("next_billing_on"))
            == query.next_billing_on.isoformat()
            and str(metadata.get("expected_total"))
            == str(round_money(query.expected_total))
            and str(metadata.get("expected_remaining_credit"))
            == str(round_money(query.expected_remaining_credit))
        ):
            fingerprint = str(metadata.get("preview_fingerprint") or "")
            if fingerprint:
                selected_allocation = round_money(
                    to_decimal(
                        db.scalar(
                            select(
                                func.coalesce(func.sum(PaymentAllocation.amount), 0)
                            ).where(
                                PaymentAllocation.invoice_id == invoice.id,
                                PaymentAllocation.payment_id == query.payment_id,
                                PaymentAllocation.is_active.is_(True),
                            )
                        )
                    )
                )
                entitlement_id = db.scalar(
                    select(ServiceEntitlement.id).where(
                        ServiceEntitlement.source_invoice_id == invoice.id,
                        ServiceEntitlement.subscription_id == query.subscription_id,
                        ServiceEntitlement.status == ServiceEntitlementStatus.active,
                    )
                )
                subscription = db.get(Subscription, query.subscription_id)
                if (
                    invoice.status is not InvoiceStatus.paid
                    or round_money(to_decimal(invoice.balance_due)) != Decimal("0.00")
                    or selected_allocation != round_money(query.expected_total)
                    or entitlement_id is None
                    or subscription is None
                    or invoice.billing_period_end is None
                    or subscription.next_billing_at is None
                    or _utc(subscription.next_billing_at)
                    != _utc(invoice.billing_period_end)
                ):
                    _error(
                        "incomplete_repair",
                        "Existing missing-invoice repair evidence has drifted.",
                        invoice_id=str(invoice.id),
                    )
                return invoice, fingerprint
    return None


def preview_missing_paid_prepaid_invoice_repair(
    db: Session,
    query: MissingPaidPrepaidInvoiceRepairQuery,
) -> MissingPaidPrepaidInvoiceRepairPreview:
    """Preview one explicit missing-invoice correction without writing money."""

    existing_repair = _existing_missing_paid_invoice_repair(db, query)
    if existing_repair is not None:
        invoice, fingerprint = existing_repair
        return _build_missing_paid_invoice_preview(
            query=query,
            disposition=MissingPaidPrepaidInvoiceRepairDisposition.already_repaired,
            reason="invoice carries structural reviewed missing-invoice evidence",
            issued_at=_utc(invoice.issued_at) if invoice.issued_at else None,
            paid_at=_utc(invoice.paid_at) if invoice.paid_at else None,
            due_at=_utc(invoice.due_at) if invoice.due_at else None,
            period_start=(
                _utc(invoice.billing_period_start)
                if invoice.billing_period_start
                else None
            ),
            period_end=(
                _utc(invoice.billing_period_end) if invoice.billing_period_end else None
            ),
            subtotal=to_decimal(invoice.subtotal),
            tax_total=to_decimal(invoice.tax_total),
            total=to_decimal(invoice.total),
            currency=(invoice.currency or "NGN").upper(),
            account_credit_before=get_account_credit_balance(
                db,
                str(query.account_id),
                currency=(invoice.currency or "NGN").upper(),
            ),
            existing_invoice_id=invoice.id,
            fingerprint_override=fingerprint,
        )

    expected_total = round_money(query.expected_total)
    expected_remaining = round_money(query.expected_remaining_credit)
    period_start = _business_midnight(query.issued_on)
    period_end = _business_midnight(query.next_billing_on)
    due_at = _business_midnight(query.due_on)
    if (
        expected_total <= Decimal("0.00")
        or expected_remaining < Decimal("0.00")
        or period_end <= period_start
        or query.due_on != query.next_billing_on
    ):
        return _build_missing_paid_invoice_preview(
            query=query,
            disposition=MissingPaidPrepaidInvoiceRepairDisposition.manual_review,
            reason="reviewed dates or expected monetary values are invalid",
            due_at=due_at,
            period_start=period_start,
            period_end=period_end,
        )

    subscription = db.get(Subscription, query.subscription_id)
    if (
        subscription is None
        or subscription.subscriber_id != query.account_id
        or subscription.billing_mode is not BillingMode.prepaid
        or subscription.status
        not in {
            SubscriptionStatus.active,
            SubscriptionStatus.suspended,
            SubscriptionStatus.blocked,
        }
    ):
        return _build_missing_paid_invoice_preview(
            query=query,
            disposition=MissingPaidPrepaidInvoiceRepairDisposition.manual_review,
            reason="subscription is not the selected eligible prepaid contract",
            due_at=due_at,
            period_start=period_start,
            period_end=period_end,
        )
    current_anchor = (
        _utc(subscription.next_billing_at)
        if subscription.next_billing_at is not None
        else None
    )
    if current_anchor is not None and current_anchor > period_start:
        return _build_missing_paid_invoice_preview(
            query=query,
            disposition=MissingPaidPrepaidInvoiceRepairDisposition.manual_review,
            reason="subscription already has a billing anchor inside the reviewed period",
            due_at=due_at,
            period_start=period_start,
            period_end=period_end,
            evidence={"current_next_billing_at": current_anchor},
        )

    payment = db.get(Payment, query.payment_id)
    settlement = payment.settlement if payment is not None else None
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
    local_zone = ZoneInfo(APP_TIMEZONE_NAME)
    payment_paid_at = (
        _utc(payment.paid_at)
        if payment is not None and payment.paid_at is not None
        else None
    )
    if (
        payment is None
        or not payment.is_active
        or payment.account_id != query.account_id
        or payment.status is not PaymentStatus.succeeded
        or payment_paid_at is None
        or payment_paid_at.astimezone(local_zone).date() != query.issued_on
        or settlement is None
        or settlement.payment_id != payment.id
        or has_return
    ):
        return _build_missing_paid_invoice_preview(
            query=query,
            disposition=MissingPaidPrepaidInvoiceRepairDisposition.manual_review,
            reason="selected payment is not successful unreturned settlement evidence",
            issued_at=payment_paid_at,
            paid_at=payment_paid_at,
            due_at=due_at,
            period_start=period_start,
            period_end=period_end,
        )

    from app.services.prepaid_service_renewals import (
        resolve_prepaid_monthly_charge_detail,
    )

    charge = resolve_prepaid_monthly_charge_detail(
        db,
        subscription,
        payment_paid_at,
    )
    if (
        charge is None
        or charge.total != expected_total
        or charge.currency != (payment.currency or "NGN").upper()
        or settlement.currency.upper() != charge.currency
    ):
        return _build_missing_paid_invoice_preview(
            query=query,
            disposition=MissingPaidPrepaidInvoiceRepairDisposition.manual_review,
            reason="expected total does not match canonical prepaid contract terms",
            issued_at=payment_paid_at,
            paid_at=payment_paid_at,
            due_at=due_at,
            period_start=period_start,
            period_end=period_end,
            total=expected_total,
        )

    account_credit = round_money(
        get_account_credit_balance(
            db,
            str(query.account_id),
            currency=charge.currency,
        )
    )
    selected_payment_available = round_money(
        PaymentAllocations.available_amount(db, str(payment.id))
    )
    if (
        account_credit != round_money(expected_total + expected_remaining)
        or selected_payment_available < expected_total
    ):
        return _build_missing_paid_invoice_preview(
            query=query,
            disposition=MissingPaidPrepaidInvoiceRepairDisposition.manual_review,
            reason="reviewed account credit or selected payment capacity changed",
            issued_at=payment_paid_at,
            paid_at=payment_paid_at,
            due_at=due_at,
            period_start=period_start,
            period_end=period_end,
            subtotal=charge.subtotal,
            tax_total=charge.tax_total,
            total=charge.total,
            currency=charge.currency,
            account_credit_before=account_credit,
            selected_payment_available=selected_payment_available,
        )

    competing_invoice_id = db.scalar(
        select(Invoice.id)
        .join(InvoiceLine, InvoiceLine.invoice_id == Invoice.id)
        .where(
            Invoice.account_id == query.account_id,
            Invoice.is_active.is_(True),
            Invoice.status.notin_({InvoiceStatus.void, InvoiceStatus.written_off}),
            InvoiceLine.subscription_id == subscription.id,
            InvoiceLine.is_active.is_(True),
            InvoiceLine.amount > Decimal("0.00"),
            or_(
                Invoice.billing_period_start.is_(None),
                Invoice.billing_period_end.is_(None),
                (
                    (Invoice.billing_period_start < period_end)
                    & (Invoice.billing_period_end > period_start)
                ),
            ),
        )
        .limit(1)
    )
    overlapping_entitlement_id = db.scalar(
        select(ServiceEntitlement.id)
        .where(
            ServiceEntitlement.subscription_id == subscription.id,
            ServiceEntitlement.status == ServiceEntitlementStatus.active,
            ServiceEntitlement.starts_at < period_end,
            ServiceEntitlement.ends_at > period_start,
        )
        .limit(1)
    )
    if competing_invoice_id is not None or overlapping_entitlement_id is not None:
        return _build_missing_paid_invoice_preview(
            query=query,
            disposition=MissingPaidPrepaidInvoiceRepairDisposition.manual_review,
            reason="competing invoice or service entitlement overlaps the reviewed period",
            issued_at=payment_paid_at,
            paid_at=payment_paid_at,
            due_at=due_at,
            period_start=period_start,
            period_end=period_end,
            subtotal=charge.subtotal,
            tax_total=charge.tax_total,
            total=charge.total,
            currency=charge.currency,
            account_credit_before=account_credit,
            selected_payment_available=selected_payment_available,
            existing_invoice_id=competing_invoice_id,
            evidence={"overlapping_entitlement_id": overlapping_entitlement_id},
        )

    return _build_missing_paid_invoice_preview(
        query=query,
        disposition=MissingPaidPrepaidInvoiceRepairDisposition.exact_missing_invoice,
        reason="one reviewed payment-backed correction can create the missing invoice",
        issued_at=payment_paid_at,
        paid_at=payment_paid_at,
        due_at=due_at,
        period_start=period_start,
        period_end=period_end,
        subtotal=charge.subtotal,
        tax_total=charge.tax_total,
        total=charge.total,
        currency=charge.currency,
        account_credit_before=account_credit,
        selected_payment_available=selected_payment_available,
        evidence={
            "subscription_updated_at": subscription.updated_at,
            "subscription_next_billing_at": current_anchor,
            "payment_updated_at": payment.updated_at,
            "settlement_id": settlement.id,
            "settlement_amount": settlement.amount,
            "settlement_unallocated_amount": settlement.unallocated_amount,
            "tax_rate_id": charge.tax_rate_id,
            "tax_application": charge.tax_application,
        },
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
    context: CommandContext | None,
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
    # Reviewed opening funding is already approved, fingerprinted financial
    # evidence. Funding-change consequences do not carry an operator command
    # context, so bind their consumption to the exact invoice/funding preview.
    # Interactive reconciliation keeps its caller-provided idempotency key.
    command_key = (
        context.idempotency_key
        if context is not None and context.idempotency_key
        else f"funding-change:{invoice.id}:{preview.fingerprint}"
    )
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
    due_at: datetime | None = None,
    reviewed_document_correction: bool = False,
    selected_payment_id: UUID | None = None,
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
                issuance=InvoiceIssuanceInput(
                    issued_at=_utc(effective_at),
                    due_at=_utc(due_at or effective_at),
                    due_date_basis=InvoiceDueDateBasis.prepaid_service_period,
                    due_date_basis_ref=(f"prepaid-draft-review:{preview.fingerprint}"),
                    due_date_policy_version="prepaid-draft-reconciliation-v1",
                    reason="reconcile_exactly_funded_prepaid_draft",
                ),
                apply_available_credit=False,
            )
            if selected_payment_id is not None:
                if not reviewed_document_correction:
                    _error(
                        "review_required",
                        "Selected-payment settlement requires reviewed document correction.",
                    )
                result = (
                    AccountCreditApplications.apply_invoice_from_selected_payment_fully(
                        db,
                        invoice,
                        payment_id=selected_payment_id,
                        expected_amount=preview.invoice_total,
                    )
                )
                opening_consumption = None
            else:
                funding = _funding_preview(db, invoice)
            if (
                selected_payment_id is None
                and preview.disposition
                is PrepaidDraftDisposition.reviewed_opening_fundable
            ):
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
            elif selected_payment_id is None:
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
                if reviewed_opening_correction or reviewed_document_correction
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


def _missing_paid_invoice_result(
    db: Session,
    *,
    invoice: Invoice,
    query: MissingPaidPrepaidInvoiceRepairQuery,
    preview_fingerprint: str,
    replayed: bool,
) -> MissingPaidPrepaidInvoiceRepairResult:
    entitlement = db.scalar(
        select(ServiceEntitlement).where(
            ServiceEntitlement.source_invoice_id == invoice.id,
            ServiceEntitlement.subscription_id == query.subscription_id,
            ServiceEntitlement.status == ServiceEntitlementStatus.active,
        )
    )
    remaining_credit = round_money(
        get_account_credit_balance(
            db,
            str(query.account_id),
            currency=(invoice.currency or "NGN").upper(),
        )
    )
    local_zone = ZoneInfo(APP_TIMEZONE_NAME)
    selected_allocation = round_money(
        to_decimal(
            db.scalar(
                select(func.coalesce(func.sum(PaymentAllocation.amount), 0)).where(
                    PaymentAllocation.invoice_id == invoice.id,
                    PaymentAllocation.payment_id == query.payment_id,
                    PaymentAllocation.is_active.is_(True),
                )
            )
        )
    )
    subscription = db.get(Subscription, query.subscription_id)
    if (
        entitlement is None
        or invoice.status is not InvoiceStatus.paid
        or round_money(to_decimal(invoice.balance_due)) != Decimal("0.00")
        or invoice.issued_at is None
        or invoice.paid_at is None
        or invoice.due_at is None
        or invoice.billing_period_start is None
        or invoice.billing_period_end is None
        or round_money(to_decimal(invoice.total)) != round_money(query.expected_total)
        or _utc(invoice.issued_at).astimezone(local_zone).date() != query.issued_on
        or _utc(invoice.paid_at).astimezone(local_zone).date() != query.issued_on
        or _utc(invoice.due_at).astimezone(local_zone).date() != query.due_on
        or _utc(invoice.billing_period_start).astimezone(local_zone).date()
        != query.issued_on
        or _utc(invoice.billing_period_end).astimezone(local_zone).date()
        != query.next_billing_on
        or selected_allocation != round_money(query.expected_total)
        or subscription is None
        or subscription.next_billing_at is None
        or _utc(subscription.next_billing_at) != _utc(invoice.billing_period_end)
        or remaining_credit != round_money(query.expected_remaining_credit)
    ):
        _error(
            "incomplete_repair",
            "Reviewed missing-invoice repair evidence is incomplete.",
            invoice_id=str(invoice.id),
        )
    return MissingPaidPrepaidInvoiceRepairResult(
        invoice_id=invoice.id,
        invoice_number=invoice.invoice_number,
        subscription_id=query.subscription_id,
        payment_id=query.payment_id,
        entitlement_id=entitlement.id,
        issued_at=_utc(invoice.issued_at),
        paid_at=_utc(invoice.paid_at),
        due_at=_utc(invoice.due_at),
        billing_period_start=_utc(invoice.billing_period_start),
        billing_period_end=_utc(invoice.billing_period_end),
        total=round_money(to_decimal(invoice.total)),
        remaining_credit=remaining_credit,
        preview_fingerprint=preview_fingerprint,
        replayed=replayed,
    )


def create_reviewed_paid_prepaid_invoice(
    db: Session,
    command: CreateReviewedPaidPrepaidInvoiceCommand,
) -> MissingPaidPrepaidInvoiceRepairResult:
    """Create and settle one explicitly reviewed missing prepaid invoice."""

    def operation() -> MissingPaidPrepaidInvoiceRepairResult:
        key = (command.context.idempotency_key or "").strip()
        if not key or len(key) > 120:
            _error("missing_idempotency_key", "A bounded idempotency key is required.")

        lock_account(db, str(command.query.account_id))
        locked_subscription = lock_for_update(
            db,
            Subscription,
            str(command.query.subscription_id),
        )
        locked_payment = lock_for_update(db, Payment, str(command.query.payment_id))
        if locked_subscription is None or locked_payment is None:
            _error("not_actionable", "Reviewed subscription or payment was not found.")
        db.refresh(locked_subscription)
        db.refresh(locked_payment)

        reservation = db.scalar(
            select(IdempotencyKey)
            .where(
                IdempotencyKey.scope == _MISSING_PAID_INVOICE_REPAIR_IDEMPOTENCY_SCOPE,
                IdempotencyKey.key == key,
            )
            .with_for_update()
        )
        if reservation is not None:
            if (
                reservation.account_id != command.query.account_id
                or not reservation.ref_id
            ):
                _error(
                    "idempotency_conflict",
                    "Idempotency key is reserved for different repair evidence.",
                )
            replay_invoice = db.get(Invoice, UUID(reservation.ref_id))
            if replay_invoice is None:
                _error(
                    "incomplete_repair",
                    "Idempotency evidence points to a missing invoice.",
                )
            metadata = dict(replay_invoice.metadata_ or {}).get(
                _MISSING_PAID_INVOICE_REPAIR_METADATA_KEY
            )
            if (
                not isinstance(metadata, dict)
                or metadata.get("preview_fingerprint") != command.preview_fingerprint
            ):
                _error(
                    "idempotency_conflict",
                    "Idempotency key was used with different repair evidence.",
                )
            return _missing_paid_invoice_result(
                db,
                invoice=replay_invoice,
                query=command.query,
                preview_fingerprint=command.preview_fingerprint,
                replayed=True,
            )

        current = preview_missing_paid_prepaid_invoice_repair(db, command.query)
        if current.fingerprint != command.preview_fingerprint:
            _error(
                "stale_preview",
                "Missing-invoice evidence changed after preview; preview again.",
            )
        if not current.actionable:
            _error(
                "not_actionable",
                "Missing prepaid invoice requires additional evidence review.",
                disposition=current.disposition.value,
                reason=current.reason,
            )
        if (
            current.issued_at is None
            or current.paid_at is None
            or current.due_at is None
            or current.billing_period_start is None
            or current.billing_period_end is None
        ):
            _error("incomplete_repair", "Reviewed invoice timestamps are incomplete.")

        from app.services.prepaid_service_renewals import (
            resolve_prepaid_monthly_charge_detail,
        )

        charge = resolve_prepaid_monthly_charge_detail(
            db,
            locked_subscription,
            current.paid_at,
        )
        if charge is None or charge.total != current.total:
            _error("stale_preview", "Contract charge changed after preview.")

        reservation = IdempotencyKey(
            scope=_MISSING_PAID_INVOICE_REPAIR_IDEMPOTENCY_SCOPE,
            key=key,
            account_id=current.account_id,
        )
        db.add(reservation)
        try:
            db.flush()
        except IntegrityError:
            _error(
                "idempotency_conflict",
                "Idempotency key was concurrently reserved by another command.",
            )

        try:
            invoice = Invoices.stage_system_invoice_for_owner(
                db,
                InvoiceCreate(
                    account_id=current.account_id,
                    status=InvoiceStatus.draft,
                    currency=current.currency,
                    subtotal=current.subtotal,
                    tax_total=current.tax_total,
                    total=current.total,
                    balance_due=current.total,
                    billing_period_start=current.billing_period_start,
                    billing_period_end=current.billing_period_end,
                    paid_at=current.paid_at,
                    memo=(
                        "Reviewed entity-scoped correction for a missing prepaid "
                        "service invoice."
                    ),
                ),
                reason="reviewed_missing_prepaid_paid_invoice",
            )
            offer_name = (
                locked_subscription.offer.name
                if locked_subscription.offer is not None
                else "Prepaid service"
            )
            InvoiceLines.stage_system_line_for_owner(
                db,
                SystemInvoiceLineCreate(
                    invoice_id=invoice.id,
                    subscription_id=locked_subscription.id,
                    description=(
                        f"{offer_name} ({command.query.issued_on} - "
                        f"{command.query.next_billing_on})"
                    ),
                    quantity=Decimal("1.000"),
                    unit_price=charge.unit_price,
                    amount=charge.unit_price,
                    tax_rate_id=charge.tax_rate_id,
                    tax_application=charge.tax_application,
                    metadata_={
                        "kind": "base_subscription",
                        "billing_period_start": (
                            current.billing_period_start.isoformat()
                        ),
                        "billing_period_end": current.billing_period_end.isoformat(),
                        "reviewed_missing_invoice": True,
                        "created_by_command_id": str(command.context.command_id),
                    },
                    billing_line_key=(
                        "missing-prepaid-repair:"
                        f"{locked_subscription.id}:{command.query.issued_on}:"
                        f"{command.query.next_billing_on}"
                    ),
                ),
                reason="reviewed_missing_prepaid_paid_invoice",
            )
        except InvoiceOwnerError as exc:
            _error(
                "participant_rejected",
                "Invoice owner rejected reviewed document construction.",
                participant_error=exc.code,
            )

        funding = _funding_preview(db, invoice)
        draft_preview = _build_preview(
            invoice=invoice,
            disposition=PrepaidDraftDisposition.exact_payment_fundable,
            action=PrepaidDraftAction.settle_paid,
            funding=funding,
            subscription_ids=(locked_subscription.id,),
            reason="selected reviewed payment fully funds the missing invoice",
        )
        if not funding.fully_funded or draft_preview.invoice_total != current.total:
            _error(
                "incomplete_repair",
                "Constructed invoice did not become exactly payment-fundable.",
                disposition=draft_preview.disposition.value,
            )
        changed_invoice, applied, payment_applied, opening_consumption = _stage_action(
            db,
            preview=draft_preview,
            effective_at=current.issued_at,
            due_at=current.due_at,
            context=command.context,
            reviewed_document_correction=True,
            selected_payment_id=command.query.payment_id,
        )
        if (
            applied != current.total
            or payment_applied != current.total
            or opening_consumption is not None
        ):
            _error(
                "incomplete_repair",
                "Reviewed repair did not consume the exact payment-backed amount.",
            )
        selected_allocation = round_money(
            to_decimal(
                db.scalar(
                    select(func.coalesce(func.sum(PaymentAllocation.amount), 0)).where(
                        PaymentAllocation.invoice_id == changed_invoice.id,
                        PaymentAllocation.payment_id == command.query.payment_id,
                        PaymentAllocation.is_active.is_(True),
                    )
                )
            )
        )
        if selected_allocation != current.total:
            _error(
                "incomplete_repair",
                "The selected payment did not exclusively fund the repaired invoice.",
                selected_payment_applied=str(selected_allocation),
            )

        metadata = dict(changed_invoice.metadata_ or {})
        metadata[_MISSING_PAID_INVOICE_REPAIR_METADATA_KEY] = {
            "subscription_id": str(command.query.subscription_id),
            "payment_id": str(command.query.payment_id),
            "issued_on": command.query.issued_on.isoformat(),
            "due_on": command.query.due_on.isoformat(),
            "next_billing_on": command.query.next_billing_on.isoformat(),
            "expected_total": str(round_money(command.query.expected_total)),
            "expected_remaining_credit": str(
                round_money(command.query.expected_remaining_credit)
            ),
            "preview_fingerprint": current.fingerprint,
            "command_id": str(command.context.command_id),
            "actor": command.context.actor,
            "reason": command.context.reason,
        }
        changed_invoice.metadata_ = metadata
        reservation.ref_id = str(changed_invoice.id)
        AuditEvents.stage(
            db,
            AuditEventCreate(
                action="create_reviewed_paid_prepaid_invoice",
                entity_type="invoice",
                entity_id=str(changed_invoice.id),
                metadata_={
                    "subscription_id": str(command.query.subscription_id),
                    "payment_id": str(command.query.payment_id),
                    "preview_fingerprint": current.fingerprint,
                    "total": str(current.total),
                    "currency": current.currency,
                    "expected_remaining_credit": str(current.expected_remaining_credit),
                    "issued_on": command.query.issued_on.isoformat(),
                    "due_on": command.query.due_on.isoformat(),
                    "next_billing_on": command.query.next_billing_on.isoformat(),
                },
            ),
        )
        db.flush()
        return _missing_paid_invoice_result(
            db,
            invoice=changed_invoice,
            query=command.query,
            preview_fingerprint=current.fingerprint,
            replayed=False,
        )

    return execute_owner_command(
        db,
        definition=_MISSING_PAID_INVOICE_REPAIR_COMMAND,
        context=command.context,
        operation=operation,
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


def _opening_settlement_preview_payload(
    preview: OpeningSettlementCorrectionPreview,
) -> dict[str, object]:
    return {
        "invoice_id": preview.invoice_id,
        "account_id": preview.account_id,
        "invoice_number": preview.invoice_number,
        "allocation_id": preview.allocation_id,
        "payment_id": preview.payment_id,
        "opening_position_id": preview.opening_position_id,
        "baseline_id": preview.baseline_id,
        "original_posting_group_id": preview.original_posting_group_id,
        "disposition": preview.disposition,
        "currency": preview.currency,
        "invoice_total": preview.invoice_total,
        "balance_due": preview.balance_due,
        "allocated_amount": preview.allocated_amount,
        "confirmed_balance": preview.confirmed_balance,
        "subledger_credit_before": preview.subledger_credit_before,
        "subledger_receivable_before": preview.subledger_receivable_before,
        "opening_settlement_amount": preview.opening_settlement_amount,
        "reason": preview.reason,
    }


def preview_opening_settlement_correction(
    db: Session,
    query: OpeningSettlementCorrectionQuery,
) -> OpeningSettlementCorrectionPreview:
    """Prove one invoice/allocation was already absorbed by its opening.

    The preview is deliberately narrow.  It accepts only the exact signature
    where a pre-opening invoice is reconstructed in the approved opening, a
    later payment allocation duplicated that economic effect, and the linked
    customer-subledger group is the complete two-lane application posting.
    """

    invoice = db.get(Invoice, query.invoice_id)
    if invoice is None:
        _error("invoice_not_found", "Invoice was not found.")
    allocation = db.get(PaymentAllocation, query.allocation_id)
    currency = (invoice.currency or "NGN").strip().upper()
    invoice_total = round_money(to_decimal(invoice.total))
    balance_due = round_money(to_decimal(invoice.balance_due))
    allocated_amount = (
        round_money(to_decimal(allocation.amount))
        if allocation is not None
        else Decimal("0.00")
    )
    payment = allocation.payment if allocation is not None else None
    opening = db.scalar(
        select(CustomerSubledgerOpeningPosition).where(
            CustomerSubledgerOpeningPosition.account_id == invoice.account_id,
            CustomerSubledgerOpeningPosition.currency == currency,
        )
    )
    baseline = (
        db.get(PrepaidFundingBaseline, opening.baseline_id)
        if opening is not None and opening.baseline_id is not None
        else None
    )
    groups = (
        tuple(
            db.scalars(
                select(CustomerPostingGroup)
                .where(
                    CustomerPostingGroup.source_kind
                    == PostingSourceKind.payment_allocation.value,
                    CustomerPostingGroup.source_id == query.allocation_id,
                    CustomerPostingGroup.command_kind
                    == PostingCommandKind.customer_credit_application,
                    CustomerPostingGroup.reverses_group_id.is_(None),
                )
                .order_by(CustomerPostingGroup.id)
            ).all()
        )
        if allocation is not None
        else ()
    )
    original_group = groups[0] if len(groups) == 1 else None
    group_reversal = (
        db.scalar(
            select(CustomerPostingGroup).where(
                CustomerPostingGroup.reverses_group_id == original_group.id
            )
        )
        if original_group is not None
        else None
    )
    consumption = db.scalar(
        select(PrepaidOpeningFundingConsumption).where(
            PrepaidOpeningFundingConsumption.invoice_id == invoice.id
        )
    )

    try:
        confirmed_balance = round_money(
            verified_prepaid_funding_balance(db, invoice.account_id, currency=currency)
        )
    except PrepaidFundingBaselineMissingError:
        confirmed_balance = Decimal("0.00")
    position = resolve_position(
        db,
        account_id=invoice.account_id,
        currency=currency,
    )

    issues: list[str] = []
    if not invoice.is_active or invoice.is_proforma:
        issues.append("invoice is not an active financial document")
    if invoice.status not in (
        InvoiceStatus.partially_paid,
        InvoiceStatus.issued,
        InvoiceStatus.overdue,
        InvoiceStatus.paid,
    ):
        issues.append("invoice status is outside the reviewed correction cohort")
    if len(currency) != 3 or invoice_total <= Decimal("0.00"):
        issues.append("invoice currency or total is invalid")
    if allocation is None or allocation.invoice_id != invoice.id:
        issues.append("selected allocation does not belong to the invoice")
    elif payment is None or payment.account_id != invoice.account_id:
        issues.append("allocation payment does not belong to the invoice account")
    elif (
        not payment.is_active
        or payment.status is not PaymentStatus.succeeded
        or payment.settlement is None
        or payment.refunds
        or payment.reversal is not None
        or (payment.currency or "").upper() != currency
    ):
        issues.append("allocation payment lacks exact unreturned settlement evidence")

    active_allocations = tuple(
        db.scalars(
            select(PaymentAllocation)
            .where(
                PaymentAllocation.invoice_id == invoice.id,
                PaymentAllocation.is_active.is_(True),
            )
            .order_by(PaymentAllocation.id)
        ).all()
    )
    already_reconciled = bool(
        consumption is not None
        and allocation is not None
        and not allocation.is_active
        and group_reversal is not None
        and invoice.status is InvoiceStatus.paid
        and balance_due == Decimal("0.00")
        and round_money(to_decimal(consumption.amount)) == invoice_total
        and confirmed_balance == round_money(query.expected_confirmed_balance)
        and position.unapplied_customer_credit == confirmed_balance
        and position.collectible_receivable == Decimal("0.00")
    )
    if not already_reconciled:
        if allocation is None or not allocation.is_active:
            issues.append(
                "selected allocation is not the sole active invoice allocation"
            )
        elif active_allocations != (allocation,):
            issues.append("invoice has competing active allocation evidence")
        if consumption is not None:
            issues.append("invoice already has opening-funding consumption evidence")
        if opening is None or baseline is None:
            issues.append("approved subledger opening or funding baseline is missing")
        else:
            invoice_instant = invoice.issued_at or invoice.created_at
            allocation_instant = (
                allocation.created_at if allocation is not None else None
            )
            if invoice_instant is None or not (
                _utc(baseline.position_at)
                < _utc(invoice_instant)
                <= _utc(opening.occurred_at)
            ):
                issues.append("invoice is not between the baseline and opening")
            if allocation_instant is None or _utc(allocation_instant) <= _utc(
                opening.occurred_at
            ):
                issues.append("allocation does not post after the approved opening")
            delta = (
                native_customer_financial_balances_by_currency(
                    db,
                    [invoice.account_id],
                    after=_utc(baseline.position_at),
                    before=_utc(opening.occurred_at),
                )
                .get(invoice.account_id, {})
                .get(currency, Decimal("0.00"))
            )
            reconstructed_opening = round_money(
                to_decimal(baseline.amount) + to_decimal(delta)
            )
            if reconstructed_opening != round_money(
                to_decimal(opening.legacy_position)
            ):
                issues.append(
                    "opening does not exactly reconstruct from baseline facts"
                )
            prior_consumption = round_money(
                to_decimal(
                    db.scalar(
                        select(
                            func.coalesce(
                                func.sum(PrepaidOpeningFundingConsumption.amount), 0
                            )
                        ).where(
                            PrepaidOpeningFundingConsumption.baseline_id == baseline.id
                        )
                    )
                )
            )
            if (
                round_money(to_decimal(baseline.amount) - prior_consumption)
                < invoice_total
            ):
                issues.append("approved opening source cannot fund the full invoice")

        if balance_due <= Decimal("0.00") or allocated_amount <= Decimal("0.00"):
            issues.append("invoice does not have a positive partial-settlement gap")
        if round_money(allocated_amount + balance_due) != invoice_total:
            issues.append("allocation and remaining balance do not equal invoice total")
        if confirmed_balance != round_money(query.expected_confirmed_balance):
            issues.append("confirmed customer balance changed")
        if allocation is None or len(groups) != 1 or original_group is None:
            issues.append("allocation does not have one exact customer posting group")
        else:
            ledger_entry_ids = tuple(
                value
                for value in (
                    allocation.ledger_entry_id,
                    allocation.consumption_ledger_entry_id,
                )
                if value is not None
            )
            prior_ledger_reversal = (
                db.scalar(
                    select(LedgerEntry.id).where(
                        LedgerEntry.reversal_of_entry_id.in_(ledger_entry_ids)
                    )
                )
                if ledger_entry_ids
                else None
            )
            effects = tuple(
                db.scalars(
                    select(CustomerPositionEffect)
                    .where(CustomerPositionEffect.group_id == original_group.id)
                    .order_by(CustomerPositionEffect.effect, CustomerPositionEffect.id)
                ).all()
            )
            effect_signature = {
                (
                    effect.effect,
                    round_money(to_decimal(effect.amount)),
                    effect.invoice_id,
                    effect.payment_id,
                )
                for effect in effects
            }
            expected_signature = {
                (
                    PositionEffectKind.customer_credit_consumed,
                    allocated_amount,
                    None,
                    payment.id if payment is not None else None,
                ),
                (
                    PositionEffectKind.receivable_settled,
                    allocated_amount,
                    invoice.id,
                    None,
                ),
            }
            if (
                original_group.authority.value != "authoritative"
                or prior_ledger_reversal is not None
                or original_group.producer_owner
                != PostingProducer.account_credit_applications.value
                or len(effects) != 2
                or effect_signature != expected_signature
                or group_reversal is not None
            ):
                issues.append(
                    "allocation posting is not the exact unreversed two-lane effect"
                )
        expected_credit_before = round_money(confirmed_balance - allocated_amount)
        if (
            position.unapplied_customer_credit != expected_credit_before
            or position.collectible_receivable != -allocated_amount
            or position.prepaid_funding_reserved != Decimal("0.00")
            or position.written_off_total != Decimal("0.00")
            or position.refunded_total != Decimal("0.00")
            or position.adjustment_total != Decimal("0.00")
        ):
            issues.append(
                "customer subledger does not match the duplicate-application signature"
            )
        if db.scalar(select(CustomerSubledgerAuthorityCutover.id).limit(1)) is None:
            issues.append("customer subledger authority is not active")

    if already_reconciled:
        disposition = OpeningSettlementCorrectionDisposition.already_reconciled
        reason = "the exact reviewed correction is already complete"
    elif issues:
        disposition = OpeningSettlementCorrectionDisposition.manual_review
        reason = "; ".join(dict.fromkeys(issues))
    else:
        disposition = (
            OpeningSettlementCorrectionDisposition.exact_preopening_double_application
        )
        reason = (
            "invoice is fully embedded in the approved opening and the later "
            "allocation duplicated only its customer-subledger effect"
        )
    preview = OpeningSettlementCorrectionPreview(
        invoice_id=invoice.id,
        account_id=invoice.account_id,
        invoice_number=invoice.invoice_number,
        allocation_id=query.allocation_id,
        payment_id=payment.id if payment is not None else None,
        opening_position_id=opening.id if opening is not None else None,
        baseline_id=baseline.id if baseline is not None else None,
        original_posting_group_id=(
            original_group.id if original_group is not None else None
        ),
        disposition=disposition,
        currency=currency,
        invoice_total=invoice_total,
        balance_due=balance_due,
        allocated_amount=allocated_amount,
        confirmed_balance=confirmed_balance,
        subledger_credit_before=position.unapplied_customer_credit,
        subledger_receivable_before=position.collectible_receivable,
        opening_settlement_amount=invoice_total,
        reason=reason,
        fingerprint="",
    )
    return replace(
        preview,
        fingerprint=_hash(_opening_settlement_preview_payload(preview)),
    )


def _opening_settlement_result(
    db: Session,
    *,
    query: OpeningSettlementCorrectionQuery,
    preview_fingerprint: str,
    replayed: bool,
) -> OpeningSettlementCorrectionResult:
    invoice = db.get(Invoice, query.invoice_id)
    allocation = db.get(PaymentAllocation, query.allocation_id)
    consumption = db.scalar(
        select(PrepaidOpeningFundingConsumption).where(
            PrepaidOpeningFundingConsumption.invoice_id == query.invoice_id
        )
    )
    if invoice is None or allocation is None or consumption is None:
        _error("incomplete_repair", "Opening-settlement evidence is incomplete.")
    original_group = db.scalar(
        select(CustomerPostingGroup).where(
            CustomerPostingGroup.source_kind
            == PostingSourceKind.payment_allocation.value,
            CustomerPostingGroup.source_id == allocation.id,
            CustomerPostingGroup.command_kind
            == PostingCommandKind.customer_credit_application,
            CustomerPostingGroup.reverses_group_id.is_(None),
        )
    )
    posting_reversal = (
        db.scalar(
            select(CustomerPostingGroup).where(
                CustomerPostingGroup.reverses_group_id == original_group.id
            )
        )
        if original_group is not None
        else None
    )
    ledger_original_ids = tuple(
        value
        for value in (
            allocation.ledger_entry_id,
            allocation.consumption_ledger_entry_id,
        )
        if value is not None
    )
    ledger_reversals = tuple(
        db.scalars(
            select(LedgerEntry)
            .where(LedgerEntry.reversal_of_entry_id.in_(ledger_original_ids))
            .order_by(LedgerEntry.id)
        ).all()
    )
    currency = (invoice.currency or "NGN").upper()
    confirmed = round_money(
        verified_prepaid_funding_balance(db, invoice.account_id, currency=currency)
    )
    position = resolve_position(db, account_id=invoice.account_id, currency=currency)
    if (
        allocation.is_active
        or posting_reversal is None
        or len(ledger_reversals) != len(ledger_original_ids)
        or invoice.status is not InvoiceStatus.paid
        or round_money(to_decimal(invoice.balance_due)) != Decimal("0.00")
        or round_money(to_decimal(consumption.amount))
        != round_money(to_decimal(invoice.total))
        or confirmed != round_money(query.expected_confirmed_balance)
        or position.unapplied_customer_credit != confirmed
        or position.collectible_receivable != Decimal("0.00")
    ):
        _error(
            "incomplete_repair", "Opening-settlement correction did not close exactly."
        )
    return OpeningSettlementCorrectionResult(
        invoice_id=invoice.id,
        allocation_id=allocation.id,
        opening_funding_consumption_id=consumption.id,
        customer_posting_reversal_id=posting_reversal.id,
        ledger_reversal_ids=tuple(item.id for item in ledger_reversals),
        final_status=invoice.status,
        final_balance_due=round_money(to_decimal(invoice.balance_due)),
        confirmed_balance=confirmed,
        subledger_credit_after=position.unapplied_customer_credit,
        subledger_receivable_after=position.collectible_receivable,
        preview_fingerprint=preview_fingerprint,
        replayed=replayed,
    )


def reconcile_opening_settlement_correction(
    db: Session,
    command: ReconcileOpeningSettlementCorrectionCommand,
) -> OpeningSettlementCorrectionResult:
    """Correct one reviewed post-opening duplicate allocation atomically."""

    def operation() -> OpeningSettlementCorrectionResult:
        key = (command.context.idempotency_key or "").strip()
        if not key or len(key) > 120:
            _error("missing_idempotency_key", "A bounded idempotency key is required.")
        invoice = db.get(Invoice, command.query.invoice_id)
        if invoice is None:
            _error("invoice_not_found", "Invoice was not found.")
        lock_account(db, str(invoice.account_id))
        reservation = db.scalar(
            select(IdempotencyKey)
            .where(
                IdempotencyKey.scope == _OPENING_SETTLEMENT_IDEMPOTENCY_SCOPE,
                IdempotencyKey.key == key,
            )
            .with_for_update()
        )
        if reservation is not None:
            if reservation.account_id != invoice.account_id or not reservation.ref_id:
                _error(
                    "idempotency_conflict", "Idempotency evidence belongs elsewhere."
                )
            replay_consumption = db.get(
                PrepaidOpeningFundingConsumption, UUID(reservation.ref_id)
            )
            if (
                replay_consumption is None
                or replay_consumption.invoice_id != command.query.invoice_id
                or replay_consumption.reconciliation_fingerprint
                != command.preview_fingerprint
            ):
                _error("idempotency_conflict", "Idempotency evidence changed.")
            return _opening_settlement_result(
                db,
                query=command.query,
                preview_fingerprint=command.preview_fingerprint,
                replayed=True,
            )

        locked_invoice = lock_for_update(db, Invoice, str(command.query.invoice_id))
        locked_allocation = lock_for_update(
            db, PaymentAllocation, str(command.query.allocation_id)
        )
        if locked_invoice is None or locked_allocation is None:
            _error("not_actionable", "Invoice or allocation was not found.")
        current = preview_opening_settlement_correction(db, command.query)
        if current.fingerprint != command.preview_fingerprint:
            _error(
                "stale_preview", "Opening-settlement evidence changed; preview again."
            )
        if not current.actionable or current.baseline_id is None:
            _error(
                "not_actionable",
                "Opening-settlement correction requires additional review.",
                disposition=current.disposition.value,
                reason=current.reason,
            )
        baseline = db.scalar(
            select(PrepaidFundingBaseline)
            .where(PrepaidFundingBaseline.id == current.baseline_id)
            .with_for_update()
        )
        if baseline is None or not baseline.is_active:
            _error("stale_preview", "Approved opening baseline changed; preview again.")
        original_group = db.scalar(
            select(CustomerPostingGroup)
            .where(CustomerPostingGroup.id == current.original_posting_group_id)
            .with_for_update()
        )
        if original_group is None:
            _error("stale_preview", "Allocation posting changed; preview again.")

        reservation = IdempotencyKey(
            scope=_OPENING_SETTLEMENT_IDEMPOTENCY_SCOPE,
            key=key,
            account_id=current.account_id,
        )
        db.add(reservation)
        try:
            db.flush()
        except IntegrityError:
            _error("idempotency_conflict", "Idempotency key was concurrently reserved.")

        ledger_reversals = AccountCreditApplications.release_for_reviewed_opening_settlement(
            db,
            ReviewedOpeningSettlementAllocationRelease(
                invoice_id=current.invoice_id,
                allocation_id=current.allocation_id,
                reason=(
                    "Reviewed correction: invoice was fully absorbed by the approved "
                    "customer opening position"
                ),
            ),
        )
        ledger_entry = LedgerEntries.create(
            db,
            LedgerEntryCreate(
                account_id=current.account_id,
                invoice_id=current.invoice_id,
                entry_type=LedgerEntryType.debit,
                source=LedgerSource.adjustment,
                category=LedgerCategory.internet_service,
                amount=current.opening_settlement_amount,
                currency=current.currency,
                memo=(
                    "Reviewed pre-opening settlement for invoice "
                    f"{current.invoice_number or current.invoice_id}"
                ),
                effective_date=_utc(command.effective_at),
            ),
            affects_customer_position=False,
            commit=False,
        )
        opening_key = (
            "preopening-invoice:" + hashlib.sha256(key.encode("utf-8")).hexdigest()
        )
        consumption = PrepaidOpeningFundingConsumption(
            baseline_id=baseline.id,
            account_id=current.account_id,
            invoice_id=current.invoice_id,
            ledger_entry_id=ledger_entry.id,
            amount=current.opening_settlement_amount,
            currency=current.currency,
            approval_evidence_ref=(
                f"{baseline.batch.evidence_ref}; customer-subledger-opening:"
                f"{current.opening_position_id}"
            ),
            approval_actor=baseline.batch.approved_by,
            reconciliation_fingerprint=current.fingerprint,
            idempotency_key=opening_key,
            consumed_at=_utc(command.effective_at),
        )
        db.add(consumption)
        db.flush()
        posting_reversal = stage_reversal(
            db,
            StageReversalCommand(
                original_group_id=original_group.id,
                producer_owner=PostingProducer.prepaid_draft_reconciliation,
                source_kind=PostingSourceKind.prepaid_opening_funding_consumption,
                source_id=consumption.id,
                occurred_at=_utc(command.effective_at),
                idempotency_key=f"posting:preopening-settlement:{consumption.id}",
            ),
            context=command.context,
        )
        finalize_reviewed_document_settlement_for_owner(db, locked_invoice)
        reservation.ref_id = str(consumption.id)
        AuditEvents.stage(
            db,
            AuditEventCreate(
                action="reconcile_preopening_invoice_settlement",
                entity_type="invoice",
                entity_id=str(current.invoice_id),
                metadata_={
                    "allocation_id": str(current.allocation_id),
                    "payment_id": str(current.payment_id),
                    "opening_position_id": str(current.opening_position_id),
                    "baseline_id": str(current.baseline_id),
                    "opening_funding_consumption_id": str(consumption.id),
                    "original_posting_group_id": str(original_group.id),
                    "customer_posting_reversal_id": str(posting_reversal.id),
                    "ledger_reversal_ids": [
                        str(reversal.id) for reversal, _original in ledger_reversals
                    ],
                    "invoice_total": str(current.invoice_total),
                    "confirmed_balance": str(current.confirmed_balance),
                    "preview_fingerprint": current.fingerprint,
                    "economic_delta": "0.00",
                },
            ),
        )
        db.flush()
        return _opening_settlement_result(
            db,
            query=command.query,
            preview_fingerprint=current.fingerprint,
            replayed=False,
        )

    return execute_owner_command(
        db,
        definition=_OPENING_SETTLEMENT_CORRECTION_COMMAND,
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
    command: FundingChangeDraftCommand,
) -> FundingChangeDraftResult:
    """Settle one exact existing draft before any invoice-less renewal.

    This is a flush-only participant for the existing funding-change
    transaction. Any draft, including an underfunded one, blocks the parallel
    direct-renewal path. Multiple drafts are intentionally left for reviewed
    reconciliation.
    """

    account_id = command.account_id
    currency = command.currency.strip().upper()
    effective_at = _utc(command.effective_at)
    if len(currency) != 3 or not command.evidence_ref.strip():
        _error(
            "invalid_funding_observation",
            "Funding change requires currency and evidence.",
        )
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
    "CreateReviewedPaidPrepaidInvoiceCommand",
    "FundingChangeDraftCommand",
    "FundingChangeDraftResult",
    "MissingPaidPrepaidInvoiceRepairDisposition",
    "MissingPaidPrepaidInvoiceRepairPreview",
    "MissingPaidPrepaidInvoiceRepairQuery",
    "MissingPaidPrepaidInvoiceRepairResult",
    "OpeningSettlementCorrectionDisposition",
    "OpeningSettlementCorrectionPreview",
    "OpeningSettlementCorrectionQuery",
    "OpeningSettlementCorrectionResult",
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
    "ReconcileOpeningSettlementCorrectionCommand",
    "RepairHistoricalPaidPrepaidInvoiceCommand",
    "adopt_funded_prepaid_proforma",
    "create_reviewed_paid_prepaid_invoice",
    "preview_prepaid_draft_cohort",
    "preview_prepaid_draft_reconciliation",
    "preview_funded_prepaid_proforma_adoption",
    "preview_historical_paid_prepaid_invoice_repair",
    "preview_missing_paid_prepaid_invoice_repair",
    "preview_opening_settlement_correction",
    "reconcile_prepaid_draft_invoice",
    "reconcile_opening_settlement_correction",
    "repair_historical_paid_prepaid_invoice",
    "stage_prepaid_draft_after_funding_change",
]
