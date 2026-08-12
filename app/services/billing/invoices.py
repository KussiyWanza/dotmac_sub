"""Invoice and invoice line management services."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from app.models.billing import (
    Invoice,
    InvoiceClosure,
    InvoiceClosureLedgerEvidence,
    InvoiceClosureOrigin,
    InvoiceClosureType,
    InvoiceLine,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    TaxApplication,
)
from app.models.domain_settings import SettingDomain
from app.models.idempotency import IdempotencyKey
from app.schemas.audit import AuditEventCreate
from app.schemas.billing import (
    InvoiceBulkVoidRequest,
    InvoiceBulkWriteOffRequest,
    InvoiceClosureConfirm,
    InvoiceClosureReconciliationRequest,
    InvoiceCreate,
    InvoiceLineCreate,
    InvoiceLineUpdate,
    InvoiceUpdate,
    LedgerEntryCreate,
    SystemInvoiceLineCreate,
)
from app.services import customer_tax_policies, numbering, settings_spec
from app.services.audit import AuditEvents
from app.services.billing._common import (
    _recalculate_invoice_totals,
    _resolve_tax_rate,
    _validate_account,
    _validate_invoice_line_amount,
    _validate_invoice_totals,
    assert_legal_invoice_transition,
    lock_account,
    resolve_invoice_settlement_amounts,
)
from app.services.billing.ledger import LedgerEntries
from app.services.common import (
    apply_ordering,
    apply_pagination,
    coerce_uuid,
    get_by_id,
    round_money,
    to_decimal,
    validate_enum,
)
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.invoice_classification import collectible_ar_invoice_filter
from app.services.locking import lock_for_update
from app.services.response import ListResponseMixin
from app.services.sync_feeds import apply_sync_page, sync_page_response

logger = logging.getLogger(__name__)

_VOID_IDEMPOTENCY_SCOPE = "invoice_void"
_WRITE_OFF_IDEMPOTENCY_SCOPE = "invoice_write_off"
_RECONCILIATION_IDEMPOTENCY_SCOPE = "invoice_closure_reconciliation"
_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._~-]{16,120}$")


class InvoiceOwnerError(DomainError):
    """Transport-neutral failure from an invoice-owner participant."""


def _apply_available_account_credit(db: Session, invoice: Invoice) -> None:
    """Hand an allocatable invoice to the canonical account-credit owner."""
    if (
        invoice.is_active
        and not invoice.is_proforma
        and invoice.status
        in {
            InvoiceStatus.issued,
            InvoiceStatus.partially_paid,
            InvoiceStatus.overdue,
        }
        and round_money(to_decimal(invoice.balance_due)) > Decimal("0.00")
    ):
        from app.services.billing.account_credit import AccountCreditApplications

        # Sessions use autoflush=False; make the issued state visible to the
        # applicator's deterministic query before it selects the account cohort.
        db.flush()
        AccountCreditApplications.apply(db, str(invoice.account_id))


def _apply_available_account_credit_when_fully_funded(
    db: Session, invoice: Invoice
) -> None:
    """Apply account credit only when it can settle the whole invoice."""
    if (
        not invoice.is_active
        or invoice.is_proforma
        or invoice.status
        not in {
            InvoiceStatus.issued,
            InvoiceStatus.partially_paid,
            InvoiceStatus.overdue,
        }
        or round_money(to_decimal(invoice.balance_due)) <= Decimal("0.00")
    ):
        return

    from app.services.billing.account_credit import AccountCreditApplications

    db.flush()
    preview = AccountCreditApplications.preview_invoice_funding(db, invoice)
    if not preview.fully_funded:
        return
    try:
        AccountCreditApplications.apply_invoice_fully(
            db,
            invoice,
            preview_fingerprint=preview.fingerprint,
        )
    except DomainError as exc:
        raise HTTPException(status_code=409, detail=exc.message) from exc


@dataclass(frozen=True)
class InvoiceClosureLedgerEffect:
    reverses_ledger_entry_id: UUID | None
    result_entry_type: LedgerEntryType
    result_source: LedgerSource
    amount: Decimal
    currency: str


@dataclass(frozen=True)
class InvoiceLifecycleTransitionResult:
    invoice: Invoice
    changed: bool
    event_emitted: bool = False


@dataclass(frozen=True, slots=True)
class ProformaConversionInput:
    """Locked documentary values for one proforma-to-invoice transition."""

    invoice_id: UUID
    invoice_number: str | None
    memo: str | None
    issued_at: datetime
    reason: str


@dataclass(frozen=True, slots=True)
class PrepaidProformaDocumentAdoption:
    """Exact documentary identity approved by the prepaid reconciler."""

    invoice_id: UUID
    line_id: UUID
    subscription_id: UUID
    billing_period_start: datetime
    billing_period_end: datetime
    line_description: str
    adoption_evidence_ref: str


@dataclass(frozen=True, slots=True)
class PaidPrepaidInvoiceDocumentRepair:
    """Exact missing identity approved for one historical paid invoice."""

    invoice_id: UUID
    line_id: UUID
    subscription_id: UUID
    billing_period_start: datetime
    billing_period_end: datetime
    line_description: str
    repair_evidence_ref: str


@dataclass(frozen=True, slots=True)
class DraftInvoiceLineReplacement:
    """One line in a complete admin-draft replacement request."""

    payload: InvoiceLineCreate
    line_id: UUID | None = None


class DraftInvoiceParticipantError(ValueError):
    """Flush-only rejection mapped by the draft-authoring coordinator."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class InvoiceClosurePreview:
    invoice_id: UUID
    invoice_number: str | None
    account_id: UUID
    closure_type: InvoiceClosureType
    status_before: InvoiceStatus
    status_after: InvoiceStatus
    invoice_total: Decimal
    payments_applied: Decimal
    credits_applied: Decimal
    receivable_before: Decimal
    receivable_after: Decimal
    closure_amount: Decimal
    currency: str
    released_allocation_ids: tuple[UUID, ...]
    ledger_effects: tuple[InvoiceClosureLedgerEffect, ...]
    access_consequence: str
    fingerprint: str


@dataclass(frozen=True)
class InvoiceClosureCapability:
    allowed: bool
    reason: str | None = None


@dataclass(frozen=True)
class InvoiceClosureResult:
    invoice: Invoice
    closure: InvoiceClosure
    preview: InvoiceClosurePreview | None
    idempotent_replay: bool = False


@dataclass(frozen=True)
class InvoiceClosureEvidenceCandidate:
    ledger_entry_id: UUID
    entry_type: LedgerEntryType
    source: LedgerSource
    amount: Decimal
    currency: str
    reversal_of_entry_id: UUID | None


@dataclass(frozen=True)
class InvoiceClosureEvidenceInspection:
    invoice_id: UUID
    closure_type: InvoiceClosureType
    expected_amount: Decimal
    currency: str
    candidates: tuple[InvoiceClosureEvidenceCandidate, ...]
    fingerprint: str


@dataclass(frozen=True)
class InvoiceFinancialSummary:
    """Owner-produced invoice receivable projection for UI/API consumers."""

    currency: str
    invoice_total: Decimal
    receivable_balance: Decimal
    payments_applied: Decimal
    credits_applied: Decimal


def reconcile_service_after_invoice_settlement(
    db: Session, account_id, invoice_id=None
) -> None:
    """Lift overdue enforcement after a non-payment settlement clears the debt.

    Write-off / void zero an invoice's balance without a Payment, so they never
    hit the restore-on-payment path in ``payments._finalize_invoice_payment_effects``.
    Without this, an overdue invoice that is written off or voided clears the debt
    but leaves the ``overdue`` enforcement lock active — the service stays
    suspended on a stale lock. Mirror the payment path: if the account no longer
    owes overdue debt, restore eligible service and re-derive account status.
    Caller commits.
    """
    from app.services import collections as collections_service
    from app.services.account_lifecycle import compute_account_status

    if not collections_service.has_overdue_balance(db, str(account_id)):
        collections_service.restore_account_services(
            db,
            str(account_id),
            invoice_id=str(invoice_id) if invoice_id else None,
        )
    compute_account_status(db, str(account_id))


def next_invoice_number(db: Session) -> str:
    """Generate the next sequential invoice number."""
    return numbering.generate_required_number(
        db,
        SettingDomain.billing,
        "invoice_number",
        "invoice_number_prefix",
        "invoice_number_padding",
        "invoice_number_start",
    )


def _normalize_closure_key(value: str) -> str:
    key = value.strip()
    if not _IDEMPOTENCY_KEY_RE.fullmatch(key):
        raise HTTPException(
            status_code=400,
            detail="Invoice closure idempotency key must be 16-120 safe characters",
        )
    return key


def _invoice_closure_scope(closure_type: InvoiceClosureType) -> str:
    return (
        _VOID_IDEMPOTENCY_SCOPE
        if closure_type == InvoiceClosureType.void
        else _WRITE_OFF_IDEMPOTENCY_SCOPE
    )


def _is_collectible_receivable(db: Session, invoice_id: UUID) -> bool:
    return (
        db.query(Invoice.id)
        .filter(Invoice.id == invoice_id)
        .filter(collectible_ar_invoice_filter())
        .first()
        is not None
    )


