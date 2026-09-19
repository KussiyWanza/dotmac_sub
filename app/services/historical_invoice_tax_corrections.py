"""Reviewed atomic correction for a paid invoice that omitted output tax.

The owner preserves history: it voids the incorrect paid document through the
invoice owner, releases the exact native payment allocation, settles one named
subscription draft, creates a tax-correct replacement, and consumes the same
payment completely.  Preview is read-only; confirmation is single-account,
fingerprinted, idempotent, and all-or-nothing.
"""

from __future__ import annotations

import enum
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import NoReturn
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.billing import (
    Invoice,
    InvoiceClosure,
    InvoiceClosureType,
    InvoiceDueDateBasis,
    InvoiceLine,
    InvoiceStatus,
    Payment,
    PaymentAllocation,
    PaymentRefund,
    PaymentReversal,
    PaymentStatus,
    TaxApplication,
    TaxRate,
)
from app.schemas.audit import AuditEventCreate
from app.schemas.billing import InvoiceCreate, SystemInvoiceLineCreate
from app.services.audit import AuditEvents
from app.services.billing._common import (
    get_spendable_account_credit_balance,
    lock_account,
)
from app.services.billing.account_credit import AccountCreditApplications
from app.services.billing.invoices import (
    HistoricalInvoiceTaxCorrectionDocumentEvidence,
    InvoiceIssuanceInput,
    InvoiceLines,
    InvoiceOwnerError,
    Invoices,
)
from app.services.billing.payments import PaymentAllocations
from app.services.common import round_money
from app.services.customer_tax_policies import get_customer_vat_exemption_policy
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.locking import lock_for_update
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

OWNER = "financial.historical_invoice_tax_corrections"
CONCERN = "reviewed historical invoice tax correction coordination"
CORRECTION_SCOPE = "billing:invoice:update"
_POLICY_VERSION = "historical-invoice-tax-correction-v1"
_MAX_REASON_LENGTH = 500

_CORRECT_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern=CONCERN,
    name="correct_historical_invoice_tax",
)


class HistoricalInvoiceTaxCorrectionDisposition(enum.StrEnum):
    eligible = "eligible"
    already_corrected = "already_corrected"
    manual_review = "manual_review"


class HistoricalInvoiceTaxCorrectionError(DomainError):
    """Transport-neutral rejection from the correction owner."""


def _error(suffix: str, message: str, **details: object) -> NoReturn:
    raise HistoricalInvoiceTaxCorrectionError(
        code=f"{OWNER}.{suffix}",
        message=message,
        details=details,
    )


@dataclass(frozen=True, slots=True)
class HistoricalInvoiceTaxCorrectionQuery:
    account_id: UUID
    source_invoice_id: UUID
    source_invoice_line_id: UUID
    void_evidence_invoice_id: UUID
    subscription_invoice_id: UUID
    payment_id: UUID
    tax_rate_id: UUID
    issued_at: datetime
    due_at: datetime
    currency: str = "NGN"


@dataclass(frozen=True, slots=True)
class HistoricalInvoiceTaxCorrectionPreview:
    disposition: HistoricalInvoiceTaxCorrectionDisposition
    reason: str
    account_id: UUID
    source_invoice_id: UUID
    source_invoice_number: str | None
    void_evidence_invoice_id: UUID
    subscription_invoice_id: UUID
    subscription_invoice_number: str | None
    payment_id: UUID
    tax_rate_id: UUID
    currency: str
    source_subtotal: Decimal
    source_tax_total: Decimal
    subscription_total: Decimal
    tax_rate_percent: Decimal
    tax_amount: Decimal
    replacement_total: Decimal
    payment_amount: Decimal
    payment_available_before: Decimal
    payment_available_after_void: Decimal
    projected_final_payment_available: Decimal
    account_credit_before: Decimal
    projected_final_account_credit: Decimal
    source_void_fingerprint: str | None
    source_payment_allocation_id: UUID | None
    replacement_invoice_id: UUID | None
    fingerprint: str

    @property
    def actionable(self) -> bool:
        return self.disposition is HistoricalInvoiceTaxCorrectionDisposition.eligible


@dataclass(frozen=True, slots=True)
class CorrectHistoricalInvoiceTaxCommand:
    query: HistoricalInvoiceTaxCorrectionQuery
    expected_preview_fingerprint: str
    permission_granted: bool
    authorized_system_user_id: UUID


@dataclass(frozen=True, slots=True)
class HistoricalInvoiceTaxCorrectionResult:
    account_id: UUID
    source_invoice_id: UUID
    source_invoice_closure_id: UUID
    source_payment_allocation_id: UUID
    subscription_invoice_id: UUID
    replacement_invoice_id: UUID
    payment_id: UUID
    subscription_payment_allocation_id: UUID
    replacement_payment_allocation_id: UUID
    source_subtotal: Decimal
    subscription_total: Decimal
    tax_amount: Decimal
    replacement_total: Decimal
    currency: str
    preview_fingerprint: str
    replayed: bool


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _normalized_currency(value: str) -> str:
    currency = value.strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        _error("currency_invalid", "Correction currency must be a three-letter code.")
    return currency