def _closure_fingerprint(
    *,
    invoice: Invoice,
    closure_type: InvoiceClosureType,
    payments_applied: Decimal,
    credits_applied: Decimal,
    receivable_before: Decimal,
    effects: tuple[InvoiceClosureLedgerEffect, ...],
) -> str:
    encoded = json.dumps(
        {
            "invoice_id": str(invoice.id),
            "closure_type": closure_type.value,
            "status": invoice.status.value,
            "invoice_total": f"{round_money(to_decimal(invoice.total)):.2f}",
            "balance_due_summary": (
                f"{round_money(to_decimal(invoice.balance_due)):.2f}"
            ),
            "payments_applied": f"{payments_applied:.2f}",
            "credits_applied": f"{credits_applied:.2f}",
            "receivable_before": f"{receivable_before:.2f}",
            "currency": invoice.currency,
            "ledger_effects": [
                {
                    "reverses_ledger_entry_id": (
                        str(effect.reverses_ledger_entry_id)
                        if effect.reverses_ledger_entry_id
                        else None
                    ),
                    "entry_type": effect.result_entry_type.value,
                    "source": effect.result_source.value,
                    "amount": f"{effect.amount:.2f}",
                    "currency": effect.currency,
                }
                for effect in effects
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _invoice_debits_to_reverse(db: Session, invoice: Invoice) -> list[LedgerEntry]:
    debits = (
        db.query(LedgerEntry)
        .filter(LedgerEntry.invoice_id == invoice.id)
        .filter(LedgerEntry.entry_type == LedgerEntryType.debit)
        # Current invoice postings use ``invoice``. Historical remediation
        # postings used ``adjustment`` while still carrying the structural
        # invoice_id link. Both are exact receivable evidence; unrelated
        # account adjustments have no invoice_id and remain outside this owner.
        .filter(LedgerEntry.source.in_([LedgerSource.invoice, LedgerSource.adjustment]))
        .filter(LedgerEntry.is_active.is_(True))
        .order_by(LedgerEntry.created_at.asc(), LedgerEntry.id.asc())
        .all()
    )
    if not debits:
        return []
    reversed_original = db.scalar(
        select(LedgerEntry.id).where(
            LedgerEntry.reversal_of_entry_id.in_([entry.id for entry in debits])
        )
    )
    if reversed_original is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                "Invoice ledger evidence is already partially reversed; use "
                "historical evidence reconciliation"
            ),
        )
    return debits


def _build_closure_preview(
    db: Session,
    invoice: Invoice,
    closure_type: InvoiceClosureType,
) -> InvoiceClosurePreview:
    if invoice.closure is not None:
        raise HTTPException(
            status_code=409, detail="Invoice already has closure evidence"
        )
    if invoice.status in {InvoiceStatus.void, InvoiceStatus.written_off}:
        raise HTTPException(
            status_code=409,
            detail=(
                "Historical terminal invoice lacks closure evidence; inspect and "
                "reconcile exact evidence instead"
            ),
        )
    settlement = resolve_invoice_settlement_amounts(db, invoice.id)
    payments_applied = round_money(settlement.payments_applied)
    credits_applied = round_money(settlement.credits_applied)
    released_allocation_ids: tuple[UUID, ...] = ()
    derived_receivable = max(
        Decimal("0.00"),
        round_money(to_decimal(invoice.total) - settlement.total_applied),
    )
    effects: tuple[InvoiceClosureLedgerEffect, ...]

    if closure_type == InvoiceClosureType.write_off:
        if invoice.status not in {
            InvoiceStatus.issued,
            InvoiceStatus.partially_paid,
            InvoiceStatus.overdue,
        }:
            raise HTTPException(
                status_code=409,
                detail="Only an issued collectible receivable can be written off",
            )
        if not _is_collectible_receivable(db, invoice.id):
            raise HTTPException(
                status_code=409,
                detail=(
                    "Prepaid or reconciliation-held invoices are not bad debt; "
                    "use the appropriate void/remediation owner"
                ),
            )
        if derived_receivable <= 0:
            raise HTTPException(status_code=409, detail="Invoice has no receivable")
        effects = (
            InvoiceClosureLedgerEffect(
                reverses_ledger_entry_id=None,
                result_entry_type=LedgerEntryType.credit,
                result_source=LedgerSource.adjustment,
                amount=derived_receivable,
                currency=invoice.currency,
            ),
        )
        status_after = InvoiceStatus.written_off
        receivable_before = derived_receivable
        access_consequence = "recheck_after_receivable_closure"
    else:
        if invoice.status not in {
            InvoiceStatus.draft,
            InvoiceStatus.issued,
            InvoiceStatus.overdue,
            InvoiceStatus.partially_paid,
            InvoiceStatus.paid,
        }:
            raise HTTPException(status_code=409, detail="Invoice cannot be voided")
        if invoice.status == InvoiceStatus.paid and derived_receivable > 0:
            raise HTTPException(
                status_code=409,
                detail="A paid invoice must be reversed through payment/credit owners",
            )
        from app.services.billing.account_credit import AccountCreditApplications

        release_preview = AccountCreditApplications.preview_invoice_void_release(
            db, invoice.id
        )
        if (
            credits_applied > 0
            or settlement.opening_funding_applied > 0
            or release_preview.amount != payments_applied
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    "Invoice has applied payment or credit value; reverse that "
                    "settlement through its owner before voiding"
                ),
            )
        debits = _invoice_debits_to_reverse(db, invoice)
        invoice_effects = tuple(
            InvoiceClosureLedgerEffect(
                reverses_ledger_entry_id=entry.id,
                result_entry_type=LedgerEntryType.credit,
                result_source=entry.source,
                amount=round_money(to_decimal(entry.amount)),
                currency=entry.currency,
            )
            for entry in debits
        )
        allocation_effects = tuple(
            InvoiceClosureLedgerEffect(
                reverses_ledger_entry_id=entry.original_entry_id,
                result_entry_type=entry.result_entry_type,
                result_source=entry.result_source,
                amount=entry.amount,
                currency=entry.currency,
            )
            for entry in release_preview.entries
        )
        effects = invoice_effects + allocation_effects
        released_allocation_ids = release_preview.allocation_ids
        status_after = InvoiceStatus.void
        receivable_before = (
            Decimal("0.00")
            if invoice.status == InvoiceStatus.draft
            else derived_receivable
        )
        access_consequence = (
            "none_draft_had_no_collectible_receivable"
            if invoice.status == InvoiceStatus.draft
            else "recheck_after_receivable_closure"
        )

    fingerprint = _closure_fingerprint(
        invoice=invoice,
        closure_type=closure_type,
        payments_applied=payments_applied,
        credits_applied=credits_applied,
        receivable_before=receivable_before,
        effects=effects,
    )
    return InvoiceClosurePreview(
        invoice_id=invoice.id,
        invoice_number=invoice.invoice_number,
        account_id=invoice.account_id,
        closure_type=closure_type,
        status_before=invoice.status,
        status_after=status_after,
        invoice_total=round_money(to_decimal(invoice.total)),
        payments_applied=payments_applied,
        credits_applied=credits_applied,
        receivable_before=receivable_before,
        receivable_after=Decimal("0.00"),
        closure_amount=receivable_before,
        currency=invoice.currency,
        released_allocation_ids=released_allocation_ids,
        ledger_effects=effects,
        access_consequence=access_consequence,
        fingerprint=fingerprint,
    )


def _stage_invoice_closure_audit(
    db: Session,
    *,
    closure: InvoiceClosure,
    preview: InvoiceClosurePreview,
) -> None:
    AuditEvents.stage(
        db,
        AuditEventCreate(
            action=closure.closure_type.value,
            entity_type="invoice",
            entity_id=str(closure.invoice_id),
            metadata_={
                "closure_id": str(closure.id),
                "amount": str(closure.amount),
                "currency": closure.currency,
                "payments_applied": str(closure.payments_applied),
                "credits_applied": str(closure.credits_applied),
                "receivable_before": str(closure.receivable_before),
                "receivable_after": str(closure.receivable_after),
                "preview_fingerprint": preview.fingerprint,
                "ledger_entry_ids": [
                    str(evidence.ledger_entry_id)
                    for evidence in closure.ledger_evidence
                ],
                "released_payment_allocation_ids": [
                    str(allocation_id)
                    for allocation_id in preview.released_allocation_ids
                ],
                "access_consequence": preview.access_consequence,
            },
        ),
    )


class Invoices(ListResponseMixin):
    @staticmethod
    def create_with_lines(
        db: Session,
        payload: InvoiceCreate,
        lines: tuple[InvoiceLineCreate, ...],
        *,
        commit: bool = True,
    ) -> Invoice:
        """Create one complete draft or issued document atomically."""
        if payload.status not in {InvoiceStatus.draft, InvoiceStatus.issued}:
            raise HTTPException(
                status_code=409,
                detail="Invoice construction starts only as draft or issued",
            )
        if payload.is_proforma and payload.status != InvoiceStatus.draft:
            raise HTTPException(
                status_code=409,
                detail="Proforma documents must remain drafts",
            )
        if not lines:
            raise HTTPException(
                status_code=400,
                detail="Invoice construction requires at least one line",
            )
        lock_account(db, str(payload.account_id))
        target_status = payload.status
        draft_payload = payload.model_copy(update={"status": InvoiceStatus.draft})
        try:
            invoice = Invoices.stage_admin_draft(db, draft_payload)
            InvoiceLines.replace_admin_draft_lines(
                db,
                invoice.id,
                tuple(
                    DraftInvoiceLineReplacement(
                        payload=line.model_copy(update={"invoice_id": invoice.id})
                    )
                    for line in lines
                ),
            )
            if target_status == InvoiceStatus.issued:
                invoice.status = InvoiceStatus.issued
                invoice.issued_at = payload.issued_at or datetime.now(UTC)
            AuditEvents.stage(
                db,
                AuditEventCreate(
                    action="create_invoice_with_lines",
                    entity_type="invoice",
                    entity_id=str(invoice.id),
                    metadata_={
                        "account_id": str(invoice.account_id),
                        "status": invoice.status.value,
                        "line_count": len(lines),
                        "total": str(invoice.total),
                        "financial_effect": "invoice_document_created",
                    },
                ),
            )
            emit_event(
                db,
                EventType.invoice_created,
                {
                    "invoice_id": str(invoice.id),
                    "invoice_number": invoice.invoice_number,
                    "amount": str(invoice.total),
                    "total": str(invoice.total),
                    "due_date": (
                        invoice.due_at.date().isoformat() if invoice.due_at else None
                    ),
                    "currency": invoice.currency,
                    "status": invoice.status.value,
                    "is_proforma": bool(invoice.is_proforma),
                },
                account_id=invoice.account_id,
                invoice_id=invoice.id,
            )
            _apply_available_account_credit(db, invoice)
            if commit:
                db.commit()
                db.refresh(invoice)
            else:
                db.flush()
            return invoice
        except Exception:
            if commit:
                db.rollback()
            raise

    @staticmethod
    def stage_admin_draft(db: Session, payload: InvoiceCreate) -> Invoice:
        """Stage one draft header for the contracted authoring coordinator."""
        if payload.status != InvoiceStatus.draft:
            raise DraftInvoiceParticipantError(
                "invoice_not_editable",
                "Administrative authoring can stage only draft invoices",
            )
        _validate_account(db, str(payload.account_id))
        data = payload.model_dump()
        if not data.get("invoice_number"):
            data["invoice_number"] = next_invoice_number(db)
        _validate_invoice_totals(data)
        invoice = Invoice(**data)
        db.add(invoice)
        db.flush()
        return invoice

    @staticmethod
    def stage_admin_draft_header(
        db: Session,
        invoice_id: UUID,
        payload: InvoiceUpdate,
    ) -> Invoice:
        """Stage explicit editable header fields without completing a transaction."""
        invoice = lock_for_update(db, Invoice, invoice_id)
        if invoice is None or not invoice.is_active:
            raise DraftInvoiceParticipantError(
                "invoice_not_found",
                "Invoice not found",
            )
        if invoice.status != InvoiceStatus.draft:
            raise DraftInvoiceParticipantError(
                "invoice_not_editable",
                "Only draft invoices can be edited",
            )
        data = payload.model_dump(exclude_unset=True)
        if "account_id" in data and data["account_id"] != invoice.account_id:
            raise DraftInvoiceParticipantError(
                "account_mismatch",
                "Invoice account cannot be changed",
            )
        if "currency" in data and data["currency"] != invoice.currency:
            raise DraftInvoiceParticipantError(
                "currency_mismatch",
                "Invoice currency cannot be changed",
            )
        allowed = {
            "invoice_number",
            "issued_at",
            "due_at",
            "memo",
            "is_proforma",
        }
        unexpected = set(data) - allowed - {"account_id", "currency"}
        if unexpected:
            raise DraftInvoiceParticipantError(
                "invoice_not_editable",
                "Draft header request contains owner-derived fields",
            )
        for key in allowed & set(data):
            setattr(invoice, key, data[key])
        invoice.updated_at = datetime.now(UTC)
        db.flush()
        return invoice

    @staticmethod
    def financial_summary(db: Session, invoice_id: str) -> InvoiceFinancialSummary:
        invoice = get_by_id(db, Invoice, invoice_id)
        if not invoice:
            raise HTTPException(status_code=404, detail="Invoice not found")
        settlement = resolve_invoice_settlement_amounts(db, invoice.id)
        return InvoiceFinancialSummary(
            currency=invoice.currency,
            invoice_total=invoice.total,
            receivable_balance=invoice.balance_due,
            payments_applied=settlement.payments_applied,
            credits_applied=settlement.credits_applied,
        )

    @staticmethod
    def preview_void(db: Session, invoice_id: str) -> InvoiceClosurePreview:
        invoice = get_by_id(db, Invoice, invoice_id)
        if not invoice:
            raise HTTPException(status_code=404, detail="Invoice not found")
        return _build_closure_preview(db, invoice, InvoiceClosureType.void)

    @staticmethod
    def preview_write_off(db: Session, invoice_id: str) -> InvoiceClosurePreview:
        invoice = get_by_id(db, Invoice, invoice_id)
        if not invoice:
            raise HTTPException(status_code=404, detail="Invoice not found")
        return _build_closure_preview(db, invoice, InvoiceClosureType.write_off)

    @staticmethod
    def void_capability(db: Session, invoice_id: str) -> InvoiceClosureCapability:
        try:
            Invoices.preview_void(db, invoice_id)
        except HTTPException as exc:
            return InvoiceClosureCapability(allowed=False, reason=str(exc.detail))
        return InvoiceClosureCapability(allowed=True)

    @staticmethod
    def write_off_capability(db: Session, invoice_id: str) -> InvoiceClosureCapability:
        try:
            Invoices.preview_write_off(db, invoice_id)
        except HTTPException as exc:
            return InvoiceClosureCapability(allowed=False, reason=str(exc.detail))
        return InvoiceClosureCapability(allowed=True)

    @staticmethod
    def _closure_replay(
        db: Session,
        *,
        closure_type: InvoiceClosureType,
        key: str,
        invoice_id: str,
        preview_fingerprint: str,
    ) -> InvoiceClosureResult | None:
        reservation = db.scalar(
            select(IdempotencyKey).where(
                IdempotencyKey.scope == _invoice_closure_scope(closure_type),
                IdempotencyKey.key == key,
            )
        )
        if reservation is None:
            return None
        if not reservation.ref_id:
            raise HTTPException(
                status_code=409, detail="Invoice closure is in progress"
            )
        closure = db.get(InvoiceClosure, coerce_uuid(reservation.ref_id))
        if closure is None or str(closure.invoice_id) != str(invoice_id):
            raise HTTPException(
                status_code=409,
                detail="Idempotency key belongs to another invoice closure",
            )
        if (
            closure.closure_type != closure_type
            or closure.preview_fingerprint != preview_fingerprint
        ):
            raise HTTPException(
                status_code=409,
                detail="Idempotency key was used with a different closure preview",
            )
        invoice = get_by_id(db, Invoice, closure.invoice_id)
        if invoice is None:
            raise HTTPException(status_code=409, detail="Invoice closure is incomplete")
        return InvoiceClosureResult(
            invoice=invoice,
            closure=closure,
            preview=None,
            idempotent_replay=True,
        )

    @staticmethod
    def _confirm_closure(
        db: Session,
        invoice_id: str,
        payload: InvoiceClosureConfirm,
        *,
        closure_type: InvoiceClosureType,
        origin: InvoiceClosureOrigin,
        commit: bool,
        stage_audit: bool,
        reconcile_access: bool,
    ) -> InvoiceClosureResult:
        key = _normalize_closure_key(payload.idempotency_key)
        replay = Invoices._closure_replay(
            db,
            closure_type=closure_type,
            key=key,
            invoice_id=invoice_id,
            preview_fingerprint=payload.preview_fingerprint,
        )
        if replay:
            return replay
        initial = get_by_id(db, Invoice, invoice_id)
        if not initial:
            raise HTTPException(status_code=404, detail="Invoice not found")
        lock_account(db, str(initial.account_id))
        invoice = lock_for_update(db, Invoice, coerce_uuid(invoice_id))
        if not invoice:
            raise HTTPException(status_code=404, detail="Invoice not found")
        preview = _build_closure_preview(db, invoice, closure_type)
        if preview.fingerprint != payload.preview_fingerprint:
            raise HTTPException(
                status_code=409,
                detail="Financial state changed after preview; preview again",
            )
        reservation = IdempotencyKey(
            scope=_invoice_closure_scope(closure_type),
            key=key,
            account_id=invoice.account_id,
        )
        db.add(reservation)
        try:
            db.flush()
            ledger_results: list[tuple[LedgerEntry, UUID | None]] = []
            if closure_type == InvoiceClosureType.write_off:
                effect = preview.ledger_effects[0]
                entry = LedgerEntries.create(
                    db,
                    LedgerEntryCreate(
                        account_id=invoice.account_id,
                        invoice_id=invoice.id,
                        entry_type=effect.result_entry_type,
                        source=effect.result_source,
                        amount=effect.amount,
                        currency=effect.currency,
                        memo=payload.memo
                        or f"Invoice write-off {invoice.invoice_number or invoice.id}",
                    ),
                    commit=False,
                )
                ledger_results.append((entry, None))
            else:
                release_entry_ids: set[UUID] = set()
                if preview.released_allocation_ids:
                    from app.services.billing.account_credit import (
                        AccountCreditApplications,
                    )

                    release_preview = (
                        AccountCreditApplications.preview_invoice_void_release(
                            db, invoice.id
                        )
                    )
                    if (
                        release_preview.allocation_ids
                        != preview.released_allocation_ids
                    ):
                        raise HTTPException(
                            status_code=409,
                            detail="Account-credit allocation evidence changed",
                        )
                    release_entry_ids = {
                        entry.original_entry_id for entry in release_preview.entries
                    }
                for effect in preview.ledger_effects:
                    assert effect.reverses_ledger_entry_id is not None
                    if effect.reverses_ledger_entry_id in release_entry_ids:
                        continue
                    reversal = LedgerEntries.reverse(
                        db,
                        str(effect.reverses_ledger_entry_id),
                        memo=payload.memo
                        or f"Void invoice {invoice.invoice_number or invoice.id}",
                        commit=False,
                    )
                    ledger_results.append((reversal, effect.reverses_ledger_entry_id))

                if preview.released_allocation_ids:
                    ledger_results.extend(
                        AccountCreditApplications.release_for_invoice_void(
                            db,
                            invoice_id=invoice.id,
                            expected_allocation_ids=preview.released_allocation_ids,
                            memo=payload.memo
                            or f"Void invoice {invoice.invoice_number or invoice.id}",
                        )
                    )

            closure = InvoiceClosure(
                invoice_id=invoice.id,
                closure_type=closure_type,
                origin=origin,
                amount=preview.closure_amount,
                receivable_before=preview.receivable_before,
                receivable_after=preview.receivable_after,
                payments_applied=preview.payments_applied,
                credits_applied=preview.credits_applied,
                currency=preview.currency,
                reason=payload.memo,
                preview_fingerprint=preview.fingerprint,
                idempotency_key=key,
            )
            db.add(closure)
            db.flush()
            evidence_rows: list[InvoiceClosureLedgerEvidence] = []
            for entry, original_id in ledger_results:
                evidence = InvoiceClosureLedgerEvidence(
                    closure_id=closure.id,
                    ledger_entry_id=entry.id,
                    reverses_ledger_entry_id=original_id,
                )
                db.add(evidence)
                evidence_rows.append(evidence)
            db.flush()

            if closure_type == InvoiceClosureType.void:
                invoice.status = InvoiceStatus.void
            else:
                invoice.status = InvoiceStatus.written_off
            invoice.balance_due = preview.receivable_after
            invoice.paid_at = None
            if payload.memo:
                invoice.memo = payload.memo
            reservation.ref_id = str(closure.id)
            db.flush()
            closure.ledger_evidence = evidence_rows
            if stage_audit:
                _stage_invoice_closure_audit(db, closure=closure, preview=preview)
            if (
                reconcile_access
                and preview.access_consequence == "recheck_after_receivable_closure"
            ):
                reconcile_service_after_invoice_settlement(
                    db, invoice.account_id, invoice.id
                )
            if commit:
                db.commit()
                db.refresh(invoice)
                db.refresh(closure)
            else:
                db.flush()
            return InvoiceClosureResult(
                invoice=invoice,
                closure=closure,
                preview=preview,
            )
        except IntegrityError as exc:
            db.rollback()
            replay = Invoices._closure_replay(
                db,
                closure_type=closure_type,
                key=key,
                invoice_id=invoice_id,
                preview_fingerprint=payload.preview_fingerprint,
            )
            if replay:
                return replay
            raise HTTPException(
                status_code=409, detail="Invoice already has terminal closure evidence"
            ) from exc
        except Exception:
            db.rollback()
            raise

    @staticmethod
    def confirm_void(
        db: Session,
        invoice_id: str,
        payload: InvoiceClosureConfirm,
        *,
        origin: InvoiceClosureOrigin = InvoiceClosureOrigin.manual,
        commit: bool = True,
        stage_audit: bool = True,
        reconcile_access: bool = True,
    ) -> InvoiceClosureResult:
        return Invoices._confirm_closure(
            db,
            invoice_id,
            payload,
            closure_type=InvoiceClosureType.void,
            origin=origin,
            commit=commit,
            stage_audit=stage_audit,
            reconcile_access=reconcile_access,
        )

    @staticmethod
    def confirm_write_off(
        db: Session,
        invoice_id: str,
        payload: InvoiceClosureConfirm,
        *,
        origin: InvoiceClosureOrigin = InvoiceClosureOrigin.manual,
        commit: bool = True,
        stage_audit: bool = True,
        reconcile_access: bool = True,
    ) -> InvoiceClosureResult:
        return Invoices._confirm_closure(
            db,
            invoice_id,
            payload,
            closure_type=InvoiceClosureType.write_off,
            origin=origin,
            commit=commit,
            stage_audit=stage_audit,
            reconcile_access=reconcile_access,
        )

    @staticmethod
    def void_system(
        db: Session,
        invoice_id: str,
        *,
        reason: str,
        idempotency_key: str,
        commit: bool = True,
        reconcile_access: bool = True,
    ) -> InvoiceClosureResult:
        preview = Invoices.preview_void(db, invoice_id)
        return Invoices.confirm_void(
            db,
            invoice_id,
            InvoiceClosureConfirm(
                preview_fingerprint=preview.fingerprint,
                idempotency_key=idempotency_key,
                memo=reason,
            ),
            origin=InvoiceClosureOrigin.system,
            commit=commit,
            reconcile_access=reconcile_access,
        )

    @staticmethod
    def void_pristine_draft_for_owner(
        db: Session,
        invoice_id: str,
        *,
        reason: str,
        idempotency_key: str,
    ) -> InvoiceClosureResult:
        """Stage a pristine draft closure without ending the owner's transaction."""
        initial = get_by_id(db, Invoice, invoice_id)
        if initial is None:
            raise InvoiceOwnerError(
                code="financial.invoice.invoice_not_found",
                message="Invoice was not found.",
            )
        lock_account(db, str(initial.account_id))
        invoice = lock_for_update(db, Invoice, coerce_uuid(invoice_id))
        if invoice is None:
            raise InvoiceOwnerError(
                code="financial.invoice.invoice_not_found",
                message="Invoice was not found.",
            )
        try:
            preview = _build_closure_preview(db, invoice, InvoiceClosureType.void)
        except HTTPException as exc:
            raise InvoiceOwnerError(
                code="financial.invoice.void_rejected",
                message="Invoice owner rejected the void transition.",
                details={"reason": str(exc.detail)},
            ) from exc
        if (
            preview.status_before != InvoiceStatus.draft
            or preview.payments_applied != Decimal("0.00")
            or preview.credits_applied != Decimal("0.00")
            or preview.released_allocation_ids
            or preview.ledger_effects
        ):
            raise InvoiceOwnerError(
                code="financial.invoice.draft_not_pristine",
                message="Only a pristine draft can use the composed void transition.",
            )
        closure = InvoiceClosure(
            invoice_id=invoice.id,
            closure_type=InvoiceClosureType.void,
            origin=InvoiceClosureOrigin.system,
            amount=preview.closure_amount,
            receivable_before=preview.receivable_before,
            receivable_after=preview.receivable_after,
            payments_applied=preview.payments_applied,
            credits_applied=preview.credits_applied,
            currency=preview.currency,
            reason=reason,
            preview_fingerprint=preview.fingerprint,
            idempotency_key=idempotency_key,
        )
        db.add(closure)
        db.flush()
        invoice.status = InvoiceStatus.void
        invoice.balance_due = preview.receivable_after
        invoice.paid_at = None
        invoice.memo = reason
        _stage_invoice_closure_audit(db, closure=closure, preview=preview)
        db.flush()
        return InvoiceClosureResult(
            invoice=invoice,
            closure=closure,
            preview=preview,
        )

    @staticmethod
    def write_off_system(
        db: Session,
        invoice_id: str,
        *,
        reason: str,
        idempotency_key: str,
        commit: bool = True,
        reconcile_access: bool = True,
    ) -> InvoiceClosureResult:
        preview = Invoices.preview_write_off(db, invoice_id)
        return Invoices.confirm_write_off(
            db,
            invoice_id,
            InvoiceClosureConfirm(
                preview_fingerprint=preview.fingerprint,
                idempotency_key=idempotency_key,
                memo=reason,
            ),
            origin=InvoiceClosureOrigin.system,
            commit=commit,
            reconcile_access=reconcile_access,
        )

    @staticmethod
    def inspect_closure_evidence(
        db: Session, invoice_id: str
    ) -> InvoiceClosureEvidenceInspection:
        invoice = get_by_id(db, Invoice, invoice_id)
        if not invoice:
            raise HTTPException(status_code=404, detail="Invoice not found")
        if invoice.closure is not None:
            raise HTTPException(
                status_code=409, detail="Invoice already has closure evidence"
            )
        if invoice.status == InvoiceStatus.void:
            closure_type = InvoiceClosureType.void
        elif invoice.status == InvoiceStatus.written_off:
            closure_type = InvoiceClosureType.write_off
        else:
            raise HTTPException(
                status_code=409,
                detail="Only a historical void or written-off invoice can be reconciled",
            )
        settlement = resolve_invoice_settlement_amounts(db, invoice.id)
        expected_amount = max(
            Decimal("0.00"),
            round_money(to_decimal(invoice.total) - settlement.total_applied),
        )
        entries = (
            db.query(LedgerEntry)
            .filter(LedgerEntry.invoice_id == invoice.id)
            .filter(LedgerEntry.entry_type == LedgerEntryType.credit)
            .filter(
                LedgerEntry.source.in_([LedgerSource.adjustment, LedgerSource.invoice])
            )
            .order_by(LedgerEntry.created_at.asc(), LedgerEntry.id.asc())
            .all()
        )
        candidates = tuple(
            InvoiceClosureEvidenceCandidate(
                ledger_entry_id=entry.id,
                entry_type=entry.entry_type,
                source=entry.source,
                amount=round_money(to_decimal(entry.amount)),
                currency=entry.currency,
                reversal_of_entry_id=entry.reversal_of_entry_id,
            )
            for entry in entries
        )
        encoded = json.dumps(
            {
                "invoice_id": str(invoice.id),
                "closure_type": closure_type.value,
                "status": invoice.status.value,
                "invoice_total": f"{round_money(to_decimal(invoice.total)):.2f}",
                "expected_amount": f"{expected_amount:.2f}",
                "currency": invoice.currency,
                "candidates": [
                    {
                        "ledger_entry_id": str(candidate.ledger_entry_id),
                        "source": candidate.source.value,
                        "amount": f"{candidate.amount:.2f}",
                        "currency": candidate.currency,
                        "reversal_of_entry_id": (
                            str(candidate.reversal_of_entry_id)
                            if candidate.reversal_of_entry_id
                            else None
                        ),
                    }
                    for candidate in candidates
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return InvoiceClosureEvidenceInspection(
            invoice_id=invoice.id,
            closure_type=closure_type,
            expected_amount=expected_amount,
            currency=invoice.currency,
            candidates=candidates,
            fingerprint=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        )

    @staticmethod
    def _closure_reconciliation_replay(
        db: Session,
        *,
        key: str,
        invoice_id: str,
        payload: InvoiceClosureReconciliationRequest,
    ) -> InvoiceClosureResult | None:
        reservation = db.scalar(
            select(IdempotencyKey).where(
                IdempotencyKey.scope == _RECONCILIATION_IDEMPOTENCY_SCOPE,
                IdempotencyKey.key == key,
            )
        )
        if reservation is None:
            return None
        if not reservation.ref_id:
            raise HTTPException(
                status_code=409, detail="Invoice evidence reconciliation is in progress"
            )
        closure = db.get(InvoiceClosure, coerce_uuid(reservation.ref_id))
        if (
            closure is None
            or str(closure.invoice_id) != str(invoice_id)
            or closure.closure_type != payload.closure_type
            or closure.preview_fingerprint != payload.preview_fingerprint
        ):
            raise HTTPException(
                status_code=409,
                detail="Idempotency key was used with different closure evidence",
            )
        invoice = get_by_id(db, Invoice, invoice_id)
        if not invoice:
            raise HTTPException(status_code=409, detail="Invoice closure is incomplete")
        return InvoiceClosureResult(
            invoice=invoice,
            closure=closure,
            preview=None,
            idempotent_replay=True,
        )

    @staticmethod
    def reconcile_closure_evidence(
        db: Session,
        invoice_id: str,
        payload: InvoiceClosureReconciliationRequest,
    ) -> InvoiceClosureResult:
        key = _normalize_closure_key(payload.idempotency_key)
        replay = Invoices._closure_reconciliation_replay(
            db, key=key, invoice_id=invoice_id, payload=payload
        )
        if replay:
            return replay
        initial = get_by_id(db, Invoice, invoice_id)
        if not initial:
            raise HTTPException(status_code=404, detail="Invoice not found")
        lock_account(db, str(initial.account_id))
        invoice = lock_for_update(db, Invoice, coerce_uuid(invoice_id))
        if not invoice:
            raise HTTPException(status_code=404, detail="Invoice not found")
        inspection = Invoices.inspect_closure_evidence(db, invoice_id)
        if (
            inspection.closure_type != payload.closure_type
            or inspection.fingerprint != payload.preview_fingerprint
        ):
            raise HTTPException(
                status_code=409,
                detail="Historical evidence changed after inspection; inspect again",
            )
        selected_ids = [selection.ledger_entry_id for selection in payload.evidence]
        if len(selected_ids) != len(set(selected_ids)):
            raise HTTPException(status_code=400, detail="Duplicate ledger evidence")
        candidates = {
            candidate.ledger_entry_id: candidate for candidate in inspection.candidates
        }
        selected_pairs: list[tuple[LedgerEntry, UUID | None]] = []
        for selection in payload.evidence:
            if selection.ledger_entry_id not in candidates:
                raise HTTPException(
                    status_code=409,
                    detail="Selected ledger entry is not an inspected candidate",
                )
            entry = db.get(LedgerEntry, selection.ledger_entry_id)
            if entry is None or entry.invoice_id != invoice.id:
                raise HTTPException(status_code=409, detail="Ledger evidence mismatch")
            selected_pairs.append((entry, selection.reverses_ledger_entry_id))

        if payload.closure_type == InvoiceClosureType.write_off:
            if len(selected_pairs) != 1 or selected_pairs[0][1] is not None:
                raise HTTPException(
                    status_code=409,
                    detail="Write-off reconciliation requires one exact credit entry",
                )
            entry = selected_pairs[0][0]
            if (
                entry.source != LedgerSource.adjustment
                or entry.entry_type != LedgerEntryType.credit
                or entry.currency != invoice.currency
                or round_money(to_decimal(entry.amount)) != inspection.expected_amount
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Selected entry is not the exact historical write-off",
                )
        else:
            original_debits = (
                db.query(LedgerEntry)
                .filter(LedgerEntry.invoice_id == invoice.id)
                .filter(LedgerEntry.entry_type == LedgerEntryType.debit)
                .filter(
                    LedgerEntry.source.in_(
                        [LedgerSource.invoice, LedgerSource.adjustment]
                    )
                )
                .all()
            )
            original_ids = {entry.id for entry in original_debits}
            selected_original_ids = {
                original_id
                for _, original_id in selected_pairs
                if original_id is not None
            }
            if (
                any(original_id is None for _, original_id in selected_pairs)
                or len(selected_pairs) != len(original_ids)
                or selected_original_ids != original_ids
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Void reconciliation requires one exact reversal for every "
                        "original invoice debit"
                    ),
                )
            originals = {entry.id: entry for entry in original_debits}
            for entry, original_id in selected_pairs:
                assert original_id is not None
                original = originals[original_id]
                if (
                    entry.entry_type != LedgerEntryType.credit
                    or entry.currency != original.currency
                    or round_money(to_decimal(entry.amount))
                    != round_money(to_decimal(original.amount))
                    or (
                        entry.reversal_of_entry_id is not None
                        and entry.reversal_of_entry_id != original.id
                    )
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="Selected entry is not an exact reversal of its debit",
                    )

        reservation = IdempotencyKey(
            scope=_RECONCILIATION_IDEMPOTENCY_SCOPE,
            key=key,
            account_id=invoice.account_id,
        )
        db.add(reservation)
        try:
            closure = InvoiceClosure(
                invoice_id=invoice.id,
                closure_type=payload.closure_type,
                origin=InvoiceClosureOrigin.historical_reconciliation,
                amount=inspection.expected_amount,
                receivable_before=inspection.expected_amount,
                receivable_after=Decimal("0.00"),
                payments_applied=resolve_invoice_settlement_amounts(
                    db, invoice.id
                ).payments_applied,
                credits_applied=resolve_invoice_settlement_amounts(
                    db, invoice.id
                ).credits_applied,
                currency=invoice.currency,
                reason=payload.reason,
                preview_fingerprint=inspection.fingerprint,
                idempotency_key=key,
            )
            db.add(closure)
            db.flush()
            evidence_rows = [
                InvoiceClosureLedgerEvidence(
                    closure_id=closure.id,
                    ledger_entry_id=entry.id,
                    reverses_ledger_entry_id=original_id,
                )
                for entry, original_id in selected_pairs
            ]
            db.add_all(evidence_rows)
            db.flush()
            closure.ledger_evidence = evidence_rows
            reservation.ref_id = str(closure.id)
            AuditEvents.stage(
                db,
                AuditEventCreate(
                    action="reconcile_invoice_closure_evidence",
                    entity_type="invoice",
                    entity_id=str(invoice.id),
                    metadata_={
                        "closure_id": str(closure.id),
                        "closure_type": closure.closure_type.value,
                        "ledger_entry_ids": [
                            str(evidence.ledger_entry_id) for evidence in evidence_rows
                        ],
                        "money_posted": "none",
                        "preview_fingerprint": inspection.fingerprint,
                    },
                ),
            )
            db.commit()
            db.refresh(closure)
            return InvoiceClosureResult(
                invoice=invoice,
                closure=closure,
                preview=None,
            )
        except IntegrityError as exc:
            db.rollback()
            replay = Invoices._closure_reconciliation_replay(
                db, key=key, invoice_id=invoice_id, payload=payload
            )
            if replay:
                return replay
            raise HTTPException(
                status_code=409, detail="Invoice closure evidence already exists"
            ) from exc

    @staticmethod
    def stage_system_invoice(
        db: Session,
        payload: InvoiceCreate,
        *,
        reason: str,
    ) -> Invoice:
        """Stage an automation-owned draft or issued invoice in its transaction."""
        if payload.status not in {InvoiceStatus.draft, InvoiceStatus.issued}:
            raise HTTPException(
                status_code=409,
                detail="System invoice creation starts only as draft or issued",
            )
        _validate_account(db, str(payload.account_id))
        data = payload.model_dump()
        if not data.get("invoice_number"):
            data["invoice_number"] = next_invoice_number(db)
        _validate_invoice_totals(data)
        invoice = Invoice(**data)
        db.add(invoice)
        db.flush()
        AuditEvents.stage(
            db,
            AuditEventCreate(
                action="stage_system_invoice",
                entity_type="invoice",
                entity_id=str(invoice.id),
                metadata_={
                    "account_id": str(invoice.account_id),
                    "status": invoice.status.value,
                    "reason": reason,
                    "financial_effect": "invoice_document_staged",
                    "ledger_transaction_id": None,
                },
            ),
        )
        _apply_available_account_credit(db, invoice)
        return invoice

    @staticmethod
    def stage_system_invoice_for_owner(
        db: Session,
        payload: InvoiceCreate,
        *,
        reason: str,
    ) -> Invoice:
        """Stage an invoice as a flush-only participant for a command owner."""

        try:
            return Invoices.stage_system_invoice(db, payload, reason=reason)
        except HTTPException as exc:
            raise InvoiceOwnerError(
                code="financial.invoice.stage_rejected",
                message="Invoice owner rejected system document construction.",
                details={"reason": str(exc.detail)},
            ) from exc

    @staticmethod
    def issue_draft_system(
        db: Session,
        invoice_id: str,
        *,
        issued_at: datetime,
        due_at: datetime | None,
        reason: str,
        announce: bool = False,
        apply_available_credit: bool = True,
        require_full_available_credit: bool = False,
        commit: bool = False,
    ) -> InvoiceLifecycleTransitionResult:
        """Own the deterministic draft -> issued transition.

        ``apply_available_credit=False`` is reserved for domain owners that
        immediately compose a stricter allocation policy, such as prepaid
        draft-until-fully-funded renewal. Thin adapters must keep the default.
        ``require_full_available_credit=True`` narrows the automatic credit
        application to exact full settlement and leaves underfunded credit
        untouched.
        """
        invoice = lock_for_update(db, Invoice, invoice_id)
        if invoice is None:
            raise HTTPException(status_code=404, detail="Invoice not found")
        if invoice.status == InvoiceStatus.issued:
            return InvoiceLifecycleTransitionResult(invoice=invoice, changed=False)
        if invoice.status != InvoiceStatus.draft:
            raise HTTPException(
                status_code=409,
                detail="Only a draft invoice can be issued by automation",
            )
        if invoice.is_proforma:
            raise HTTPException(
                status_code=409,
                detail="Convert the proforma before issuing an invoice",
            )
        invoice.status = InvoiceStatus.issued
        invoice.issued_at = issued_at
        invoice.due_at = due_at
        AuditEvents.stage(
            db,
            AuditEventCreate(
                action="issue_invoice_system",
                entity_type="invoice",
                entity_id=str(invoice.id),
                metadata_={
                    "from_status": InvoiceStatus.draft.value,
                    "to_status": InvoiceStatus.issued.value,
                    "reason": reason,
                    "issued_at": issued_at.isoformat(),
                    "due_at": due_at.isoformat() if due_at else None,
                    "ledger_transaction_id": None,
                    "service_access_consequence": "none",
                },
            ),
        )
        if announce:
            emit_event(
                db,
                EventType.invoice_sent,
                {
                    "invoice_id": str(invoice.id),
                    "invoice_number": invoice.invoice_number,
                    "amount": str(invoice.total),
                    "total": str(invoice.total),
                    "due_date": (
                        invoice.due_at.date().isoformat() if invoice.due_at else None
                    ),
                    "currency": invoice.currency,
                    "from_status": InvoiceStatus.draft.value,
                    "to_status": InvoiceStatus.issued.value,
                },
                account_id=invoice.account_id,
                invoice_id=invoice.id,
            )
        if apply_available_credit and require_full_available_credit:
            _apply_available_account_credit_when_fully_funded(db, invoice)
        elif apply_available_credit:
            _apply_available_account_credit(db, invoice)
        if commit:
            db.commit()
            db.refresh(invoice)
        else:
            db.flush()
        return InvoiceLifecycleTransitionResult(
            invoice=invoice,
            changed=True,
            event_emitted=announce,
        )

    @staticmethod
    def convert_proforma_for_owner(
        db: Session,
        conversion: ProformaConversionInput,
    ) -> InvoiceLifecycleTransitionResult:
        """Convert one locked proforma inside its coordinator transaction."""

        invoice = lock_for_update(db, Invoice, conversion.invoice_id)
        if invoice is None or not invoice.is_active:
            raise InvoiceOwnerError(
                code="financial.invoice.proforma_not_found",
                message="Proforma invoice was not found.",
                details={"invoice_id": str(conversion.invoice_id)},
            )
        if invoice.status != InvoiceStatus.draft:
            raise InvoiceOwnerError(
                code="financial.invoice.proforma_not_draft",
                message="Only a draft proforma can be converted.",
                details={
                    "invoice_id": str(invoice.id),
                    "status": invoice.status.value,
                },
            )

        invoice.invoice_number = conversion.invoice_number
        invoice.memo = conversion.memo
        invoice.is_proforma = False
        invoice.status = InvoiceStatus.issued
        invoice.issued_at = conversion.issued_at
        emit_event(
            db,
            EventType.invoice_sent,
            {
                "invoice_id": str(invoice.id),
                "invoice_number": invoice.invoice_number,
                "amount": str(invoice.total),
                "total": str(invoice.total),
                "due_date": invoice.due_at.date().isoformat()
                if invoice.due_at
                else None,
                "currency": invoice.currency,
                "from_status": InvoiceStatus.draft.value,
                "to_status": InvoiceStatus.issued.value,
                "reason": conversion.reason,
            },
            account_id=invoice.account_id,
            invoice_id=invoice.id,
        )
        _apply_available_account_credit(db, invoice)
        db.flush()
        return InvoiceLifecycleTransitionResult(
            invoice=invoice,
            changed=True,
            event_emitted=True,
        )

    @staticmethod
    def adopt_prepaid_proforma_document_for_owner(
        db: Session,
        adoption: PrepaidProformaDocumentAdoption,
    ) -> Invoice:
        """Apply a reconciler-approved identity to one pristine proforma.

        This is a flush-only participant. The prepaid reconciliation owner
        decides whether the evidence is sufficient and owns the surrounding
        transaction, audit trail, event, and idempotency reservation.
        """

        invoice = lock_for_update(db, Invoice, str(adoption.invoice_id))
        if (
            invoice is None
            or not invoice.is_active
            or invoice.status is not InvoiceStatus.draft
            or not invoice.is_proforma
            or invoice.billing_period_start is not None
            or invoice.billing_period_end is not None
        ):
            raise InvoiceOwnerError(
                code="financial.invoice.proforma_adoption_rejected",
                message="Invoice is not a pristine active proforma draft.",
                details={"invoice_id": str(adoption.invoice_id)},
            )
        if adoption.billing_period_end <= adoption.billing_period_start:
            raise InvoiceOwnerError(
                code="financial.invoice.proforma_adoption_rejected",
                message="Adopted billing period must be positive.",
                details={"invoice_id": str(adoption.invoice_id)},
            )
        line = db.scalar(
            select(InvoiceLine)
            .where(
                InvoiceLine.id == adoption.line_id,
                InvoiceLine.invoice_id == invoice.id,
                InvoiceLine.is_active.is_(True),
            )
            .with_for_update()
        )
        if line is None or line.subscription_id is not None:
            raise InvoiceOwnerError(
                code="financial.invoice.proforma_adoption_rejected",
                message="Proforma line identity changed after review.",
                details={
                    "invoice_id": str(adoption.invoice_id),
                    "line_id": str(adoption.line_id),
                },
            )

        invoice.is_proforma = False
        invoice.billing_period_start = adoption.billing_period_start
        invoice.billing_period_end = adoption.billing_period_end
        line.subscription_id = adoption.subscription_id
        line.description = adoption.line_description
        line_metadata = dict(line.metadata_ or {})
        line_metadata.update(
            {
                "kind": "base_subscription",
                "billing_period_start": adoption.billing_period_start.isoformat(),
                "billing_period_end": adoption.billing_period_end.isoformat(),
                "prepaid_proforma_adoption_ref": adoption.adoption_evidence_ref,
            }
        )
        line.metadata_ = line_metadata
        db.flush()
        return invoice

    @staticmethod
    def repair_paid_prepaid_document_for_owner(
        db: Session,
        repair: PaidPrepaidInvoiceDocumentRepair,
    ) -> Invoice:
        """Apply reviewed identity to one already-paid historical invoice.

        This flush-only participant changes no invoice status, balance, total,
        allocation, or ledger fact. The prepaid reconciliation owner proves the
        exact settlement and owns the surrounding transaction and consequences.
        """

        invoice = lock_for_update(db, Invoice, str(repair.invoice_id))
        if (
            invoice is None
            or not invoice.is_active
            or invoice.status is not InvoiceStatus.paid
            or invoice.is_proforma
            or invoice.balance_due != Decimal("0.00")
            or invoice.billing_period_start is not None
            or invoice.billing_period_end is not None
        ):
            raise InvoiceOwnerError(
                code="financial.invoice.paid_prepaid_document_repair_rejected",
                message=(
                    "Invoice is not an active paid document with missing period identity."
                ),
                details={"invoice_id": str(repair.invoice_id)},
            )
        if repair.billing_period_end <= repair.billing_period_start:
            raise InvoiceOwnerError(
                code="financial.invoice.paid_prepaid_document_repair_rejected",
                message="Repaired billing period must be positive.",
                details={"invoice_id": str(repair.invoice_id)},
            )
        line = db.scalar(
            select(InvoiceLine)
            .where(
                InvoiceLine.id == repair.line_id,
                InvoiceLine.invoice_id == invoice.id,
                InvoiceLine.is_active.is_(True),
            )
            .with_for_update()
        )
        if line is None or line.subscription_id is not None:
            raise InvoiceOwnerError(
                code="financial.invoice.paid_prepaid_document_repair_rejected",
                message="Paid invoice line identity changed after review.",
                details={
                    "invoice_id": str(repair.invoice_id),
                    "line_id": str(repair.line_id),
                },
            )

        invoice.billing_period_start = repair.billing_period_start
        invoice.billing_period_end = repair.billing_period_end
        line.subscription_id = repair.subscription_id
        line.description = repair.line_description
        line_metadata = dict(line.metadata_ or {})
        line_metadata.update(
            {
                "kind": "base_subscription",
                "billing_period_start": repair.billing_period_start.isoformat(),
                "billing_period_end": repair.billing_period_end.isoformat(),
                "paid_prepaid_invoice_repair_ref": repair.repair_evidence_ref,
            }
        )
        line.metadata_ = line_metadata
        db.flush()
        return invoice

    @staticmethod
    def issue_draft_for_owner(
        db: Session,
        invoice_id: str,
        *,
        issued_at: datetime,
        due_at: datetime | None,
        reason: str,
        announce: bool = False,
        apply_available_credit: bool = True,
    ) -> InvoiceLifecycleTransitionResult:
        """Compose draft issuance inside another owner without HTTP coupling."""
        try:
            return Invoices.issue_draft_system(
                db,
                invoice_id,
                issued_at=issued_at,
                due_at=due_at,
                reason=reason,
                announce=announce,
                apply_available_credit=apply_available_credit,
                commit=False,
            )
        except HTTPException as exc:
            raise InvoiceOwnerError(
                code="financial.invoice.issue_rejected",
                message="Invoice owner rejected the issue transition.",
                details={"reason": str(exc.detail)},
            ) from exc

    @staticmethod
    def announce_issued(
        db: Session,
        invoice_id: str,
        *,
        reason: str,
        commit: bool = False,
    ) -> Invoice:
        """Emit the canonical invoice notification event for an issued document."""

        invoice = lock_for_update(db, Invoice, invoice_id)
        if invoice is None or not invoice.is_active:
            raise HTTPException(status_code=404, detail="Invoice not found")
        if invoice.is_proforma or invoice.status in {
            InvoiceStatus.draft,
            InvoiceStatus.void,
            InvoiceStatus.written_off,
        }:
            raise HTTPException(
                status_code=409,
                detail="Only an issued invoice can be announced",
            )
        emit_event(
            db,
            EventType.invoice_sent,
            {
                "invoice_id": str(invoice.id),
                "invoice_number": invoice.invoice_number,
                "amount": str(invoice.total),
                "total": str(invoice.total),
                "balance_due": str(invoice.balance_due),
                "due_date": (
                    invoice.due_at.date().isoformat() if invoice.due_at else None
                ),
                "currency": invoice.currency,
                "status": invoice.status.value,
                "reason": reason,
            },
            account_id=invoice.account_id,
            invoice_id=invoice.id,
        )
        if commit:
            db.commit()
            db.refresh(invoice)
        else:
            db.flush()
        return invoice

    @staticmethod
    def return_unfunded_prepaid_to_draft_system(
        db: Session,
        invoice_id: str,
        *,
        reason: str,
        commit: bool = False,
    ) -> InvoiceLifecycleTransitionResult:
        """Retire an unfunded prepaid receivable without touching money state."""
        from app.models.catalog import BillingMode, Subscription
        from app.models.subscriber import Subscriber

        invoice = lock_for_update(db, Invoice, invoice_id)
        if invoice is None:
            raise HTTPException(status_code=404, detail="Invoice not found")
        if invoice.status == InvoiceStatus.draft:
            return InvoiceLifecycleTransitionResult(invoice=invoice, changed=False)
        if invoice.status not in {InvoiceStatus.issued, InvoiceStatus.overdue}:
            raise HTTPException(
                status_code=409,
                detail="Only an unpaid issued prepaid invoice can return to draft",
            )
        settlement = resolve_invoice_settlement_amounts(db, invoice.id)
        if settlement.total_applied > 0:
            raise HTTPException(
                status_code=409,
                detail="Invoice has financial activity and cannot return to draft",
            )
        prepaid_line_exists = (
            db.query(InvoiceLine.id)
            .join(Subscription, Subscription.id == InvoiceLine.subscription_id)
            .filter(InvoiceLine.invoice_id == invoice.id)
            .filter(InvoiceLine.is_active.is_(True))
            .filter(Subscription.billing_mode == BillingMode.prepaid)
            .first()
            is not None
        )
        account = db.get(Subscriber, invoice.account_id)
        prepaid_account_scope = bool(
            account is not None and account.billing_mode == BillingMode.prepaid
        ) or (
            db.query(Subscription.id)
            .filter(Subscription.subscriber_id == invoice.account_id)
            .filter(Subscription.billing_mode == BillingMode.prepaid)
            .first()
            is not None
        )
        if not (prepaid_line_exists or prepaid_account_scope):
            raise HTTPException(
                status_code=409,
                detail="Only a prepaid invoice can return to draft",
            )
        previous_status = invoice.status
        invoice.status = InvoiceStatus.draft
        invoice.issued_at = None
        invoice.due_at = None
        invoice.paid_at = None
        AuditEvents.stage(
            db,
            AuditEventCreate(
                action="return_unfunded_prepaid_invoice_to_draft",
                entity_type="invoice",
                entity_id=str(invoice.id),
                metadata_={
                    "from_status": previous_status.value,
                    "to_status": InvoiceStatus.draft.value,
                    "reason": reason,
                    "payments_applied": str(settlement.payments_applied),
                    "credits_applied": str(settlement.credits_applied),
                    "opening_funding_applied": str(settlement.opening_funding_applied),
                    "ledger_transaction_id": None,
                    "service_access_consequence": "none",
                },
            ),
        )
        if commit:
            db.commit()
            db.refresh(invoice)
        else:
            db.flush()
        return InvoiceLifecycleTransitionResult(invoice=invoice, changed=True)

    @staticmethod
    def mark_overdue_system(
        db: Session,
        invoice_id: str,
        *,
        as_of: datetime,
        reason: str,
        commit: bool = False,
    ) -> InvoiceLifecycleTransitionResult:
        """Own overdue eligibility, state, audit, and one-time observation event."""
        invoice = lock_for_update(db, Invoice, invoice_id)
        if invoice is None:
            raise HTTPException(status_code=404, detail="Invoice not found")
        if not invoice.is_active or invoice.balance_due <= Decimal("0.00"):
            raise HTTPException(
                status_code=409, detail="Invoice is not overdue-eligible"
            )
        due_at = invoice.due_at
        if due_at is None:
            raise HTTPException(status_code=409, detail="Invoice has no due date")
        normalized_due = due_at if due_at.tzinfo else due_at.replace(tzinfo=UTC)
        normalized_as_of = as_of if as_of.tzinfo else as_of.replace(tzinfo=UTC)
        if normalized_due > normalized_as_of:
            raise HTTPException(status_code=409, detail="Invoice is not due yet")
        if invoice.status not in {
            InvoiceStatus.issued,
            InvoiceStatus.partially_paid,
            InvoiceStatus.overdue,
        }:
            raise HTTPException(status_code=409, detail="Invoice cannot become overdue")
        previous_status = invoice.status
        changed = previous_status != InvoiceStatus.overdue
        if changed:
            invoice.status = InvoiceStatus.overdue
        metadata = dict(invoice.metadata_ or {})
        announce = not bool(metadata.get("overdue_event_sent"))
        if announce:
            days_overdue = (normalized_as_of.date() - normalized_due.date()).days
            emit_event(
                db,
                EventType.invoice_overdue,
                {
                    "invoice_id": str(invoice.id),
                    "invoice_number": invoice.invoice_number or "",
                    "amount": str(invoice.balance_due or invoice.total),
                    "due_date": normalized_due.date().isoformat(),
                    "days_overdue": str(days_overdue),
                    "from_status": previous_status.value,
                    "to_status": InvoiceStatus.overdue.value,
                },
                invoice_id=invoice.id,
                account_id=invoice.account_id,
            )
            metadata["overdue_event_sent"] = normalized_as_of.isoformat()
            invoice.metadata_ = metadata
        if changed or announce:
            AuditEvents.stage(
                db,
                AuditEventCreate(
                    action="mark_invoice_overdue_system",
                    entity_type="invoice",
                    entity_id=str(invoice.id),
                    metadata_={
                        "from_status": previous_status.value,
                        "to_status": InvoiceStatus.overdue.value,
                        "reason": reason,
                        "due_at": normalized_due.isoformat(),
                        "as_of": normalized_as_of.isoformat(),
                        "event_emitted": announce,
                        "ledger_transaction_id": None,
                        "service_access_consequence": "observation_only",
                    },
                ),
            )
        if commit:
            db.commit()
            db.refresh(invoice)
        else:
            db.flush()
        return InvoiceLifecycleTransitionResult(
            invoice=invoice,
            changed=changed,
            event_emitted=announce,
        )

    @staticmethod
    def create(db: Session, payload: InvoiceCreate, *, commit: bool = True):
        _validate_account(db, str(payload.account_id))
        data = payload.model_dump()
        fields_set = payload.model_fields_set
        if "currency" not in fields_set:
            default_currency = settings_spec.resolve_value(
                db, SettingDomain.billing, "default_currency"
            )
            if default_currency:
                data["currency"] = default_currency
        if "status" not in fields_set:
            default_status = settings_spec.resolve_value(
                db, SettingDomain.billing, "default_invoice_status"
            )
            if default_status:
                data["status"] = validate_enum(default_status, InvoiceStatus, "status")
        if data.get("status") in {
            InvoiceStatus.partially_paid,
            InvoiceStatus.paid,
            InvoiceStatus.void,
            InvoiceStatus.written_off,
        }:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Settled, void, and written-off invoice states require their "
                    "named financial owner workflows"
                ),
            )
        if not data.get("invoice_number"):
            generated = next_invoice_number(db)
            if generated:
                data["invoice_number"] = generated
        _validate_invoice_totals(data)
        invoice = Invoice(**data)
        db.add(invoice)
        db.flush()
        emit_event(
            db,
            EventType.invoice_created,
            {
                "invoice_id": str(invoice.id),
                "invoice_number": invoice.invoice_number,
                "amount": str(invoice.total) if invoice.total else None,
                "total": str(invoice.total) if invoice.total else None,
                "due_date": (
                    invoice.due_at.date().isoformat() if invoice.due_at else None
                ),
                "currency": invoice.currency,
                "status": invoice.status.value if invoice.status else None,
            },
            account_id=invoice.account_id,
            invoice_id=invoice.id,
        )
        _apply_available_account_credit(db, invoice)
        if commit:
            db.commit()
            db.refresh(invoice)
        else:
            db.flush()

        return invoice

    @staticmethod
    def create_for_subscription(
        db: Session,
        subscriber_id: str,
        subscription_id: str,
        *,
        allow_prepaid: bool = False,
        commit: bool = True,
    ) -> Invoice:
        """Create an invoice with line items auto-populated from a subscription's offer price.

        Looks up the subscription's active offer price and creates an invoice
        with a single line item for the recurring charge.  Tax is applied
        according to the subscriber's tax rate if set.

        Prepaid subscriptions draw down a deposit and are not normally invoiced;
        this raises unless ``allow_prepaid=True`` is passed as a deliberate
        credit/admin override.
        """
        from app.models.catalog import (
            BillingMode,
            CatalogOffer,
            OfferPrice,
            Subscription,
            billing_cycle_noun,
        )
        from app.models.subscriber import Subscriber

        subscription = db.get(Subscription, coerce_uuid(subscription_id))
        if not subscription:
            raise HTTPException(status_code=404, detail="Subscription not found")

        if subscription.billing_mode == BillingMode.prepaid and not allow_prepaid:
            raise HTTPException(
                status_code=400,
                detail=(
                    "This is a prepaid subscription. Use the explicit prepaid "
                    "invoice path or credit override only when the charge is "
                    "intentional."
                ),
            )

        subscriber = db.get(Subscriber, coerce_uuid(subscriber_id))
        if not subscriber:
            raise HTTPException(status_code=404, detail="Subscriber not found")

        offer = db.get(CatalogOffer, subscription.offer_id)
        if not offer:
            raise HTTPException(status_code=404, detail="Catalog offer not found")

        # Find the active recurring price
        offer_price = (
            db.query(OfferPrice)
            .filter(
                OfferPrice.offer_id == offer.id,
                OfferPrice.is_active.is_(True),
                OfferPrice.price_type == "recurring",
            )
            .first()
        )
        if not offer_price:
            raise HTTPException(
                status_code=400,
                detail=f"No active recurring price for offer {offer.name}",
            )

        amount = Decimal(str(offer_price.amount))
        currency = offer_price.currency or "NGN"

        # Resolve tax
        tax_rate_id = getattr(subscriber, "tax_rate_id", None)
        vat_policy = customer_tax_policies.get_customer_vat_exemption_policy(
            db,
            account_id=subscriber.id,
        )
        if vat_policy.vat_exempt:
            tax_rate_id = None
        tax_total = Decimal("0")
        if tax_rate_id:
            from app.models.billing import TaxRate

            tax_rate = db.get(TaxRate, tax_rate_id)
            if tax_rate and tax_rate.rate:
                tax_total = (
                    amount * Decimal(str(tax_rate.rate)) / Decimal("100")
                ).quantize(Decimal("0.01"))

        total = amount + tax_total

        # Create invoice
        invoice_number = numbering.generate_required_number(
            db,
            SettingDomain.billing,
            "invoice_number",
            "invoice_number_prefix",
            "invoice_number_padding",
            "invoice_number_start",
        )
        # An invoice that calls itself issued must carry the dates that make
        # that true. Declaring the status without them produced invoices with no
        # issue date and no due date: they could never age, never go overdue,
        # and dropped out of anything filtering on issue date. Settlement then
        # advanced them to paid, so they were paid without ever being issued.
        # `issue_draft_system` remains the owner of the draft -> issued
        # transition; this path builds the state directly and must therefore
        # match it.
        from app.services.billing_settings import resolve_payment_due_days

        issued_at = datetime.now(UTC)
        due_days = resolve_payment_due_days(db, subscriber=subscriber)
        invoice = Invoice(
            account_id=subscriber_id,
            invoice_number=invoice_number,
            currency=currency,
            subtotal=amount,
            tax_total=tax_total,
            total=total,
            balance_due=total,
            status=InvoiceStatus.issued,
            issued_at=issued_at,
            due_at=issued_at + timedelta(days=due_days),
        )
        db.add(invoice)
        db.flush()

        # Create line item
        line = InvoiceLine(
            invoice_id=invoice.id,
            subscription_id=subscription_id,
            description=(
                f"{offer.name} — "
                f"{billing_cycle_noun(subscription.billing_cycle or offer_price.billing_cycle)}"
                " service"
            ),
            quantity=Decimal("1"),
            unit_price=amount,
            amount=amount,
            tax_rate_id=tax_rate_id,
            tax_application=TaxApplication.exclusive,
            is_active=True,
        )
        db.add(line)
        emit_event(
            db,
            EventType.invoice_created,
            {
                "invoice_id": str(invoice.id),
                "invoice_number": invoice.invoice_number,
                "total": str(invoice.total),
                "currency": invoice.currency,
                "status": invoice.status.value,
            },
            account_id=invoice.account_id,
            invoice_id=invoice.id,
        )
        _apply_available_account_credit(db, invoice)
        if commit:
            db.commit()
            db.refresh(invoice)
        else:
            db.flush()

        logger.info(
            "Created invoice %s for subscription %s: %s %s",
            invoice.invoice_number,
            subscription_id,
            currency,
            total,
        )
        return invoice

    @staticmethod
    def get(db: Session, invoice_id: str):
        invoice = get_by_id(
            db,
            Invoice,
            invoice_id,
            options=[
                selectinload(Invoice.lines),
                selectinload(Invoice.payment_allocations),
            ],
        )
        if not invoice:
            raise HTTPException(status_code=404, detail="Invoice not found")
        return invoice

    @staticmethod
    def list(
        db: Session,
        account_id: str | None,
        status: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
        *,
        updated_since: datetime | None = None,
    ):
        query = db.query(Invoice).options(
            selectinload(Invoice.lines),
            selectinload(Invoice.payment_allocations),
        )
        if account_id:
            query = query.filter(Invoice.account_id == account_id)
        if status:
            query = query.filter(
                Invoice.status == validate_enum(status, InvoiceStatus, "status")
            )
        if is_active is None:
            query = query.filter(Invoice.is_active.is_(True))
        else:
            query = query.filter(Invoice.is_active == is_active)
        # Incremental-sync watermark: only rows modified at/after the cutoff.
        # Backed by ix_invoices_is_active_updated_at so the ERP AR sync stops
        # re-listing every invoice each cycle (pool-starvation incident).
        if updated_since is not None:
            query = query.filter(Invoice.updated_at >= updated_since)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": Invoice.created_at,
                "updated_at": Invoice.updated_at,
                "due_at": Invoice.due_at,
                "issued_at": Invoice.issued_at,
                "status": Invoice.status,
            },
        )
        # Stable, keyset-friendly tiebreaker so forward paging over a watermark
        # is deterministic (e.g. order_by=updated_at&order_dir=asc from the sync).
        query = query.order_by(Invoice.id.asc())
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def list_for_sync(
        db: Session,
        account_id: str | None,
        status: str | None,
        is_active: bool | None,
        limit: int,
        offset: int,
        *,
        updated_since: datetime | None = None,
    ):
        """Return the ordered ERP invoice delta without detail-only relations."""
        query = db.query(Invoice).options(
            selectinload(
                Invoice.lines.and_(InvoiceLine.is_active.is_(True))
            ).selectinload(InvoiceLine.tax_rate)
        )
        if account_id:
            query = query.filter(Invoice.account_id == account_id)
        if status:
            query = query.filter(
                Invoice.status == validate_enum(status, InvoiceStatus, "status")
            )
        if is_active is None:
            query = query.filter(Invoice.is_active.is_(True))
        else:
            query = query.filter(Invoice.is_active == is_active)
        return apply_sync_page(
            query,
            Invoice,
            updated_since=updated_since,
            limit=limit,
            offset=offset,
        ).all()

    @classmethod
    def sync_list_response(cls, db: Session, **kwargs):
        limit = kwargs["limit"]
        offset = kwargs["offset"]
        items = cls.list_for_sync(db, **kwargs)
        return sync_page_response(items, limit=limit, offset=offset)

    @staticmethod
    def update(db: Session, invoice_id: str, payload: InvoiceUpdate):
        invoice = get_by_id(db, Invoice, invoice_id)
        if not invoice:
            raise HTTPException(status_code=404, detail="Invoice not found")
        previous_status = invoice.status
        data = payload.model_dump(exclude_unset=True)
        if invoice.status in {InvoiceStatus.void, InvoiceStatus.written_off}:
            raise HTTPException(
                status_code=409,
                detail="Terminal invoices are immutable financial evidence",
            )
        documentary_fields = set(data) - {"status"}
        if invoice.status != InvoiceStatus.draft and documentary_fields:
            raise HTTPException(
                status_code=409,
                detail="Issued invoices are immutable; use an exact lifecycle action",
            )
        if invoice.discount_type and set(data).intersection(
            {"subtotal", "tax_total", "total", "balance_due"}
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    "Discounted Invoice totals are owner-derived; use Edit Invoice "
                    "to change its Line Items and discount together"
                ),
            )
        if data.get("status") in {
            InvoiceStatus.partially_paid,
            InvoiceStatus.paid,
            InvoiceStatus.void,
            InvoiceStatus.written_off,
        }:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Payment settlement, void, and write-off states require their "
                    "named owner workflows"
                ),
            )
        if "balance_due" in data and round_money(
            to_decimal(data["balance_due"])
        ) != round_money(to_decimal(invoice.balance_due)):
            raise HTTPException(
                status_code=409,
                detail="Invoice balance_due is owner-derived and cannot be edited",
            )
        if "account_id" in data and data["account_id"] != invoice.account_id:
            raise HTTPException(
                status_code=400,
                detail="Cannot change account on an existing invoice",
            )
        if "currency" in data and data["currency"] != invoice.currency:
            raise HTTPException(
                status_code=400, detail="Currency does not match invoice"
            )
        merged = {
            "subtotal": data.get("subtotal", invoice.subtotal),
            "discount_amount": invoice.discount_amount,
            "tax_total": data.get("tax_total", invoice.tax_total),
            "total": data.get("total", invoice.total),
            "balance_due": data.get("balance_due", invoice.balance_due),
        }
        _validate_invoice_totals(merged)
        if "status" in data:
            assert_legal_invoice_transition(previous_status, data["status"])
        for key, value in data.items():
            setattr(invoice, key, value)

        # Emit invoice events based on status transitions
        new_status = invoice.status
        if previous_status != new_status:
            payload_dict = {
                "invoice_id": str(invoice.id),
                "invoice_number": invoice.invoice_number,
                "amount": str(invoice.total) if invoice.total else None,
                "total": str(invoice.total) if invoice.total else None,
                "due_date": (
                    invoice.due_at.date().isoformat() if invoice.due_at else None
                ),
                "currency": invoice.currency,
                "from_status": previous_status.value if previous_status else None,
                "to_status": new_status.value if new_status else None,
            }

            if new_status == InvoiceStatus.issued:
                emit_event(
                    db,
                    EventType.invoice_sent,
                    payload_dict,
                    account_id=invoice.account_id,
                    invoice_id=invoice.id,
                )
                _apply_available_account_credit(db, invoice)
            elif new_status == InvoiceStatus.paid:
                emit_event(
                    db,
                    EventType.invoice_paid,
                    payload_dict,
                    account_id=invoice.account_id,
                    invoice_id=invoice.id,
                )
            elif new_status == InvoiceStatus.overdue:
                emit_event(
                    db,
                    EventType.invoice_overdue,
                    payload_dict,
                    account_id=invoice.account_id,
                    invoice_id=invoice.id,
                )

        db.commit()
        db.refresh(invoice)

        return invoice

    @staticmethod
    def delete(db: Session, invoice_id: str):
        invoice = get_by_id(db, Invoice, invoice_id)
        if not invoice:
            raise HTTPException(status_code=404, detail="Invoice not found")
        if invoice.status != InvoiceStatus.draft:
            raise HTTPException(
                status_code=409,
                detail="Only draft invoices can be deleted; preview void instead",
            )
        invoice.is_active = False
        db.commit()

    @staticmethod
    def write_off(db: Session, invoice_id: str, memo: str | None = None):
        """Compatibility entry point for trusted deterministic system callers."""
        return Invoices.write_off_system(
            db,
            invoice_id,
            reason=memo or "Write-off",
            idempotency_key=f"legacy-invoice-writeoff-{invoice_id}",
        ).invoice

    @staticmethod
    def void(db: Session, invoice_id: str, memo: str | None = None):
        """Compatibility entry point for trusted deterministic system callers."""
        return Invoices.void_system(
            db,
            invoice_id,
            reason=memo or "Void invoice",
            idempotency_key=f"legacy-invoice-void-{invoice_id}",
        ).invoice

    @staticmethod
    def bulk_write_off(db: Session, payload: InvoiceBulkWriteOffRequest) -> int:
        if not payload.invoice_ids:
            raise HTTPException(status_code=400, detail="invoice_ids required")
        ids = [coerce_uuid(invoice_id) for invoice_id in payload.invoice_ids]
        invoices = db.query(Invoice).filter(Invoice.id.in_(ids)).all()
        if len(invoices) != len(ids):
            raise HTTPException(
                status_code=404, detail="One or more invoices not found"
            )
        try:
            updated = 0
            for invoice in invoices:
                try:
                    preview = _build_closure_preview(
                        db, invoice, InvoiceClosureType.write_off
                    )
                except HTTPException as exc:
                    if exc.status_code < 500:
                        continue
                    raise
                Invoices.confirm_write_off(
                    db,
                    str(invoice.id),
                    InvoiceClosureConfirm(
                        preview_fingerprint=preview.fingerprint,
                        idempotency_key=f"bulk-invoice-writeoff-{invoice.id}",
                        memo=payload.memo or "Bulk invoice write-off",
                    ),
                    origin=InvoiceClosureOrigin.system,
                    commit=False,
                )
                updated += 1
            db.commit()
        except SQLAlchemyError:
            db.rollback()
            raise
        return updated

    @staticmethod
    def bulk_write_off_response(
        db: Session, payload: InvoiceBulkWriteOffRequest
    ) -> dict:
        updated = Invoices.bulk_write_off(db, payload)
        return {"updated": updated}

    @staticmethod
    def bulk_void(db: Session, payload: InvoiceBulkVoidRequest) -> int:
        if not payload.invoice_ids:
            raise HTTPException(status_code=400, detail="invoice_ids required")
        ids = [coerce_uuid(invoice_id) for invoice_id in payload.invoice_ids]
        invoices = db.query(Invoice).filter(Invoice.id.in_(ids)).all()
        if len(invoices) != len(ids):
            raise HTTPException(
                status_code=404, detail="One or more invoices not found"
            )
        try:
            updated = 0
            for invoice in invoices:
                try:
                    preview = _build_closure_preview(
                        db, invoice, InvoiceClosureType.void
                    )
                except HTTPException as exc:
                    if exc.status_code < 500:
                        continue
                    raise
                Invoices.confirm_void(
                    db,
                    str(invoice.id),
                    InvoiceClosureConfirm(
                        preview_fingerprint=preview.fingerprint,
                        idempotency_key=f"bulk-invoice-void-{invoice.id}",
                        memo=payload.memo or "Bulk invoice void",
                    ),
                    origin=InvoiceClosureOrigin.system,
                    commit=False,
                )
                updated += 1
            db.commit()
            return updated
        except SQLAlchemyError:
            db.rollback()
            raise

    @staticmethod
    def bulk_void_response(db: Session, payload: InvoiceBulkVoidRequest) -> dict:
        updated = Invoices.bulk_void(db, payload)
        return {"updated": updated}