def _normalized_reason(value: str) -> str:
    reason = value.strip()
    if not reason or len(reason) > _MAX_REASON_LENGTH:
        _error(
            "reason_invalid",
            f"Correction reason must contain 1 to {_MAX_REASON_LENGTH} characters.",
        )
    return reason


def _fingerprint(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _child_key(kind: str, idempotency_key: str) -> str:
    digest = hashlib.sha256(f"{kind}:{idempotency_key}".encode()).hexdigest()
    return f"hist-tax-{kind}-{digest[:48]}"


def _active_lines(
    db: Session, invoice_id: UUID, *, lock: bool
) -> tuple[InvoiceLine, ...]:
    statement = (
        select(InvoiceLine)
        .where(
            InvoiceLine.invoice_id == invoice_id,
            InvoiceLine.is_active.is_(True),
            InvoiceLine.amount > Decimal("0.00"),
        )
        .order_by(InvoiceLine.id)
    )
    if lock:
        statement = statement.with_for_update()
    return tuple(db.scalars(statement).all())


def _manual_preview(
    query: HistoricalInvoiceTaxCorrectionQuery,
    *,
    reason: str,
    source_invoice: Invoice | None = None,
    subscription_invoice: Invoice | None = None,
    replacement_invoice_id: UUID | None = None,
) -> HistoricalInvoiceTaxCorrectionPreview:
    currency = _normalized_currency(query.currency)
    payload: dict[str, object] = {
        "disposition": HistoricalInvoiceTaxCorrectionDisposition.manual_review.value,
        "reason": reason,
        "account_id": query.account_id,
        "source_invoice_id": query.source_invoice_id,
        "void_evidence_invoice_id": query.void_evidence_invoice_id,
        "subscription_invoice_id": query.subscription_invoice_id,
        "payment_id": query.payment_id,
        "tax_rate_id": query.tax_rate_id,
        "issued_at": _utc(query.issued_at),
        "due_at": _utc(query.due_at),
        "currency": currency,
    }
    zero = Decimal("0.00")
    return HistoricalInvoiceTaxCorrectionPreview(
        disposition=HistoricalInvoiceTaxCorrectionDisposition.manual_review,
        reason=reason,
        account_id=query.account_id,
        source_invoice_id=query.source_invoice_id,
        source_invoice_number=(
            source_invoice.invoice_number if source_invoice is not None else None
        ),
        void_evidence_invoice_id=query.void_evidence_invoice_id,
        subscription_invoice_id=query.subscription_invoice_id,
        subscription_invoice_number=(
            subscription_invoice.invoice_number
            if subscription_invoice is not None
            else None
        ),
        payment_id=query.payment_id,
        tax_rate_id=query.tax_rate_id,
        currency=currency,
        source_subtotal=zero,
        source_tax_total=zero,
        subscription_total=zero,
        tax_rate_percent=zero,
        tax_amount=zero,
        replacement_total=zero,
        payment_amount=zero,
        payment_available_before=zero,
        payment_available_after_void=zero,
        projected_final_payment_available=zero,
        account_credit_before=zero,
        projected_final_account_credit=zero,
        source_void_fingerprint=None,
        source_payment_allocation_id=None,
        replacement_invoice_id=replacement_invoice_id,
        fingerprint=_fingerprint(payload),
    )


def _existing_replacement(
    db: Session, query: HistoricalInvoiceTaxCorrectionQuery
) -> tuple[Invoice, HistoricalInvoiceTaxCorrectionDocumentEvidence] | None:
    invoices = db.scalars(
        select(Invoice).where(
            Invoice.account_id == query.account_id,
            Invoice.is_active.is_(True),
        )
    ).all()
    for invoice in invoices:
        evidence = Invoices.historical_tax_correction_evidence(invoice)
        if evidence is None or evidence.source_invoice_id != query.source_invoice_id:
            continue
        if (
            evidence.account_id != query.account_id
            or evidence.source_invoice_line_id != query.source_invoice_line_id
            or evidence.void_evidence_invoice_id != query.void_evidence_invoice_id
            or evidence.subscription_invoice_id != query.subscription_invoice_id
            or evidence.payment_id != query.payment_id
            or evidence.tax_rate_id != query.tax_rate_id
        ):
            _error(
                "existing_correction_conflict",
                "Source invoice already has different correction evidence.",
                source_invoice_id=str(query.source_invoice_id),
            )
        return invoice, evidence
    return None


def _already_corrected_preview(
    query: HistoricalInvoiceTaxCorrectionQuery,
    *,
    replacement: Invoice,
    evidence: HistoricalInvoiceTaxCorrectionDocumentEvidence,
) -> HistoricalInvoiceTaxCorrectionPreview:
    source = replacement.account_id == query.account_id
    if (
        not source
        or replacement.status is not InvoiceStatus.paid
        or round_money(replacement.balance_due) != Decimal("0.00")
        or round_money(replacement.total) != round_money(evidence.replacement_total)
    ):
        _error(
            "existing_correction_drift",
            "Existing correction replacement invoice has drifted.",
            replacement_invoice_id=str(replacement.id),
        )
    zero = Decimal("0.00")
    return HistoricalInvoiceTaxCorrectionPreview(
        disposition=HistoricalInvoiceTaxCorrectionDisposition.already_corrected,
        reason="The exact historical tax correction is already complete.",
        account_id=query.account_id,
        source_invoice_id=query.source_invoice_id,
        source_invoice_number=None,
        void_evidence_invoice_id=query.void_evidence_invoice_id,
        subscription_invoice_id=query.subscription_invoice_id,
        subscription_invoice_number=None,
        payment_id=query.payment_id,
        tax_rate_id=query.tax_rate_id,
        currency=evidence.currency,
        source_subtotal=evidence.source_subtotal,
        source_tax_total=zero,
        subscription_total=evidence.subscription_total,
        tax_rate_percent=zero,
        tax_amount=evidence.tax_amount,
        replacement_total=evidence.replacement_total,
        payment_amount=round_money(
            evidence.subscription_total + evidence.replacement_total
        ),
        payment_available_before=zero,
        payment_available_after_void=zero,
        projected_final_payment_available=zero,
        account_credit_before=zero,
        projected_final_account_credit=zero,
        source_void_fingerprint=None,
        source_payment_allocation_id=evidence.source_payment_allocation_id,
        replacement_invoice_id=replacement.id,
        fingerprint=evidence.preview_fingerprint,
    )


def _build_preview(
    db: Session,
    query: HistoricalInvoiceTaxCorrectionQuery,
    *,
    lock: bool,
) -> HistoricalInvoiceTaxCorrectionPreview:
    currency = _normalized_currency(query.currency)
    issued_at = _utc(query.issued_at)
    due_at = _utc(query.due_at)
    if due_at < issued_at:
        return _manual_preview(
            query, reason="Replacement due date precedes issue date."
        )

    existing = _existing_replacement(db, query)
    if existing is not None:
        _result_from_replacement(
            db,
            replacement=existing[0],
            expected_fingerprint=existing[1].preview_fingerprint,
            replayed=True,
        )
        return _already_corrected_preview(
            query,
            replacement=existing[0],
            evidence=existing[1],
        )

    if get_customer_vat_exemption_policy(
        db,
        account_id=query.account_id,
    ).vat_exempt:
        return _manual_preview(
            query,
            reason="Customer is currently VAT-exempt and requires renewed review.",
        )

    records: dict[UUID, Invoice | None] = {}
    for invoice_id in sorted(
        {
            query.source_invoice_id,
            query.void_evidence_invoice_id,
            query.subscription_invoice_id,
        },
        key=str,
    ):
        records[invoice_id] = (
            lock_for_update(db, Invoice, invoice_id)
            if lock
            else db.get(Invoice, invoice_id)
        )
    source_invoice = records[query.source_invoice_id]
    void_evidence = records[query.void_evidence_invoice_id]
    subscription_invoice = records[query.subscription_invoice_id]
    if source_invoice is None or void_evidence is None or subscription_invoice is None:
        return _manual_preview(
            query,
            reason="One or more reviewed invoice documents were not found.",
            source_invoice=source_invoice,
            subscription_invoice=subscription_invoice,
        )
    if len({invoice.id for invoice in records.values() if invoice is not None}) != 3:
        return _manual_preview(
            query,
            reason="Source, void evidence, and subscription invoices must be distinct.",
            source_invoice=source_invoice,
            subscription_invoice=subscription_invoice,
        )
    documents = (source_invoice, void_evidence, subscription_invoice)
    if any(
        invoice.account_id != query.account_id
        or invoice.currency.upper() != currency
        or not invoice.is_active
        or invoice.is_proforma
        for invoice in documents
    ):
        return _manual_preview(
            query,
            reason="Reviewed invoice account, currency, or lifecycle evidence differs.",
            source_invoice=source_invoice,
            subscription_invoice=subscription_invoice,
        )

    source_lines = _active_lines(db, source_invoice.id, lock=lock)
    if (
        source_invoice.status is not InvoiceStatus.paid
        or round_money(source_invoice.balance_due) != Decimal("0.00")
        or round_money(source_invoice.tax_total) != Decimal("0.00")
        or len(source_lines) != 1
        or source_lines[0].id != query.source_invoice_line_id
        or source_lines[0].tax_rate_id is not None
        or round_money(source_lines[0].amount) != round_money(source_invoice.subtotal)
        or round_money(source_invoice.subtotal) != round_money(source_invoice.total)
        or round_money(source_invoice.total) <= Decimal("0.00")
    ):
        return _manual_preview(
            query,
            reason="Source invoice is not an exact paid base-only document.",
            source_invoice=source_invoice,
            subscription_invoice=subscription_invoice,
        )

    source_allocations_stmt = select(PaymentAllocation).where(
        PaymentAllocation.invoice_id == source_invoice.id,
        PaymentAllocation.is_active.is_(True),
        PaymentAllocation.amount > Decimal("0.00"),
    )
    if lock:
        source_allocations_stmt = source_allocations_stmt.with_for_update()
    source_allocations = tuple(db.scalars(source_allocations_stmt).all())
    if (
        len(source_allocations) != 1
        or source_allocations[0].payment_id != query.payment_id
        or round_money(source_allocations[0].amount)
        != round_money(source_invoice.total)
        or source_allocations[0].ledger_entry_id is None
        or source_allocations[0].consumption_ledger_entry_id is None
    ):
        return _manual_preview(
            query,
            reason="Source invoice is not funded by one exact releasable allocation.",
            source_invoice=source_invoice,
            subscription_invoice=subscription_invoice,
        )
    try:
        void_preview = Invoices.preview_void_for_owner(db, source_invoice.id)
    except InvoiceOwnerError:
        return _manual_preview(
            query,
            reason="Invoice owner cannot safely void and release the source invoice.",
            source_invoice=source_invoice,
            subscription_invoice=subscription_invoice,
        )
    if (
        void_preview.released_allocation_ids != (source_allocations[0].id,)
        or round_money(void_preview.payments_applied)
        != round_money(source_invoice.total)
        or round_money(void_preview.credits_applied) != Decimal("0.00")
    ):
        return _manual_preview(
            query,
            reason="Source invoice void preview does not release the exact payment.",
            source_invoice=source_invoice,
            subscription_invoice=subscription_invoice,
        )

    payment = (
        lock_for_update(db, Payment, query.payment_id)
        if lock
        else db.get(Payment, query.payment_id)
    )
    if payment is None:
        return _manual_preview(
            query,
            reason="Reviewed payment was not found.",
            source_invoice=source_invoice,
            subscription_invoice=subscription_invoice,
        )

    evidence_lines = _active_lines(db, void_evidence.id, lock=lock)
    evidence_closure = db.scalar(
        select(InvoiceClosure).where(InvoiceClosure.invoice_id == void_evidence.id)
    )
    tax_rate = (
        lock_for_update(db, TaxRate, query.tax_rate_id)
        if lock
        else db.get(TaxRate, query.tax_rate_id)
    )
    source_subtotal = round_money(source_invoice.subtotal)
    if tax_rate is None or not tax_rate.is_active or round_money(tax_rate.rate) <= 0:
        return _manual_preview(
            query,
            reason="Reviewed tax rate is missing or inactive.",
            source_invoice=source_invoice,
            subscription_invoice=subscription_invoice,
        )
    tax_percent = Decimal(str(tax_rate.rate))
    tax_amount = round_money(source_subtotal * tax_percent / Decimal("100"))
    replacement_total = round_money(source_subtotal + tax_amount)
    if (
        void_evidence.status is not InvoiceStatus.void
        or len(evidence_lines) != 1
        or evidence_lines[0].tax_rate_id != tax_rate.id
        or evidence_lines[0].tax_application is not TaxApplication.exclusive
        or round_money(evidence_lines[0].amount) != source_subtotal
        or evidence_lines[0].tax_rate_snapshot_version != 1
        or evidence_lines[0].tax_rate_code_snapshot != tax_rate.code
        or evidence_lines[0].tax_rate_percent_snapshot != tax_rate.rate
        or evidence_lines[0].tax_rate_is_active_snapshot is not True
        or round_money(void_evidence.subtotal) != source_subtotal
        or round_money(void_evidence.tax_total) != tax_amount
        or round_money(void_evidence.total) != replacement_total
        or evidence_closure is None
        or evidence_closure.closure_type is not InvoiceClosureType.void
        or round_money(evidence_closure.payments_applied) != Decimal("0.00")
        or round_money(evidence_closure.credits_applied) != Decimal("0.00")
    ):
        return _manual_preview(
            query,
            reason="Voided Finance draft does not prove the exact replacement tax shape.",
            source_invoice=source_invoice,
            subscription_invoice=subscription_invoice,
        )

    subscription_lines = _active_lines(db, subscription_invoice.id, lock=lock)
    subscription_allocations = tuple(
        db.scalars(
            select(PaymentAllocation).where(
                PaymentAllocation.invoice_id == subscription_invoice.id,
                PaymentAllocation.is_active.is_(True),
                PaymentAllocation.amount > Decimal("0.00"),
            )
        ).all()
    )
    subscription_total = round_money(subscription_invoice.total)
    subscription_subtotal = round_money(subscription_invoice.subtotal)
    subscription_tax = round_money(subscription_subtotal * tax_percent / Decimal("100"))
    if (
        subscription_invoice.status is not InvoiceStatus.draft
        or round_money(subscription_invoice.balance_due) != subscription_total
        or subscription_total <= Decimal("0.00")
        or len(subscription_lines) != 1
        or round_money(subscription_lines[0].amount) != subscription_subtotal
        or subscription_lines[0].tax_rate_id != tax_rate.id
        or subscription_lines[0].tax_application is not TaxApplication.exclusive
        or subscription_lines[0].tax_rate_snapshot_version != 1
        or subscription_lines[0].tax_rate_code_snapshot != tax_rate.code
        or subscription_lines[0].tax_rate_percent_snapshot != tax_rate.rate
        or subscription_lines[0].tax_rate_is_active_snapshot is not True
        or round_money(subscription_invoice.tax_total) != subscription_tax
        or subscription_total != round_money(subscription_subtotal + subscription_tax)
        or subscription_allocations
    ):
        return _manual_preview(
            query,
            reason="Subscription invoice is not one pristine positive draft.",
            source_invoice=source_invoice,
            subscription_invoice=subscription_invoice,
        )

    has_return = bool(
        db.scalar(
            select(PaymentRefund.id).where(PaymentRefund.payment_id == payment.id)
        )
        or db.scalar(
            select(PaymentReversal.id).where(PaymentReversal.payment_id == payment.id)
        )
    )
    all_allocations = tuple(
        db.scalars(
            select(PaymentAllocation).where(
                PaymentAllocation.payment_id == payment.id,
                PaymentAllocation.is_active.is_(True),
                PaymentAllocation.amount > Decimal("0.00"),
            )
        ).all()
    )
    payment_amount = round_money(payment.amount)
    payment_available = round_money(
        PaymentAllocations.available_amount(db, str(payment.id))
    )
    account_credit = round_money(
        get_spendable_account_credit_balance(
            db,
            str(query.account_id),
            currency=currency,
        )
    )
    required_available = round_money(subscription_total + tax_amount)
    if (
        not payment.is_active
        or payment.status is not PaymentStatus.succeeded
        or payment.account_id != query.account_id
        or payment.currency.upper() != currency
        or round_money(payment.refunded_amount) != Decimal("0.00")
        or has_return
        or {allocation.id for allocation in all_allocations}
        != {allocation.id for allocation in source_allocations}
        or payment_amount != round_money(subscription_total + replacement_total)
        or payment_available != required_available
        or account_credit != required_available
    ):
        return _manual_preview(
            query,
            reason="Named payment is not the exact sole funding source for both documents.",
            source_invoice=source_invoice,
            subscription_invoice=subscription_invoice,
        )

    payload: dict[str, object] = {
        "disposition": HistoricalInvoiceTaxCorrectionDisposition.eligible.value,
        "account_id": query.account_id,
        "source_invoice_id": source_invoice.id,
        "source_invoice_updated_at": _utc(source_invoice.updated_at),
        "source_invoice_line_id": source_lines[0].id,
        "source_invoice_line_updated_at": _utc(source_lines[0].updated_at),
        "source_payment_allocation_id": source_allocations[0].id,
        "void_evidence_invoice_id": void_evidence.id,
        "void_evidence_updated_at": _utc(void_evidence.updated_at),
        "subscription_invoice_id": subscription_invoice.id,
        "subscription_invoice_updated_at": _utc(subscription_invoice.updated_at),
        "payment_id": payment.id,
        "payment_updated_at": _utc(payment.updated_at),
        "tax_rate_id": tax_rate.id,
        "tax_rate_updated_at": _utc(tax_rate.updated_at),
        "currency": currency,
        "source_subtotal": source_subtotal,
        "subscription_total": subscription_total,
        "tax_rate_percent": tax_percent,
        "tax_amount": tax_amount,
        "replacement_total": replacement_total,
        "payment_amount": payment_amount,
        "payment_available_before": payment_available,
        "account_credit_before": account_credit,
        "source_void_fingerprint": void_preview.fingerprint,
        "issued_at": issued_at,
        "due_at": due_at,
    }
    return HistoricalInvoiceTaxCorrectionPreview(
        disposition=HistoricalInvoiceTaxCorrectionDisposition.eligible,
        reason=(
            "The named payment exactly funds the subscription and VAT-correct "
            "replacement after the source allocation is released."
        ),
        account_id=query.account_id,
        source_invoice_id=source_invoice.id,
        source_invoice_number=source_invoice.invoice_number,
        void_evidence_invoice_id=void_evidence.id,
        subscription_invoice_id=subscription_invoice.id,
        subscription_invoice_number=subscription_invoice.invoice_number,
        payment_id=payment.id,
        tax_rate_id=tax_rate.id,
        currency=currency,
        source_subtotal=source_subtotal,
        source_tax_total=Decimal("0.00"),
        subscription_total=subscription_total,
        tax_rate_percent=tax_percent,
        tax_amount=tax_amount,
        replacement_total=replacement_total,
        payment_amount=payment_amount,
        payment_available_before=payment_available,
        payment_available_after_void=payment_amount,
        projected_final_payment_available=Decimal("0.00"),
        account_credit_before=account_credit,
        projected_final_account_credit=Decimal("0.00"),
        source_void_fingerprint=void_preview.fingerprint,
        source_payment_allocation_id=source_allocations[0].id,
        replacement_invoice_id=None,
        fingerprint=_fingerprint(payload),
    )


def preview_historical_invoice_tax_correction(
    db: Session,
    query: HistoricalInvoiceTaxCorrectionQuery,
) -> HistoricalInvoiceTaxCorrectionPreview:
    """Return the exact no-write correction decision."""

    return _build_preview(db, query, lock=False)


def _result_from_replacement(
    db: Session,
    *,
    replacement: Invoice,
    expected_fingerprint: str,
    replayed: bool,
) -> HistoricalInvoiceTaxCorrectionResult:
    evidence = Invoices.historical_tax_correction_evidence(replacement)
    if evidence is None or evidence.preview_fingerprint != expected_fingerprint:
        _error(
            "replay_conflict",
            "Correction idempotency evidence belongs to a different preview.",
            replacement_invoice_id=str(replacement.id),
        )
    source = db.get(Invoice, evidence.source_invoice_id)
    source_line = db.get(InvoiceLine, evidence.source_invoice_line_id)
    void_evidence = db.get(Invoice, evidence.void_evidence_invoice_id)
    subscription = db.get(Invoice, evidence.subscription_invoice_id)
    payment = db.get(Payment, evidence.payment_id)
    closure = db.get(InvoiceClosure, evidence.source_invoice_closure_id)
    replacement_lines = _active_lines(db, replacement.id, lock=False)
    subscription_allocation = db.get(
        PaymentAllocation, evidence.subscription_payment_allocation_id
    )
    replacement_allocation = db.get(
        PaymentAllocation, evidence.replacement_payment_allocation_id
    )
    source_allocation = db.get(PaymentAllocation, evidence.source_payment_allocation_id)
    if (
        source is None
        or source.status is not InvoiceStatus.void
        or round_money(source.total) != round_money(evidence.source_subtotal)
        or source_line is None
        or not source_line.is_active
        or source_line.invoice_id != source.id
        or round_money(source_line.amount) != round_money(evidence.source_subtotal)
        or void_evidence is None
        or void_evidence.status is not InvoiceStatus.void
        or subscription is None
        or subscription.status is not InvoiceStatus.paid
        or round_money(subscription.total) != round_money(evidence.subscription_total)
        or replacement.status is not InvoiceStatus.paid
        or replacement.account_id != evidence.account_id
        or replacement.currency.upper() != evidence.currency
        or round_money(replacement.subtotal) != round_money(evidence.source_subtotal)
        or round_money(replacement.tax_total) != round_money(evidence.tax_amount)
        or round_money(replacement.total) != round_money(evidence.replacement_total)
        or len(replacement_lines) != 1
        or replacement_lines[0].tax_rate_id != evidence.tax_rate_id
        or replacement_lines[0].tax_application is not TaxApplication.exclusive
        or payment is None
        or not payment.is_active
        or payment.status is not PaymentStatus.succeeded
        or payment.account_id != evidence.account_id
        or payment.currency.upper() != evidence.currency
        or round_money(payment.amount)
        != round_money(evidence.subscription_total + evidence.replacement_total)
        or closure is None
        or closure.invoice_id != source.id
        or closure.closure_type is not InvoiceClosureType.void
        or round_money(closure.payments_applied)
        != round_money(evidence.source_subtotal)
        or source_allocation is None
        or source_allocation.is_active
        or source_allocation.payment_id != evidence.payment_id
        or source_allocation.invoice_id != source.id
        or round_money(source_allocation.amount)
        != round_money(evidence.source_subtotal)
        or subscription_allocation is None
        or not subscription_allocation.is_active
        or subscription_allocation.payment_id != evidence.payment_id
        or subscription_allocation.invoice_id != subscription.id
        or round_money(subscription_allocation.amount)
        != round_money(evidence.subscription_total)
        or replacement_allocation is None
        or not replacement_allocation.is_active
        or replacement_allocation.payment_id != evidence.payment_id
        or replacement_allocation.invoice_id != replacement.id
        or round_money(replacement_allocation.amount)
        != round_money(evidence.replacement_total)
        or round_money(
            PaymentAllocations.available_amount(db, str(evidence.payment_id))
        )
        != Decimal("0.00")
    ):
        _error(
            "correction_evidence_drift",
            "Historical tax correction evidence is incomplete or has drifted.",
            replacement_invoice_id=str(replacement.id),
        )
    return HistoricalInvoiceTaxCorrectionResult(
        account_id=evidence.account_id,
        source_invoice_id=evidence.source_invoice_id,
        source_invoice_closure_id=evidence.source_invoice_closure_id,
        source_payment_allocation_id=evidence.source_payment_allocation_id,
        subscription_invoice_id=evidence.subscription_invoice_id,
        replacement_invoice_id=replacement.id,
        payment_id=evidence.payment_id,
        subscription_payment_allocation_id=(
            evidence.subscription_payment_allocation_id
        ),
        replacement_payment_allocation_id=evidence.replacement_payment_allocation_id,
        source_subtotal=evidence.source_subtotal,
        subscription_total=evidence.subscription_total,
        tax_amount=evidence.tax_amount,
        replacement_total=evidence.replacement_total,
        currency=evidence.currency,
        preview_fingerprint=evidence.preview_fingerprint,
        replayed=replayed,
    )


def correct_historical_invoice_tax(
    db: Session,
    command: CorrectHistoricalInvoiceTaxCommand,
    *,
    context: CommandContext,
) -> HistoricalInvoiceTaxCorrectionResult:
    """Execute one reviewed correction as the only transaction owner."""

    return execute_owner_command(
        db,
        definition=_CORRECT_COMMAND,
        context=context,
        operation=lambda: _correct_historical_invoice_tax(
            db,
            command=command,
            context=context,
        ),
    )


def _correct_historical_invoice_tax(
    db: Session,
    *,
    command: CorrectHistoricalInvoiceTaxCommand,
    context: CommandContext,
) -> HistoricalInvoiceTaxCorrectionResult:
    key = (context.idempotency_key or "").strip()
    if len(key) < 16 or len(key) > 120:
        _error(
            "idempotency_key_required",
            "Correction idempotency key must contain 16 to 120 characters.",
        )
    reason = _normalized_reason(context.reason)
    if context.scope != CORRECTION_SCOPE:
        _error(
            "scope_invalid",
            "Correction command does not carry the invoice-update scope.",
        )
    if not command.permission_granted:
        _error("permission_denied", "Invoice correction permission is required.")
    if len(command.expected_preview_fingerprint) != 64:
        _error("preview_invalid", "A SHA-256 correction preview is required.")

    lock_account(db, str(command.query.account_id))
    existing = _existing_replacement(db, command.query)
    if existing is not None:
        return _result_from_replacement(
            db,
            replacement=existing[0],
            expected_fingerprint=command.expected_preview_fingerprint,
            replayed=True,
        )

    current = _build_preview(db, command.query, lock=True)
    if current.fingerprint != command.expected_preview_fingerprint:
        _error(
            "stale_preview",
            "Correction evidence changed after preview; preview again.",
            current_fingerprint=current.fingerprint,
        )
    if (
        not current.actionable
        or current.source_void_fingerprint is None
        or current.source_payment_allocation_id is None
    ):
        _error(
            "not_actionable",
            current.reason,
            disposition=current.disposition.value,
        )
    if get_customer_vat_exemption_policy(
        db,
        account_id=command.query.account_id,
    ).vat_exempt:
        _error(
            "customer_tax_policy_changed",
            "Customer is currently VAT-exempt; correction requires renewed review.",
        )

    source = db.get(Invoice, command.query.source_invoice_id)
    void_evidence = db.get(Invoice, command.query.void_evidence_invoice_id)
    subscription = db.get(Invoice, command.query.subscription_invoice_id)
    if source is None or void_evidence is None or subscription is None:
        _error("invoice_missing", "Reviewed invoice evidence disappeared under lock.")

    closure = Invoices.confirm_void_for_owner(
        db,
        source.id,
        preview_fingerprint=current.source_void_fingerprint,
        idempotency_key=_child_key("void", key),
        reason=reason,
        reconcile_access=False,
    ).closure

    subscription_issue = InvoiceIssuanceInput(
        issued_at=_utc(command.query.issued_at),
        due_at=_utc(command.query.due_at),
        due_date_basis=InvoiceDueDateBasis.approved_manual_override,
        due_date_basis_ref=f"historical-tax-correction:{source.id}:subscription",
        due_date_policy_version=_POLICY_VERSION,
        reason="historical_invoice_tax_correction_subscription_settlement",
    )
    Invoices.issue_draft_for_owner(
        db,
        str(subscription.id),
        issuance=subscription_issue,
        announce=False,
        apply_available_credit=False,
    )
    subscription_application = (
        AccountCreditApplications.apply_invoice_from_selected_payment_fully(
            db,
            subscription,
            payment_id=command.query.payment_id,
            expected_amount=current.subscription_total,
        )
    )
    if len(subscription_application.allocation_ids) != 1:
        _error(
            "subscription_settlement_incomplete",
            "Subscription invoice did not produce one exact payment allocation.",
        )
    subscription_allocation_id = UUID(subscription_application.allocation_ids[0])

    replacement = Invoices.stage_system_invoice_for_owner(
        db,
        InvoiceCreate(
            account_id=command.query.account_id,
            status=InvoiceStatus.draft,
            currency=current.currency,
            subtotal=Decimal("0.00"),
            tax_total=Decimal("0.00"),
            total=Decimal("0.00"),
            balance_due=Decimal("0.00"),
            memo=(
                f"VAT-correct replacement for "
                f"{source.invoice_number or source.id}; evidence "
                f"{void_evidence.invoice_number or void_evidence.id}"
            ),
        ),
        reason="historical_invoice_tax_correction_replacement",
    )
    evidence_line = _active_lines(db, void_evidence.id, lock=False)[0]
    InvoiceLines.stage_system_line_for_owner(
        db,
        SystemInvoiceLineCreate(
            invoice_id=replacement.id,
            description=evidence_line.description,
            quantity=evidence_line.quantity,
            unit_price=evidence_line.unit_price,
            amount=evidence_line.amount,
            tax_rate_id=command.query.tax_rate_id,
            tax_application=TaxApplication.exclusive,
            billing_line_key=f"historical-tax-correction:{source.id}",
            metadata_={
                "kind": "historical_invoice_tax_correction",
                "source_invoice_id": str(source.id),
                "void_evidence_invoice_id": str(void_evidence.id),
            },
        ),
        reason="historical_invoice_tax_correction_replacement",
    )
    replacement = Invoices.recalculate_totals_for_owner(db, replacement.id)
    if (
        round_money(replacement.subtotal) != current.source_subtotal
        or round_money(replacement.tax_total) != current.tax_amount
        or round_money(replacement.total) != current.replacement_total
        or round_money(replacement.balance_due) != current.replacement_total
    ):
        _error(
            "replacement_document_mismatch",
            "Invoice owner produced a replacement with different totals.",
        )

    replacement_issue = InvoiceIssuanceInput(
        issued_at=_utc(command.query.issued_at),
        due_at=_utc(command.query.due_at),
        due_date_basis=InvoiceDueDateBasis.approved_manual_override,
        due_date_basis_ref=f"historical-tax-correction:{source.id}:replacement",
        due_date_policy_version=_POLICY_VERSION,
        reason="historical_invoice_tax_correction_replacement",
    )
    Invoices.issue_draft_for_owner(
        db,
        str(replacement.id),
        issuance=replacement_issue,
        announce=False,
        apply_available_credit=False,
    )
    replacement_application = (
        AccountCreditApplications.apply_invoice_from_selected_payment_fully(
            db,
            replacement,
            payment_id=command.query.payment_id,
            expected_amount=current.replacement_total,
        )
    )
    if len(replacement_application.allocation_ids) != 1:
        _error(
            "replacement_settlement_incomplete",
            "Replacement invoice did not produce one exact payment allocation.",
        )
    replacement_allocation_id = UUID(replacement_application.allocation_ids[0])

    evidence = HistoricalInvoiceTaxCorrectionDocumentEvidence(
        account_id=command.query.account_id,
        source_invoice_id=source.id,
        source_invoice_line_id=command.query.source_invoice_line_id,
        source_invoice_closure_id=closure.id,
        source_payment_allocation_id=current.source_payment_allocation_id,
        void_evidence_invoice_id=void_evidence.id,
        subscription_invoice_id=subscription.id,
        payment_id=command.query.payment_id,
        tax_rate_id=command.query.tax_rate_id,
        subscription_payment_allocation_id=subscription_allocation_id,
        replacement_payment_allocation_id=replacement_allocation_id,
        source_subtotal=current.source_subtotal,
        subscription_total=current.subscription_total,
        tax_amount=current.tax_amount,
        replacement_total=current.replacement_total,
        currency=current.currency,
        preview_fingerprint=current.fingerprint,
        command_id=context.command_id,
        reason=reason,
    )
    Invoices.stage_historical_tax_correction_evidence_for_owner(
        db,
        replacement.id,
        evidence=evidence,
    )
    db.flush()

    payment_available_after = round_money(
        PaymentAllocations.available_amount(db, str(command.query.payment_id))
    )
    account_credit_after = round_money(
        get_spendable_account_credit_balance(
            db,
            str(command.query.account_id),
            currency=current.currency,
        )
    )
    if payment_available_after != Decimal("0.00") or account_credit_after != Decimal(
        "0.00"
    ):
        _error(
            "correction_balance_not_closed",
            "Correction did not consume the named payment and account credit exactly.",
            payment_available=str(payment_available_after),
            account_credit=str(account_credit_after),
        )

    AuditEvents.stage(
        db,
        AuditEventCreate(
            actor_type=AuditActorType.user,
            actor_id=str(command.authorized_system_user_id),
            action="correct_historical_invoice_tax",
            entity_type="invoice_tax_correction",
            entity_id=str(source.id),
            request_id=str(context.correlation_id),
            metadata_={
                "account_id": str(command.query.account_id),
                "source_invoice_id": str(source.id),
                "source_invoice_closure_id": str(closure.id),
                "source_payment_allocation_id": str(
                    current.source_payment_allocation_id
                ),
                "void_evidence_invoice_id": str(void_evidence.id),
                "subscription_invoice_id": str(subscription.id),
                "replacement_invoice_id": str(replacement.id),
                "payment_id": str(command.query.payment_id),
                "subscription_payment_allocation_id": str(subscription_allocation_id),
                "replacement_payment_allocation_id": str(replacement_allocation_id),
                "source_subtotal": str(current.source_subtotal),
                "subscription_total": str(current.subscription_total),
                "tax_amount": str(current.tax_amount),
                "replacement_total": str(current.replacement_total),
                "currency": current.currency,
                "preview_fingerprint": current.fingerprint,
                "command_id": str(context.command_id),
                "command_scope": context.scope,
                "command_reason": reason,
            },
        ),
    )
    emit_event(
        db,
        EventType.invoice_tax_correction_completed,
        {
            "source_invoice_id": str(source.id),
            "source_invoice_closure_id": str(closure.id),
            "source_payment_allocation_id": str(current.source_payment_allocation_id),
            "subscription_invoice_id": str(subscription.id),
            "replacement_invoice_id": str(replacement.id),
            "payment_id": str(command.query.payment_id),
            "source_subtotal": str(current.source_subtotal),
            "subscription_total": str(current.subscription_total),
            "tax_amount": str(current.tax_amount),
            "replacement_total": str(current.replacement_total),
            "currency": current.currency,
            "preview_fingerprint": current.fingerprint,
        },
        account_id=command.query.account_id,
        invoice_id=replacement.id,
    )
    db.flush()
    return HistoricalInvoiceTaxCorrectionResult(
        account_id=command.query.account_id,
        source_invoice_id=source.id,
        source_invoice_closure_id=closure.id,
        source_payment_allocation_id=current.source_payment_allocation_id,
        subscription_invoice_id=subscription.id,
        replacement_invoice_id=replacement.id,
        payment_id=command.query.payment_id,
        subscription_payment_allocation_id=subscription_allocation_id,
        replacement_payment_allocation_id=replacement_allocation_id,
        source_subtotal=current.source_subtotal,
        subscription_total=current.subscription_total,
        tax_amount=current.tax_amount,
        replacement_total=current.replacement_total,
        currency=current.currency,
        preview_fingerprint=current.fingerprint,
        replayed=False,
    )


__all__ = [
    "CORRECTION_SCOPE",
    "CorrectHistoricalInvoiceTaxCommand",
    "HistoricalInvoiceTaxCorrectionDisposition",
    "HistoricalInvoiceTaxCorrectionError",
    "HistoricalInvoiceTaxCorrectionPreview",
    "HistoricalInvoiceTaxCorrectionQuery",
    "HistoricalInvoiceTaxCorrectionResult",
    "correct_historical_invoice_tax",
    "preview_historical_invoice_tax_correction",
]