class InvoiceLines(ListResponseMixin):
    @staticmethod
    def replace_admin_draft_lines(
        db: Session,
        invoice_id: UUID,
        replacements: tuple[DraftInvoiceLineReplacement, ...],
        *,
        allow_discount_reprice: bool = False,
    ) -> None:
        """Stage the complete active line set for an administrative draft."""
        invoice = lock_for_update(db, Invoice, invoice_id)
        if invoice is None or not invoice.is_active:
            raise DraftInvoiceParticipantError(
                "invoice_not_found",
                "Invoice not found",
            )
        if invoice.status != InvoiceStatus.draft:
            raise DraftInvoiceParticipantError(
                "invoice_not_editable",
                "Invoice lines can be edited only while the invoice is a draft",
            )
        if invoice.discount_type and not allow_discount_reprice:
            raise DraftInvoiceParticipantError(
                "active_discount_blocks_line_mutation",
                "Change Invoice lines and its discount together from the Edit Invoice page",
            )
        existing = {
            line.id: line
            for line in db.scalars(
                select(InvoiceLine)
                .where(InvoiceLine.invoice_id == invoice.id)
                .where(InvoiceLine.is_active.is_(True))
                .with_for_update()
            ).all()
        }
        seen: set[UUID] = set()
        for replacement in replacements:
            payload = replacement.payload
            if payload.invoice_id != invoice.id:
                raise DraftInvoiceParticipantError(
                    "line_not_found",
                    "Invoice line belongs to another invoice",
                )
            _resolve_tax_rate(
                db,
                str(payload.tax_rate_id) if payload.tax_rate_id else None,
            )
            amount = _validate_invoice_line_amount(
                payload.quantity,
                payload.unit_price,
                payload.amount,
            )
            if replacement.line_id is None:
                data = payload.model_dump(exclude={"amount"})
                db.add(InvoiceLine(**data, amount=amount))
                continue
            line = existing.get(replacement.line_id)
            if line is None:
                raise DraftInvoiceParticipantError(
                    "line_not_found",
                    "Invoice line not found",
                )
            seen.add(line.id)
            line.description = payload.description
            line.quantity = payload.quantity
            line.unit_price = payload.unit_price
            line.amount = amount
            line.tax_rate_id = payload.tax_rate_id
            line.tax_application = payload.tax_application
        for line_id, line in existing.items():
            if line_id not in seen:
                line.is_active = False
        db.flush()
        _recalculate_invoice_totals(db, invoice)

    @staticmethod
    def stage_system_line(
        db: Session,
        payload: SystemInvoiceLineCreate,
        *,
        reason: str,
    ) -> InvoiceLine:
        """Stage one automation-produced invoice line in its caller transaction."""
        invoice = lock_for_update(db, Invoice, payload.invoice_id)
        if not invoice or not invoice.is_active:
            raise HTTPException(status_code=404, detail="Invoice not found")
        if invoice.status not in {InvoiceStatus.draft, InvoiceStatus.issued}:
            raise HTTPException(
                status_code=409,
                detail="System lines may be added only to draft or issued invoices",
            )
        _resolve_tax_rate(db, str(payload.tax_rate_id) if payload.tax_rate_id else None)
        amount = round_money(
            payload.amount
            if payload.amount is not None
            else payload.quantity * payload.unit_price
        )
        data = payload.model_dump(exclude={"amount"})
        billing_line_key = data.pop("billing_line_key", None)
        if billing_line_key:
            existing = db.scalar(
                select(InvoiceLine)
                .where(InvoiceLine.billing_line_key == billing_line_key)
                .where(InvoiceLine.is_active.is_(True))
            )
            if existing is not None:
                expected = {
                    "invoice_id": payload.invoice_id,
                    "subscription_id": payload.subscription_id,
                    "description": payload.description,
                    "quantity": payload.quantity,
                    "unit_price": payload.unit_price,
                    "amount": amount,
                    "tax_rate_id": payload.tax_rate_id,
                    "tax_application": payload.tax_application,
                    "metadata_": payload.metadata_,
                }
                if any(
                    getattr(existing, field) != value
                    for field, value in expected.items()
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="Billing line key was used for a different invoice line",
                    )
                return existing
        if invoice.discount_type:
            raise HTTPException(
                status_code=409,
                detail="Invoice lines cannot change after a discount is applied",
            )
        line = InvoiceLine(
            **data,
            amount=amount,
            billing_line_key=billing_line_key,
        )
        db.add(line)
        db.flush()
        AuditEvents.stage(
            db,
            AuditEventCreate(
                action="stage_system_invoice_line",
                entity_type="invoice_line",
                entity_id=str(line.id),
                metadata_={
                    "invoice_id": str(invoice.id),
                    "account_id": str(invoice.account_id),
                    "subscription_id": (
                        str(line.subscription_id) if line.subscription_id else None
                    ),
                    "amount": str(line.amount),
                    "currency": invoice.currency,
                    "billing_line_key": line.billing_line_key,
                    "reason": reason,
                    "financial_effect": "invoice_document_staged",
                    "ledger_transaction_id": None,
                },
            ),
        )
        return line

    @staticmethod
    def stage_system_line_for_owner(
        db: Session,
        payload: SystemInvoiceLineCreate,
        *,
        reason: str,
    ) -> InvoiceLine:
        """Stage a system line as a flush-only participant for a command owner."""

        try:
            return InvoiceLines.stage_system_line(db, payload, reason=reason)
        except HTTPException as exc:
            raise InvoiceOwnerError(
                code="financial.invoice.line_stage_rejected",
                message="Invoice owner rejected system line construction.",
                details={"reason": str(exc.detail)},
            ) from exc

    @staticmethod
    def create(db: Session, payload: InvoiceLineCreate):
        invoice = get_by_id(db, Invoice, payload.invoice_id)
        if not invoice:
            raise HTTPException(status_code=404, detail="Invoice not found")
        if invoice.status != InvoiceStatus.draft:
            raise HTTPException(
                status_code=409,
                detail="Invoice lines can be edited only while the invoice is a draft",
            )
        if invoice.discount_type:
            raise HTTPException(
                status_code=409,
                detail="Change Invoice lines and its discount together from Edit Invoice",
            )
        _resolve_tax_rate(db, str(payload.tax_rate_id) if payload.tax_rate_id else None)
        data = payload.model_dump(exclude={"amount"})
        fields_set = payload.model_fields_set
        if "tax_application" not in fields_set:
            default_tax_application = settings_spec.resolve_value(
                db, SettingDomain.billing, "default_tax_application"
            )
            if default_tax_application:
                data["tax_application"] = validate_enum(
                    default_tax_application, TaxApplication, "tax_application"
                )
        amount = _validate_invoice_line_amount(
            payload.quantity, payload.unit_price, payload.amount
        )
        line = InvoiceLine(**data, amount=amount)
        try:
            db.add(line)
            db.flush()
            _recalculate_invoice_totals(db, invoice)
            invoice.updated_at = datetime.now(UTC)
            db.commit()
        except SQLAlchemyError:
            db.rollback()
            raise
        db.refresh(line)
        return line

    @staticmethod
    def get(db: Session, line_id: str):
        line = get_by_id(db, InvoiceLine, line_id)
        if not line:
            raise HTTPException(status_code=404, detail="Invoice line not found")
        return line

    @staticmethod
    def list(
        db: Session,
        invoice_id: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(InvoiceLine)
        if invoice_id:
            query = query.filter(InvoiceLine.invoice_id == invoice_id)
        if is_active is None:
            query = query.filter(InvoiceLine.is_active.is_(True))
        else:
            query = query.filter(InvoiceLine.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": InvoiceLine.created_at, "amount": InvoiceLine.amount},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, line_id: str, payload: InvoiceLineUpdate):
        line = get_by_id(db, InvoiceLine, line_id)
        if not line:
            raise HTTPException(status_code=404, detail="Invoice line not found")
        invoice = get_by_id(db, Invoice, line.invoice_id)
        if invoice is None or invoice.status != InvoiceStatus.draft:
            raise HTTPException(
                status_code=409,
                detail="Invoice lines can be edited only while the invoice is a draft",
            )
        if invoice.discount_type:
            raise HTTPException(
                status_code=409,
                detail="Change Invoice lines and its discount together from Edit Invoice",
            )
        data = payload.model_dump(exclude_unset=True)
        if "tax_rate_id" in data:
            _resolve_tax_rate(
                db, str(data["tax_rate_id"]) if data["tax_rate_id"] else None
            )
        quantity = data.get("quantity", line.quantity)
        unit_price = data.get("unit_price", line.unit_price)
        amount = data.get("amount")
        data["amount"] = _validate_invoice_line_amount(quantity, unit_price, amount)
        for key, value in data.items():
            setattr(line, key, value)
        try:
            if invoice:
                db.flush()
                _recalculate_invoice_totals(db, invoice)
                invoice.updated_at = datetime.now(UTC)
            db.commit()
        except SQLAlchemyError:
            db.rollback()
            raise
        db.refresh(line)
        return line

    @staticmethod
    def delete(db: Session, line_id: str):
        line = get_by_id(db, InvoiceLine, line_id)
        if not line:
            raise HTTPException(status_code=404, detail="Invoice line not found")
        invoice = get_by_id(db, Invoice, line.invoice_id)
        if invoice is None or invoice.status != InvoiceStatus.draft:
            raise HTTPException(
                status_code=409,
                detail="Invoice lines can be edited only while the invoice is a draft",
            )
        if invoice.discount_type:
            raise HTTPException(
                status_code=409,
                detail="Change Invoice lines and its discount together from Edit Invoice",
            )
        line.is_active = False
        if invoice:
            db.flush()
            _recalculate_invoice_totals(db, invoice)
            invoice.updated_at = datetime.now(UTC)
        db.commit()
