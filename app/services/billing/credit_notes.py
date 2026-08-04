"""Credit note management services."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from app.models.billing import (
    CreditNote,
    CreditNoteApplication,
    CreditNoteLine,
    CreditNoteStatus,
    Invoice,
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
    CreditNoteApplicationPreviewRequest,
    CreditNoteApplyRequest,
    CreditNoteCreate,
    CreditNoteIssueConfirmation,
    CreditNoteIssuePreviewRequest,
    CreditNoteIssueRequest,
    CreditNoteLineCreate,
    CreditNoteLineUpdate,
    CreditNoteUpdate,
    CreditNoteVoidRequest,
    LedgerEntryCreate,
)
from app.services import numbering, settings_spec
from app.services.audit import AuditEvents
from app.services.billing._common import (
    _recalculate_credit_note_totals,
    _recalculate_invoice_totals,
    _resolve_tax_rate,
    _validate_account,
    _validate_credit_note_totals,
    _validate_invoice_line_amount,
    lock_account,
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
from app.services.customer_financial_ledger import calculate_customer_balance
from app.services.locking import lock_for_update
from app.services.response import ListResponseMixin
from app.services.sync_feeds import apply_sync_page, sync_page_response

logger = logging.getLogger(__name__)

_APPLICATION_IDEMPOTENCY_SCOPE = "credit_note_application"
_ISSUE_IDEMPOTENCY_SCOPE = "credit_note_issue"
_VOID_IDEMPOTENCY_SCOPE = "credit_note_void"
_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._~-]{16,120}$")
_CREDIT_APPLICABLE_INVOICE_STATUSES = frozenset(
    {
        # Preserve the existing ability to apply issued credit to a draft
        # receivable; the owner, rather than the template, decides eligibility.
        InvoiceStatus.draft,
        InvoiceStatus.issued,
        InvoiceStatus.partially_paid,
        InvoiceStatus.overdue,
    }
)


@dataclass(frozen=True)
class CreditApplicationOption:
    credit_note_id: UUID
    credit_number: str | None
    currency: str
    available_amount: Decimal
    max_applicable_amount: Decimal


@dataclass(frozen=True)
class CreditApplicationPreview:
    credit_note_id: UUID
    credit_number: str | None
    invoice_id: UUID
    invoice_number: str | None
    account_id: UUID
    currency: str
    credit_available_before: Decimal
    invoice_receivable_before: Decimal
    apply_amount: Decimal
    credit_available_after: Decimal
    invoice_receivable_after: Decimal
    settles_invoice: bool
    ledger_entry_type: LedgerEntryType
    ledger_source: LedgerSource
    access_consequence: str
    fingerprint: str


@dataclass(frozen=True)
class CreditApplicationResult:
    application: CreditNoteApplication
    ledger_entry: LedgerEntry
    consumption_ledger_entry: LedgerEntry | None
    preview: CreditApplicationPreview | None
    idempotent_replay: bool = False

    def audit_metadata(self) -> dict[str, object]:
        preview = self.preview
        metadata: dict[str, object] = {
            "application_id": str(self.application.id),
            "credit_note_id": str(self.application.credit_note_id),
            "invoice_id": str(self.application.invoice_id),
            "ledger_entry_id": str(self.ledger_entry.id),
            "consumption_ledger_entry_id": (
                str(self.consumption_ledger_entry.id)
                if self.consumption_ledger_entry
                else None
            ),
            "amount": str(self.application.amount),
            "currency": self.ledger_entry.currency,
            "preview_fingerprint": self.application.preview_fingerprint,
        }
        if preview is not None:
            metadata.update(
                {
                    "credit_available_before": str(preview.credit_available_before),
                    "credit_available_after": str(preview.credit_available_after),
                    "invoice_receivable_before": str(preview.invoice_receivable_before),
                    "invoice_receivable_after": str(preview.invoice_receivable_after),
                    "access_consequence": preview.access_consequence,
                }
            )
        return metadata


@dataclass(frozen=True)
class CreditIssuePreview:
    credit_note_id: UUID | None
    account_id: UUID
    invoice_id: UUID | None
    credit_number: str | None
    currency: str
    credit_total: Decimal
    prepaid_funding_before: Decimal
    prepaid_funding_after: Decimal
    invoice_receivable_before: Decimal | None
    invoice_receivable_after: Decimal | None
    ledger_entry_type: LedgerEntryType
    ledger_source: LedgerSource
    ledger_amount: Decimal
    access_consequence: str
    fingerprint: str


@dataclass(frozen=True)
class CreditIssueResult:
    credit_note: CreditNote
    funding_ledger_entry: LedgerEntry
    preview: CreditIssuePreview | None
    idempotent_replay: bool = False

    def audit_metadata(self) -> dict[str, object]:
        return {
            "credit_note_id": str(self.credit_note.id),
            "funding_ledger_entry_id": str(self.funding_ledger_entry.id),
            "amount": str(self.credit_note.total),
            "currency": self.credit_note.currency,
            "preview_fingerprint": self.credit_note.issue_preview_fingerprint,
            "access_consequence": (
                self.preview.access_consequence if self.preview else "none"
            ),
        }


class CreditNoteReferralRewardError(ValueError):
    """Transport-neutral rejection for the referral reward collaborator."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class CreditVoidPreview:
    credit_note_id: UUID
    account_id: UUID
    credit_number: str | None
    currency: str
    credit_available_before: Decimal
    credit_available_after: Decimal
    prepaid_funding_before: Decimal
    prepaid_funding_after: Decimal
    reverses_ledger_entry_id: UUID
    ledger_entry_type: LedgerEntryType
    ledger_source: LedgerSource
    ledger_amount: Decimal
    access_consequence: str
    fingerprint: str


@dataclass(frozen=True)
class CreditVoidResult:
    credit_note: CreditNote
    void_ledger_entry: LedgerEntry
    preview: CreditVoidPreview | None
    idempotent_replay: bool = False

    def audit_metadata(self) -> dict[str, object]:
        return {
            "credit_note_id": str(self.credit_note.id),
            "funding_ledger_entry_id": str(self.credit_note.funding_ledger_entry_id),
            "void_ledger_entry_id": str(self.void_ledger_entry.id),
            "amount": str(self.credit_note.total),
            "currency": self.credit_note.currency,
            "preview_fingerprint": self.credit_note.void_preview_fingerprint,
            "access_consequence": (
                self.preview.access_consequence if self.preview else "none"
            ),
        }


@dataclass(frozen=True)
class CreditFundingReconciliation:
    credit_note_id: UUID
    status: str
    remaining_amount: Decimal
    funding_ledger_entry_id: UUID | None
    applied: bool


def _preview_fingerprint(
    *,
    credit_note_id: object,
    invoice_id: object,
    account_id: object,
    currency: str,
    credit_available: Decimal,
    invoice_receivable: Decimal,
    apply_amount: Decimal,
) -> str:
    payload = "|".join(
        (
            str(credit_note_id),
            str(invoice_id),
            str(account_id),
            currency,
            f"{credit_available:.2f}",
            f"{invoice_receivable:.2f}",
            f"{apply_amount:.2f}",
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _stable_fingerprint(kind: str, **values: object) -> str:
    normalized = {
        key: f"{value:.2f}" if isinstance(value, Decimal) else str(value)
        for key, value in values.items()
    }
    payload = json.dumps(
        {"kind": kind, **normalized}, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _stage_credit_audit(
    db: Session,
    *,
    action: str,
    credit_note_id: UUID,
    metadata: dict[str, object],
) -> None:
    AuditEvents.stage(
        db,
        AuditEventCreate(
            action=action,
            entity_type="credit_note",
            entity_id=str(credit_note_id),
            metadata_=metadata,
        ),
    )


def _build_application_preview(
    credit_note: CreditNote,
    invoice: Invoice,
    requested_amount: Decimal | None,
) -> CreditApplicationPreview:
    if credit_note.status in {CreditNoteStatus.draft, CreditNoteStatus.void}:
        raise HTTPException(status_code=400, detail="Credit note is not applicable")
    if invoice.is_proforma:
        raise HTTPException(
            status_code=400,
            detail="Credit cannot be applied to a proforma document",
        )
    if invoice.status not in _CREDIT_APPLICABLE_INVOICE_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot apply a credit to a {invoice.status.value} invoice",
        )
    if invoice.account_id != credit_note.account_id:
        raise HTTPException(
            status_code=400, detail="Credit note does not belong to invoice account"
        )
    if credit_note.invoice_id and credit_note.invoice_id != invoice.id:
        raise HTTPException(
            status_code=400, detail="Credit note is tied to another invoice"
        )
    if invoice.currency != credit_note.currency:
        raise HTTPException(status_code=400, detail="Currency does not match invoice")

    available = round_money(credit_note.total - credit_note.applied_total)
    receivable = round_money(invoice.balance_due)
    if available <= 0:
        raise HTTPException(
            status_code=400, detail="Credit note has no available balance"
        )
    if receivable <= 0:
        raise HTTPException(status_code=400, detail="Invoice has no balance due")
    amount = round_money(
        to_decimal(requested_amount)
        if requested_amount is not None
        else min(available, receivable)
    )
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be greater than 0")
    if amount > available:
        raise HTTPException(
            status_code=400, detail="Amount exceeds credit note balance"
        )
    if amount > receivable:
        raise HTTPException(status_code=400, detail="Amount exceeds invoice balance")

    credit_after = round_money(available - amount)
    receivable_after = round_money(receivable - amount)
    settles = receivable_after <= 0
    return CreditApplicationPreview(
        credit_note_id=credit_note.id,
        credit_number=credit_note.credit_number,
        invoice_id=invoice.id,
        invoice_number=invoice.invoice_number,
        account_id=invoice.account_id,
        currency=invoice.currency,
        credit_available_before=available,
        invoice_receivable_before=receivable,
        apply_amount=amount,
        credit_available_after=credit_after,
        invoice_receivable_after=max(Decimal("0.00"), receivable_after),
        settles_invoice=settles,
        ledger_entry_type=LedgerEntryType.credit,
        ledger_source=LedgerSource.credit_note,
        access_consequence=(
            "recheck_after_receivable_settlement" if settles else "none"
        ),
        fingerprint=_preview_fingerprint(
            credit_note_id=credit_note.id,
            invoice_id=invoice.id,
            account_id=invoice.account_id,
            currency=invoice.currency,
            credit_available=available,
            invoice_receivable=receivable,
            apply_amount=amount,
        ),
    )


def _normalize_idempotency_key(value: str) -> str:
    key = value.strip()
    if not _IDEMPOTENCY_KEY_RE.fullmatch(key):
        raise HTTPException(status_code=400, detail="Invalid idempotency key")
    return key


def _issue_preview_request(
    payload: CreditNoteIssueRequest,
) -> CreditNoteIssuePreviewRequest:
    return CreditNoteIssuePreviewRequest.model_validate(
        payload.model_dump(exclude={"preview_fingerprint", "idempotency_key"})
    )


def _resolve_issue_invoice(
    db: Session,
    *,
    account_id: UUID,
    invoice_id: UUID | None,
    currency: str,
) -> Invoice | None:
    _validate_account(db, str(account_id))
    if invoice_id is None:
        return None
    invoice = get_by_id(db, Invoice, invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if invoice.account_id != account_id:
        raise HTTPException(
            status_code=400, detail="Invoice does not belong to account"
        )
    if invoice.currency != currency:
        raise HTTPException(status_code=400, detail="Currency does not match invoice")
    return invoice


def _build_issue_preview(
    db: Session,
    payload: CreditNoteIssuePreviewRequest,
    *,
    credit_note_id: UUID | None = None,
) -> CreditIssuePreview:
    total = round_money(payload.total)
    data = payload.model_dump()
    data["total"] = total
    _validate_credit_note_totals(data)
    if total <= 0:
        raise HTTPException(
            status_code=400, detail="Credit total must be greater than 0"
        )
    invoice = _resolve_issue_invoice(
        db,
        account_id=payload.account_id,
        invoice_id=payload.invoice_id,
        currency=payload.currency,
    )
    wallet_before = calculate_customer_balance(
        db, payload.account_id, currency=payload.currency
    )
    receivable = round_money(invoice.balance_due) if invoice else None
    fingerprint = _stable_fingerprint(
        "credit_note_issue",
        credit_note_id=credit_note_id,
        account_id=payload.account_id,
        invoice_id=payload.invoice_id,
        credit_number=payload.credit_number,
        currency=payload.currency,
        subtotal=round_money(payload.subtotal),
        tax_total=round_money(payload.tax_total),
        total=total,
        memo=payload.memo,
        line_description=payload.line_description,
        line_tax_rate_id=payload.line_tax_rate_id,
        line_tax_application=payload.line_tax_application,
        wallet_before=wallet_before,
        invoice_receivable=receivable,
    )
    return CreditIssuePreview(
        credit_note_id=credit_note_id,
        account_id=payload.account_id,
        invoice_id=payload.invoice_id,
        credit_number=payload.credit_number,
        currency=payload.currency,
        credit_total=total,
        prepaid_funding_before=wallet_before,
        prepaid_funding_after=round_money(wallet_before + total),
        invoice_receivable_before=receivable,
        invoice_receivable_after=receivable,
        ledger_entry_type=LedgerEntryType.credit,
        ledger_source=LedgerSource.credit_note,
        ledger_amount=total,
        access_consequence="none_credit_note_only",
        fingerprint=fingerprint,
    )


def _draft_issue_request(credit_note: CreditNote) -> CreditNoteIssuePreviewRequest:
    return CreditNoteIssuePreviewRequest(
        account_id=credit_note.account_id,
        invoice_id=credit_note.invoice_id,
        credit_number=credit_note.credit_number,
        currency=credit_note.currency,
        subtotal=credit_note.subtotal,
        tax_total=credit_note.tax_total,
        total=credit_note.total,
        memo=credit_note.memo,
    )


def _validate_funding_entry(
    credit_note: CreditNote,
    entry: LedgerEntry,
    *,
    expected_amount: Decimal | None = None,
) -> None:
    expected = round_money(
        expected_amount if expected_amount is not None else credit_note.total
    )
    if (
        entry.account_id != credit_note.account_id
        or entry.invoice_id is not None
        or entry.entry_type != LedgerEntryType.credit
        or entry.source != LedgerSource.credit_note
        or entry.currency != credit_note.currency
        or round_money(entry.amount) != expected
    ):
        raise HTTPException(
            status_code=409,
            detail="Ledger entry does not exactly fund the required credit-note amount",
        )


def _funded_credit_available(db: Session, credit_note: CreditNote) -> Decimal:
    """Return exact unconsumed funding linked to one credit note.

    Credit-note funding is source-specific evidence.  Unrelated historical
    account-credit drift must not prevent this owner from consuming or
    reversing an otherwise exact, untouched funding chain.
    """

    if not credit_note.funding_ledger_entry_id:
        raise HTTPException(
            status_code=409,
            detail="Credit note funding evidence was not found",
        )
    funding = db.get(LedgerEntry, credit_note.funding_ledger_entry_id)
    if funding is None:
        raise HTTPException(
            status_code=409,
            detail="Credit note funding evidence was not found",
        )
    if not funding.is_active or funding.reversal_of_entry_id is not None:
        raise HTTPException(
            status_code=409,
            detail="Credit note funding evidence is not active",
        )
    if (
        funding.account_id != credit_note.account_id
        or funding.invoice_id is not None
        or funding.entry_type != LedgerEntryType.credit
        or funding.source != LedgerSource.credit_note
        or funding.currency != credit_note.currency
    ):
        raise HTTPException(
            status_code=409,
            detail="Credit note funding evidence does not match the note",
        )

    consumed = Decimal("0.00")
    applications = (
        db.query(CreditNoteApplication)
        .filter(CreditNoteApplication.credit_note_id == credit_note.id)
        .order_by(CreditNoteApplication.created_at, CreditNoteApplication.id)
        .all()
    )
    for application in applications:
        if application.consumption_ledger_entry_id is None:
            continue
        entry = db.get(LedgerEntry, application.consumption_ledger_entry_id)
        amount = round_money(application.amount)
        if (
            entry is None
            or not entry.is_active
            or entry.account_id != credit_note.account_id
            or entry.invoice_id is not None
            or entry.entry_type != LedgerEntryType.debit
            or entry.source != LedgerSource.credit_note
            or entry.currency != credit_note.currency
            or round_money(entry.amount) != amount
        ):
            raise HTTPException(
                status_code=409,
                detail="Credit note consumption evidence is incomplete",
            )
        consumed = round_money(consumed + amount)

    available = round_money(funding.amount - consumed)
    document_remaining = round_money(credit_note.total - credit_note.applied_total)
    if available < Decimal("0.00") or available != document_remaining:
        raise HTTPException(
            status_code=409,
            detail="Credit note funding and document balances disagree",
        )
    return available


def _build_void_preview(db: Session, credit_note: CreditNote) -> CreditVoidPreview:
    if credit_note.status == CreditNoteStatus.void:
        raise HTTPException(status_code=400, detail="Credit note already void")
    if credit_note.status != CreditNoteStatus.issued or credit_note.applied_total > 0:
        raise HTTPException(
            status_code=400, detail="Only an unapplied issued credit note can be voided"
        )
    if not credit_note.funding_ledger_entry_id:
        raise HTTPException(
            status_code=409,
            detail="Credit note funding evidence must be reconciled before voiding",
        )
    if credit_note.void_ledger_entry_id:
        raise HTTPException(
            status_code=409, detail="Credit note already has void evidence"
        )
    funding = db.get(LedgerEntry, credit_note.funding_ledger_entry_id)
    if not funding:
        raise HTTPException(
            status_code=409, detail="Credit note funding evidence was not found"
        )
    _validate_funding_entry(credit_note, funding)
    if funding.reversal_of_entry_id is not None:
        raise HTTPException(
            status_code=409, detail="Credit note funding entry is a reversal"
        )
    operational_before = _funded_credit_available(db, credit_note)
    if operational_before != round_money(credit_note.total):
        raise HTTPException(
            status_code=409,
            detail="Exact credit-note funding is below the amount required to void this note",
        )
    wallet_before = calculate_customer_balance(
        db, credit_note.account_id, currency=credit_note.currency
    )
    total = round_money(credit_note.total)
    return CreditVoidPreview(
        credit_note_id=credit_note.id,
        account_id=credit_note.account_id,
        credit_number=credit_note.credit_number,
        currency=credit_note.currency,
        credit_available_before=total,
        credit_available_after=Decimal("0.00"),
        prepaid_funding_before=wallet_before,
        prepaid_funding_after=round_money(wallet_before - total),
        reverses_ledger_entry_id=funding.id,
        ledger_entry_type=LedgerEntryType.debit,
        ledger_source=LedgerSource.credit_note,
        ledger_amount=total,
        access_consequence="none_credit_note_only",
        fingerprint=_stable_fingerprint(
            "credit_note_void",
            credit_note_id=credit_note.id,
            funding_ledger_entry_id=funding.id,
            account_id=credit_note.account_id,
            currency=credit_note.currency,
            total=total,
            wallet_before=wallet_before,
            operational_credit_before=operational_before,
        ),
    )


class CreditNotes(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: CreditNoteCreate):
        """Create an editable draft; issuance is a separate confirmed action."""
        if payload.status != CreditNoteStatus.draft:
            raise HTTPException(
                status_code=409,
                detail="Create a draft or use the credit-note issue workflow",
            )
        _validate_account(db, str(payload.account_id))
        data = payload.model_dump()
        fields_set = payload.model_fields_set
        invoice = None
        if payload.invoice_id:
            invoice = get_by_id(db, Invoice, payload.invoice_id)
            if not invoice:
                raise HTTPException(status_code=404, detail="Invoice not found")
            if invoice.account_id != payload.account_id:
                raise HTTPException(
                    status_code=400, detail="Invoice does not belong to account"
                )
            if "currency" not in fields_set:
                data["currency"] = invoice.currency
            elif data["currency"] != invoice.currency:
                raise HTTPException(
                    status_code=400, detail="Currency does not match invoice"
                )
        if "currency" not in fields_set:
            default_currency = settings_spec.resolve_value(
                db, SettingDomain.billing, "default_currency"
            )
            if default_currency:
                data["currency"] = default_currency
        if not data.get("credit_number"):
            generated = numbering.generate_required_number(
                db,
                SettingDomain.billing,
                "credit_note_number",
                "credit_note_number_prefix",
                "credit_note_number_padding",
                "credit_note_number_start",
            )
            if generated:
                data["credit_number"] = generated
        _validate_credit_note_totals(data)
        data["status"] = CreditNoteStatus.draft
        credit_note = CreditNote(**data)
        db.add(credit_note)
        db.flush()
        db.commit()
        db.refresh(credit_note)
        return credit_note

    @staticmethod
    def preview_issue(
        db: Session, payload: CreditNoteIssuePreviewRequest
    ) -> CreditIssuePreview:
        return _build_issue_preview(db, payload)

    @staticmethod
    def preview_draft_issue(db: Session, credit_note_id: str) -> CreditIssuePreview:
        credit_note = get_by_id(db, CreditNote, credit_note_id)
        if not credit_note:
            raise HTTPException(status_code=404, detail="Credit note not found")
        if credit_note.status != CreditNoteStatus.draft:
            raise HTTPException(status_code=409, detail="Credit note is not a draft")
        return _build_issue_preview(
            db, _draft_issue_request(credit_note), credit_note_id=credit_note.id
        )

    @staticmethod
    def _issue_replay(
        db: Session, *, key: str, preview_fingerprint: str
    ) -> CreditIssueResult | None:
        reservation = db.scalar(
            select(IdempotencyKey).where(
                IdempotencyKey.scope == _ISSUE_IDEMPOTENCY_SCOPE,
                IdempotencyKey.key == key,
            )
        )
        if reservation is None:
            return None
        if not reservation.ref_id:
            raise HTTPException(
                status_code=409, detail="Credit note issue is being processed"
            )
        credit_note = get_by_id(db, CreditNote, reservation.ref_id)
        if not credit_note or not credit_note.funding_ledger_entry_id:
            raise HTTPException(
                status_code=409, detail="Credit note issue evidence is incomplete"
            )
        if credit_note.issue_preview_fingerprint != preview_fingerprint:
            raise HTTPException(
                status_code=409,
                detail="Idempotency key was used with a different issue confirmation",
            )
        funding = db.get(LedgerEntry, credit_note.funding_ledger_entry_id)
        if not funding:
            raise HTTPException(
                status_code=409, detail="Credit note funding evidence was not found"
            )
        return CreditIssueResult(
            credit_note=credit_note,
            funding_ledger_entry=funding,
            preview=None,
            idempotent_replay=True,
        )

    @staticmethod
    def _reserve_issue(db: Session, *, key: str, account_id: UUID) -> IdempotencyKey:
        reservation = IdempotencyKey(
            scope=_ISSUE_IDEMPOTENCY_SCOPE,
            key=key,
            account_id=account_id,
        )
        db.add(reservation)
        try:
            db.flush()
        except IntegrityError as exc:
            raise HTTPException(
                status_code=409, detail="Credit note issue is already being processed"
            ) from exc
        return reservation

    @staticmethod
    def issue_with_evidence(
        db: Session,
        payload: CreditNoteIssueRequest,
        *,
        commit: bool = True,
        stage_audit: bool = True,
    ) -> CreditIssueResult:
        key = _normalize_idempotency_key(payload.idempotency_key)
        replay = CreditNotes._issue_replay(
            db, key=key, preview_fingerprint=payload.preview_fingerprint
        )
        if replay:
            return replay

        lock_account(db, str(payload.account_id))
        preview_request = _issue_preview_request(payload)
        preview = _build_issue_preview(db, preview_request)
        if preview.fingerprint != payload.preview_fingerprint:
            raise HTTPException(
                status_code=409,
                detail="Financial state changed after preview; preview again",
            )
        replay = CreditNotes._issue_replay(
            db, key=key, preview_fingerprint=payload.preview_fingerprint
        )
        if replay:
            return replay
        reservation = CreditNotes._reserve_issue(
            db, key=key, account_id=payload.account_id
        )

        credit_number = payload.credit_number or numbering.generate_required_number(
            db,
            SettingDomain.billing,
            "credit_note_number",
            "credit_note_number_prefix",
            "credit_note_number_padding",
            "credit_note_number_start",
        )
        credit_note = CreditNote(
            account_id=payload.account_id,
            invoice_id=payload.invoice_id,
            credit_number=credit_number,
            status=CreditNoteStatus.issued,
            currency=payload.currency,
            subtotal=round_money(payload.subtotal),
            tax_total=round_money(payload.tax_total),
            total=preview.credit_total,
            applied_total=Decimal("0.00"),
            memo=payload.memo,
            issue_preview_fingerprint=preview.fingerprint,
            issued_at=datetime.now(UTC),
        )
        try:
            db.add(credit_note)
            db.flush()
            if payload.line_description:
                line_amount = (
                    preview.credit_total
                    if payload.line_tax_application == TaxApplication.inclusive
                    else round_money(payload.subtotal)
                )
                db.add(
                    CreditNoteLine(
                        credit_note_id=credit_note.id,
                        description=payload.line_description,
                        quantity=Decimal("1.000"),
                        unit_price=line_amount,
                        amount=line_amount,
                        tax_rate_id=payload.line_tax_rate_id,
                        tax_application=payload.line_tax_application,
                    )
                )
            funding = LedgerEntries.create(
                db,
                LedgerEntryCreate(
                    account_id=credit_note.account_id,
                    entry_type=preview.ledger_entry_type,
                    source=preview.ledger_source,
                    amount=preview.ledger_amount,
                    currency=preview.currency,
                    memo=f"Funding for credit note {credit_note.credit_number or credit_note.id}",
                ),
                commit=False,
            )
            credit_note.funding_ledger_entry_id = funding.id
            reservation.ref_id = str(credit_note.id)
            db.flush()
            if stage_audit:
                _stage_credit_audit(
                    db,
                    action="issue",
                    credit_note_id=credit_note.id,
                    metadata={
                        "funding_ledger_entry_id": str(funding.id),
                        "amount": str(preview.credit_total),
                        "currency": preview.currency,
                        "preview_fingerprint": preview.fingerprint,
                        "access_consequence": preview.access_consequence,
                    },
                )
            if commit:
                db.commit()
                db.refresh(credit_note)
                db.refresh(funding)
        except Exception:
            if commit:
                db.rollback()
            raise
        return CreditIssueResult(
            credit_note=credit_note,
            funding_ledger_entry=funding,
            preview=preview,
        )

    @staticmethod
    def issue_system(
        db: Session,
        payload: CreditNoteIssuePreviewRequest,
        *,
        idempotency_key: str,
        commit: bool = False,
    ) -> CreditIssueResult:
        """Issue through the same preview/confirmation owner for automated callers."""
        preview = CreditNotes.preview_issue(db, payload)
        return CreditNotes.issue_with_evidence(
            db,
            CreditNoteIssueRequest(
                **payload.model_dump(),
                preview_fingerprint=preview.fingerprint,
                idempotency_key=idempotency_key,
            ),
            commit=commit,
        )

    @staticmethod
    def issue_referral_reward(
        db: Session,
        *,
        referral_id: UUID,
        account_id: UUID,
        amount: Decimal,
        currency: str,
    ) -> CreditIssueResult:
        """Issue or recover one referral reward without completing the transaction.

        The memo marker and UUID5 idempotency key preserve the legacy CRM payout
        namespace, while all new issuance still uses this owner's preview,
        account lock, funding-ledger evidence, and audit path.
        """

        external_ref = f"referral:{referral_id}"
        marker = f"[ref:{external_ref}]"
        normalized_currency = str(currency or "").strip().upper()
        try:
            lock_account(db, str(account_id))
            existing = db.scalars(
                select(CreditNote)
                .where(CreditNote.account_id == account_id)
                .where(CreditNote.is_active.is_(True))
                .where(CreditNote.status == CreditNoteStatus.issued)
                .where(CreditNote.memo.ilike(f"%{marker}%"))
                .order_by(CreditNote.created_at.desc())
                .limit(1)
            ).one_or_none()
            if existing is not None:
                if (
                    round_money(existing.total) != round_money(amount)
                    or existing.currency.upper() != normalized_currency
                ):
                    raise CreditNoteReferralRewardError(
                        "financial_conflict",
                        "The referral reward reference belongs to different credit evidence.",
                    )
                if existing.funding_ledger_entry_id is None:
                    raise CreditNoteReferralRewardError(
                        "incomplete_reward_evidence",
                        "The existing referral reward has incomplete funding evidence.",
                    )
                funding = db.get(LedgerEntry, existing.funding_ledger_entry_id)
                if funding is None:
                    raise CreditNoteReferralRewardError(
                        "incomplete_reward_evidence",
                        "The existing referral reward funding entry was not found.",
                    )
                return CreditIssueResult(
                    credit_note=existing,
                    funding_ledger_entry=funding,
                    preview=None,
                    idempotent_replay=True,
                )

            operation_id = uuid5(NAMESPACE_URL, f"crm-account-credit:{external_ref}")
            return CreditNotes.issue_system(
                db,
                CreditNoteIssuePreviewRequest(
                    account_id=account_id,
                    currency=normalized_currency,
                    subtotal=amount,
                    total=amount,
                    memo=f"Referral reward (referral {referral_id}) {marker}",
                    line_description=f"Referral reward (referral {referral_id})",
                ),
                idempotency_key=f"crm-credit-{operation_id}",
                commit=False,
            )
        except CreditNoteReferralRewardError:
            raise
        except HTTPException as exc:
            raise CreditNoteReferralRewardError(
                "financial_conflict",
                "The referral reward conflicts with canonical credit-note state.",
            ) from exc

    @staticmethod
    def issue_draft_with_evidence(
        db: Session,
        credit_note_id: str,
        payload: CreditNoteIssueConfirmation,
        *,
        commit: bool = True,
        stage_audit: bool = True,
    ) -> CreditIssueResult:
        key = _normalize_idempotency_key(payload.idempotency_key)
        replay = CreditNotes._issue_replay(
            db, key=key, preview_fingerprint=payload.preview_fingerprint
        )
        if replay:
            if str(replay.credit_note.id) != str(credit_note_id):
                raise HTTPException(
                    status_code=409,
                    detail="Idempotency key belongs to another credit note",
                )
            return replay

        draft = get_by_id(db, CreditNote, credit_note_id)
        if not draft:
            raise HTTPException(status_code=404, detail="Credit note not found")
        lock_account(db, str(draft.account_id))
        draft = lock_for_update(db, CreditNote, coerce_uuid(credit_note_id))
        if not draft:
            raise HTTPException(status_code=404, detail="Credit note not found")
        if draft.status != CreditNoteStatus.draft:
            raise HTTPException(status_code=409, detail="Credit note is not a draft")
        preview = _build_issue_preview(
            db, _draft_issue_request(draft), credit_note_id=draft.id
        )
        if preview.fingerprint != payload.preview_fingerprint:
            raise HTTPException(
                status_code=409,
                detail="Financial state changed after preview; preview again",
            )
        reservation = CreditNotes._reserve_issue(
            db, key=key, account_id=draft.account_id
        )
        try:
            funding = LedgerEntries.create(
                db,
                LedgerEntryCreate(
                    account_id=draft.account_id,
                    entry_type=LedgerEntryType.credit,
                    source=LedgerSource.credit_note,
                    amount=preview.ledger_amount,
                    currency=draft.currency,
                    memo=f"Funding for credit note {draft.credit_number or draft.id}",
                ),
                commit=False,
            )
            draft.status = CreditNoteStatus.issued
            draft.issued_at = datetime.now(UTC)
            draft.funding_ledger_entry_id = funding.id
            draft.issue_preview_fingerprint = preview.fingerprint
            reservation.ref_id = str(draft.id)
            db.flush()
            if stage_audit:
                _stage_credit_audit(
                    db,
                    action="issue",
                    credit_note_id=draft.id,
                    metadata={
                        "funding_ledger_entry_id": str(funding.id),
                        "amount": str(preview.credit_total),
                        "currency": preview.currency,
                        "preview_fingerprint": preview.fingerprint,
                        "access_consequence": preview.access_consequence,
                    },
                )
            if commit:
                db.commit()
                db.refresh(draft)
                db.refresh(funding)
        except Exception:
            db.rollback()
            raise
        return CreditIssueResult(
            credit_note=draft,
            funding_ledger_entry=funding,
            preview=preview,
        )

    @staticmethod
    def get(db: Session, credit_note_id: str):
        credit_note = get_by_id(
            db,
            CreditNote,
            credit_note_id,
            options=[
                selectinload(CreditNote.lines),
                selectinload(CreditNote.applications),
            ],
        )
        if not credit_note:
            raise HTTPException(status_code=404, detail="Credit note not found")
        return credit_note

    @staticmethod
    def list(
        db: Session,
        account_id: str | None,
        invoice_id: str | None,
        status: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
        *,
        updated_since: datetime | None = None,
    ):
        query = db.query(CreditNote).options(
            selectinload(CreditNote.lines),
            selectinload(CreditNote.applications),
        )
        if account_id:
            query = query.filter(CreditNote.account_id == account_id)
        if invoice_id:
            query = query.filter(CreditNote.invoice_id == coerce_uuid(invoice_id))
        if status:
            query = query.filter(
                CreditNote.status == validate_enum(status, CreditNoteStatus, "status")
            )
        if is_active is None:
            query = query.filter(CreditNote.is_active.is_(True))
        else:
            query = query.filter(CreditNote.is_active == is_active)
        # Incremental-sync watermark (see Invoices.list); backed by
        # ix_credit_notes_is_active_updated_at.
        if updated_since is not None:
            query = query.filter(CreditNote.updated_at >= updated_since)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": CreditNote.created_at,
                "updated_at": CreditNote.updated_at,
                "status": CreditNote.status,
            },
        )
        # Stable, keyset-friendly tiebreaker for deterministic forward paging.
        query = query.order_by(CreditNote.id.asc())
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def list_for_sync(
        db: Session,
        *,
        account_id: str | None,
        status: str | None,
        is_active: bool | None,
        updated_since: datetime | None,
        limit: int,
        offset: int,
    ):
        query = db.query(CreditNote).options(
            selectinload(
                CreditNote.lines.and_(CreditNoteLine.is_active.is_(True))
            ).selectinload(CreditNoteLine.tax_rate)
        )
        if account_id:
            query = query.filter(CreditNote.account_id == account_id)
        if status:
            query = query.filter(
                CreditNote.status == validate_enum(status, CreditNoteStatus, "status")
            )
        if is_active is None:
            query = query.filter(CreditNote.is_active.is_(True))
        else:
            query = query.filter(CreditNote.is_active == is_active)
        return apply_sync_page(
            query,
            CreditNote,
            updated_since=updated_since,
            limit=limit,
            offset=offset,
        ).all()

    @classmethod
    def sync_list_response(cls, db: Session, **kwargs):
        items = cls.list_for_sync(db, **kwargs)
        return sync_page_response(items, limit=kwargs["limit"], offset=kwargs["offset"])

    @staticmethod
    def update(db: Session, credit_note_id: str, payload: CreditNoteUpdate):
        credit_note = get_by_id(db, CreditNote, credit_note_id)
        if not credit_note:
            raise HTTPException(status_code=404, detail="Credit note not found")
        if credit_note.status != CreditNoteStatus.draft:
            raise HTTPException(
                status_code=409, detail="Only draft credit notes can be edited"
            )
        data = payload.model_dump(exclude_unset=True)
        if "status" in data:
            raise HTTPException(
                status_code=409,
                detail="Use the credit-note issue or void workflow for status changes",
            )
        if "account_id" in data:
            _validate_account(db, str(data["account_id"]))
        if "invoice_id" in data:
            invoice = get_by_id(db, Invoice, data["invoice_id"])
            if not invoice:
                raise HTTPException(status_code=404, detail="Invoice not found")
            if invoice.account_id != credit_note.account_id:
                raise HTTPException(
                    status_code=400, detail="Invoice does not belong to account"
                )
            if "currency" in data:
                if data["currency"] != invoice.currency:
                    raise HTTPException(
                        status_code=400, detail="Currency does not match invoice"
                    )
            elif credit_note.currency != invoice.currency:
                raise HTTPException(
                    status_code=400, detail="Currency does not match invoice"
                )
        merged = {
            "subtotal": data.get("subtotal", credit_note.subtotal),
            "tax_total": data.get("tax_total", credit_note.tax_total),
            "total": data.get("total", credit_note.total),
            "applied_total": credit_note.applied_total,
        }
        _validate_credit_note_totals(merged)
        if (
            data.get("status")
            in {
                CreditNoteStatus.issued,
                CreditNoteStatus.partially_applied,
                CreditNoteStatus.applied,
            }
            and credit_note.issued_at is None
        ):
            credit_note.issued_at = datetime.now(UTC)
        for key, value in data.items():
            setattr(credit_note, key, value)
        db.commit()
        db.refresh(credit_note)
        return credit_note

    @staticmethod
    def delete(db: Session, credit_note_id: str):
        credit_note = get_by_id(db, CreditNote, credit_note_id)
        if not credit_note:
            raise HTTPException(status_code=404, detail="Credit note not found")
        if credit_note.status != CreditNoteStatus.draft:
            raise HTTPException(
                status_code=409, detail="Only draft credit notes can be deleted"
            )
        credit_note.is_active = False
        db.commit()

    @staticmethod
    def preview_void(db: Session, credit_note_id: str) -> CreditVoidPreview:
        credit_note = get_by_id(db, CreditNote, credit_note_id)
        if not credit_note:
            raise HTTPException(status_code=404, detail="Credit note not found")
        return _build_void_preview(db, credit_note)

    @staticmethod
    def _void_replay(
        db: Session, *, key: str, credit_note_id: str, preview_fingerprint: str
    ) -> CreditVoidResult | None:
        reservation = db.scalar(
            select(IdempotencyKey).where(
                IdempotencyKey.scope == _VOID_IDEMPOTENCY_SCOPE,
                IdempotencyKey.key == key,
            )
        )
        if reservation is None:
            return None
        if reservation.ref_id != str(credit_note_id):
            raise HTTPException(
                status_code=409, detail="Idempotency key belongs to another credit note"
            )
        credit_note = get_by_id(db, CreditNote, credit_note_id)
        if (
            not credit_note
            or not credit_note.void_ledger_entry_id
            or credit_note.status != CreditNoteStatus.void
        ):
            raise HTTPException(
                status_code=409, detail="Credit note void evidence is incomplete"
            )
        if credit_note.void_preview_fingerprint != preview_fingerprint:
            raise HTTPException(
                status_code=409,
                detail="Idempotency key was used with a different void confirmation",
            )
        void_entry = db.get(LedgerEntry, credit_note.void_ledger_entry_id)
        if not void_entry:
            raise HTTPException(
                status_code=409, detail="Credit note void ledger evidence was not found"
            )
        return CreditVoidResult(
            credit_note=credit_note,
            void_ledger_entry=void_entry,
            preview=None,
            idempotent_replay=True,
        )

    @staticmethod
    def void_with_evidence(
        db: Session,
        credit_note_id: str,
        payload: CreditNoteVoidRequest,
        *,
        commit: bool = True,
        stage_audit: bool = True,
    ) -> CreditVoidResult:
        key = _normalize_idempotency_key(payload.idempotency_key)
        replay = CreditNotes._void_replay(
            db,
            key=key,
            credit_note_id=credit_note_id,
            preview_fingerprint=payload.preview_fingerprint,
        )
        if replay:
            return replay
        credit_note = get_by_id(db, CreditNote, credit_note_id)
        if not credit_note:
            raise HTTPException(status_code=404, detail="Credit note not found")
        lock_account(db, str(credit_note.account_id))
        credit_note = lock_for_update(db, CreditNote, coerce_uuid(credit_note_id))
        if not credit_note:
            raise HTTPException(status_code=404, detail="Credit note not found")
        preview = _build_void_preview(db, credit_note)
        if preview.fingerprint != payload.preview_fingerprint:
            raise HTTPException(
                status_code=409,
                detail="Financial state changed after preview; preview again",
            )
        reservation = IdempotencyKey(
            scope=_VOID_IDEMPOTENCY_SCOPE,
            key=key,
            account_id=credit_note.account_id,
            ref_id=str(credit_note.id),
        )
        db.add(reservation)
        try:
            db.flush()
            reversal = LedgerEntries.reverse(
                db,
                str(preview.reverses_ledger_entry_id),
                memo=payload.memo
                or f"Void credit note {credit_note.credit_number or credit_note.id}",
                commit=False,
            )
            credit_note.status = CreditNoteStatus.void
            credit_note.void_ledger_entry_id = reversal.id
            credit_note.void_preview_fingerprint = preview.fingerprint
            db.flush()
            if stage_audit:
                _stage_credit_audit(
                    db,
                    action="void",
                    credit_note_id=credit_note.id,
                    metadata={
                        "funding_ledger_entry_id": str(
                            credit_note.funding_ledger_entry_id
                        ),
                        "void_ledger_entry_id": str(reversal.id),
                        "amount": str(preview.ledger_amount),
                        "currency": preview.currency,
                        "preview_fingerprint": preview.fingerprint,
                        "access_consequence": preview.access_consequence,
                    },
                )
            if commit:
                db.commit()
                db.refresh(credit_note)
                db.refresh(reversal)
        except IntegrityError as exc:
            db.rollback()
            replay = CreditNotes._void_replay(
                db,
                key=key,
                credit_note_id=credit_note_id,
                preview_fingerprint=payload.preview_fingerprint,
            )
            if replay:
                return replay
            raise HTTPException(
                status_code=409, detail="Credit note void is already being processed"
            ) from exc
        except Exception:
            db.rollback()
            raise
        return CreditVoidResult(
            credit_note=credit_note,
            void_ledger_entry=reversal,
            preview=preview,
        )

    @staticmethod
    def void_system(
        db: Session,
        credit_note_id: str,
        *,
        idempotency_key: str,
        memo: str | None = None,
        commit: bool = False,
    ) -> CreditVoidResult:
        preview = CreditNotes.preview_void(db, credit_note_id)
        return CreditNotes.void_with_evidence(
            db,
            credit_note_id,
            CreditNoteVoidRequest(
                memo=memo,
                preview_fingerprint=preview.fingerprint,
                idempotency_key=idempotency_key,
            ),
            commit=commit,
        )

    @staticmethod
    def reconcile_funding_evidence(
        db: Session,
        credit_note_id: str,
        *,
        apply: bool = False,
        existing_ledger_entry_id: str | None = None,
        create_missing: bool = False,
    ) -> CreditFundingReconciliation:
        """Report or explicitly repair historical funding evidence.

        The owner never guesses a ledger link. Applying a repair requires an
        operator-selected entry or an explicit request to post missing funding.
        """
        credit_note = get_by_id(db, CreditNote, credit_note_id)
        if not credit_note:
            raise HTTPException(status_code=404, detail="Credit note not found")
        remaining = round_money(credit_note.total - credit_note.applied_total)
        if credit_note.status in {CreditNoteStatus.draft, CreditNoteStatus.void}:
            return CreditFundingReconciliation(
                credit_note_id=credit_note.id,
                status="not_applicable",
                remaining_amount=remaining,
                funding_ledger_entry_id=credit_note.funding_ledger_entry_id,
                applied=False,
            )
        if credit_note.funding_ledger_entry_id:
            entry = db.get(LedgerEntry, credit_note.funding_ledger_entry_id)
            if not entry:
                raise HTTPException(
                    status_code=409, detail="Linked funding entry was not found"
                )
            return CreditFundingReconciliation(
                credit_note_id=credit_note.id,
                status="linked",
                remaining_amount=remaining,
                funding_ledger_entry_id=entry.id,
                applied=False,
            )
        if not apply:
            return CreditFundingReconciliation(
                credit_note_id=credit_note.id,
                status="missing_review_required",
                remaining_amount=remaining,
                funding_ledger_entry_id=None,
                applied=False,
            )
        if bool(existing_ledger_entry_id) == bool(create_missing):
            raise HTTPException(
                status_code=400,
                detail="Choose exactly one repair: link an entry or create missing funding",
            )
        lock_account(db, str(credit_note.account_id))
        credit_note = lock_for_update(db, CreditNote, credit_note.id)
        if not credit_note:
            raise HTTPException(status_code=404, detail="Credit note not found")
        remaining = round_money(credit_note.total - credit_note.applied_total)
        if remaining <= 0:
            raise HTTPException(
                status_code=409, detail="Credit note has no remaining amount to fund"
            )
        if existing_ledger_entry_id:
            entry = db.get(LedgerEntry, coerce_uuid(existing_ledger_entry_id))
            if not entry:
                raise HTTPException(status_code=404, detail="Ledger entry not found")
            _validate_funding_entry(credit_note, entry, expected_amount=remaining)
            already_linked = db.scalar(
                select(CreditNote.id).where(
                    CreditNote.funding_ledger_entry_id == entry.id,
                    CreditNote.id != credit_note.id,
                )
            )
            if already_linked:
                raise HTTPException(
                    status_code=409, detail="Ledger entry funds another credit note"
                )
        else:
            entry = LedgerEntries.create(
                db,
                LedgerEntryCreate(
                    account_id=credit_note.account_id,
                    entry_type=LedgerEntryType.credit,
                    source=LedgerSource.credit_note,
                    amount=remaining,
                    currency=credit_note.currency,
                    memo=f"Reviewed historical funding for credit note {credit_note.credit_number or credit_note.id}",
                ),
                commit=False,
            )
        credit_note.funding_ledger_entry_id = entry.id
        _stage_credit_audit(
            db,
            action="reconcile_funding",
            credit_note_id=credit_note.id,
            metadata={
                "funding_ledger_entry_id": str(entry.id),
                "remaining_amount": str(remaining),
                "currency": credit_note.currency,
                "repair": "link_existing"
                if existing_ledger_entry_id
                else "create_missing",
            },
        )
        db.commit()
        return CreditFundingReconciliation(
            credit_note_id=credit_note.id,
            status="linked",
            remaining_amount=remaining,
            funding_ledger_entry_id=entry.id,
            applied=True,
        )

    @staticmethod
    def preview_application(
        db: Session,
        credit_note_id: str,
        payload: CreditNoteApplicationPreviewRequest,
    ) -> CreditApplicationPreview:
        credit_note = get_by_id(db, CreditNote, credit_note_id)
        if not credit_note:
            raise HTTPException(status_code=404, detail="Credit note not found")
        invoice = get_by_id(db, Invoice, payload.invoice_id)
        if not invoice:
            raise HTTPException(status_code=404, detail="Invoice not found")
        return _build_application_preview(credit_note, invoice, payload.amount)

    @staticmethod
    def list_application_options(
        db: Session, invoice_id: str
    ) -> Sequence[CreditApplicationOption]:
        """Return owner-filtered credit choices; templates do no arithmetic."""
        invoice = get_by_id(db, Invoice, invoice_id)
        if not invoice:
            raise HTTPException(status_code=404, detail="Invoice not found")
        if (
            invoice.is_proforma
            or invoice.status not in _CREDIT_APPLICABLE_INVOICE_STATUSES
            or invoice.balance_due <= 0
        ):
            return []
        notes = (
            db.query(CreditNote)
            .filter(CreditNote.account_id == invoice.account_id)
            .filter(CreditNote.currency == invoice.currency)
            .filter(CreditNote.is_active.is_(True))
            .filter(
                CreditNote.status.in_(
                    [CreditNoteStatus.issued, CreditNoteStatus.partially_applied]
                )
            )
            .filter(
                or_(
                    CreditNote.invoice_id.is_(None), CreditNote.invoice_id == invoice.id
                )
            )
            .order_by(CreditNote.created_at.desc())
            .all()
        )
        options: list[CreditApplicationOption] = []
        for note in notes:
            available = round_money(note.total - note.applied_total)
            if available <= 0:
                continue
            options.append(
                CreditApplicationOption(
                    credit_note_id=note.id,
                    credit_number=note.credit_number,
                    currency=note.currency,
                    available_amount=available,
                    max_applicable_amount=min(
                        available, round_money(invoice.balance_due)
                    ),
                )
            )
        return options

    @staticmethod
    def _idempotent_result(
        db: Session,
        *,
        key: str,
        credit_note_id: str,
        invoice_id: object,
        amount: Decimal,
        preview_fingerprint: str,
    ) -> CreditApplicationResult | None:
        reservation = db.scalars(
            select(IdempotencyKey).where(
                IdempotencyKey.scope == _APPLICATION_IDEMPOTENCY_SCOPE,
                IdempotencyKey.key == key,
            )
        ).first()
        if reservation is None:
            return None
        if not reservation.ref_id:
            raise HTTPException(
                status_code=409,
                detail="Credit application is already being processed",
            )
        application = get_by_id(db, CreditNoteApplication, reservation.ref_id)
        if not application:
            raise HTTPException(
                status_code=409,
                detail="Credit application evidence is incomplete",
            )
        if str(application.credit_note_id) != str(credit_note_id) or str(
            application.invoice_id
        ) != str(invoice_id):
            raise HTTPException(
                status_code=409,
                detail="Idempotency key was used for another credit application",
            )
        if (
            round_money(application.amount) != round_money(amount)
            or application.preview_fingerprint != preview_fingerprint
        ):
            raise HTTPException(
                status_code=409,
                detail="Idempotency key was used with a different confirmation",
            )
        if not application.ledger_entry_id:
            raise HTTPException(
                status_code=409,
                detail="Credit application is missing ledger evidence",
            )
        ledger_entry = db.get(LedgerEntry, application.ledger_entry_id)
        if ledger_entry is None:
            raise HTTPException(
                status_code=409,
                detail="Credit application ledger evidence was not found",
            )
        return CreditApplicationResult(
            application=application,
            ledger_entry=ledger_entry,
            consumption_ledger_entry=(
                db.get(LedgerEntry, application.consumption_ledger_entry_id)
                if application.consumption_ledger_entry_id
                else None
            ),
            preview=None,
            idempotent_replay=True,
        )

    @staticmethod
    def apply_with_evidence(
        db: Session,
        credit_note_id: str,
        payload: CreditNoteApplyRequest,
        *,
        stage_audit: bool = True,
    ) -> CreditApplicationResult:
        key = _normalize_idempotency_key(payload.idempotency_key)
        replay = CreditNotes._idempotent_result(
            db,
            key=key,
            credit_note_id=credit_note_id,
            invoice_id=payload.invoice_id,
            amount=payload.amount,
            preview_fingerprint=payload.preview_fingerprint,
        )
        if replay is not None:
            return replay

        initial_credit_note = get_by_id(db, CreditNote, credit_note_id)
        if not initial_credit_note:
            raise HTTPException(status_code=404, detail="Credit note not found")
        # Account -> CreditNote -> Invoice is the common wallet mutation lock
        # order. The confirmed amounts are recomputed under those locks.
        lock_account(db, str(initial_credit_note.account_id))
        credit_note = lock_for_update(db, CreditNote, coerce_uuid(credit_note_id))
        if not credit_note:
            raise HTTPException(status_code=404, detail="Credit note not found")
        invoice = lock_for_update(db, Invoice, coerce_uuid(payload.invoice_id))
        if not invoice:
            raise HTTPException(status_code=404, detail="Invoice not found")

        replay = CreditNotes._idempotent_result(
            db,
            key=key,
            credit_note_id=credit_note_id,
            invoice_id=payload.invoice_id,
            amount=payload.amount,
            preview_fingerprint=payload.preview_fingerprint,
        )
        if replay is not None:
            return replay

        preview = _build_application_preview(credit_note, invoice, payload.amount)
        if payload.preview_fingerprint != preview.fingerprint:
            raise HTTPException(
                status_code=409,
                detail="Financial state changed after preview; preview again",
            )

        reservation = IdempotencyKey(
            scope=_APPLICATION_IDEMPOTENCY_SCOPE,
            key=key,
            account_id=invoice.account_id,
        )
        db.add(reservation)
        try:
            db.flush()
        except IntegrityError as exc:
            db.rollback()
            replay = CreditNotes._idempotent_result(
                db,
                key=key,
                credit_note_id=credit_note_id,
                invoice_id=payload.invoice_id,
                amount=payload.amount,
                preview_fingerprint=payload.preview_fingerprint,
            )
            if replay is not None:
                return replay
            raise HTTPException(
                status_code=409,
                detail="Credit application is already being processed",
            ) from exc

        try:
            consumption_entry = None
            if credit_note.funding_ledger_entry_id:
                available_funding = _funded_credit_available(db, credit_note)
                if available_funding < preview.apply_amount:
                    raise HTTPException(
                        status_code=409,
                        detail="Exact credit-note funding is below the confirmed application amount",
                    )
                consumption_entry = LedgerEntries.create(
                    db,
                    LedgerEntryCreate(
                        account_id=credit_note.account_id,
                        entry_type=LedgerEntryType.debit,
                        source=LedgerSource.credit_note,
                        amount=preview.apply_amount,
                        currency=credit_note.currency,
                        memo=(
                            "Credit note application funding transfer: "
                            f"{credit_note.id} -> {invoice.id}"
                        ),
                    ),
                    commit=False,
                )
            entry = LedgerEntries.create(
                db,
                LedgerEntryCreate(
                    account_id=invoice.account_id,
                    invoice_id=invoice.id,
                    entry_type=preview.ledger_entry_type,
                    source=preview.ledger_source,
                    amount=preview.apply_amount,
                    currency=preview.currency,
                    memo=payload.memo
                    or f"Credit note {credit_note.credit_number or credit_note.id} applied",
                ),
                commit=False,
            )
            application = CreditNoteApplication(
                credit_note_id=credit_note.id,
                invoice_id=invoice.id,
                ledger_entry_id=entry.id,
                consumption_ledger_entry_id=(
                    consumption_entry.id if consumption_entry else None
                ),
                preview_fingerprint=preview.fingerprint,
                amount=preview.apply_amount,
                memo=payload.memo,
            )
            db.add(application)
            db.flush()
            reservation.ref_id = str(application.id)
            _recalculate_invoice_totals(db, invoice)
            _recalculate_credit_note_totals(db, credit_note)
            db.flush()

            # Financial settlement and service access remain separate states.
            # The invoice owner hands the consequence to the access/lifecycle
            # owners; this action never promises restoration from the credit.
            from app.services.billing.invoices import (
                reconcile_service_after_invoice_settlement,
            )

            if preview.settles_invoice:
                reconcile_service_after_invoice_settlement(
                    db, invoice.account_id, invoice.id
                )
            if stage_audit:
                _stage_credit_audit(
                    db,
                    action="apply",
                    credit_note_id=credit_note.id,
                    metadata={
                        "application_id": str(application.id),
                        "invoice_id": str(invoice.id),
                        "ledger_entry_id": str(entry.id),
                        "consumption_ledger_entry_id": (
                            str(consumption_entry.id) if consumption_entry else None
                        ),
                        "amount": str(preview.apply_amount),
                        "currency": preview.currency,
                        "preview_fingerprint": preview.fingerprint,
                        "invoice_receivable_before": str(
                            preview.invoice_receivable_before
                        ),
                        "invoice_receivable_after": str(
                            preview.invoice_receivable_after
                        ),
                        "access_consequence": preview.access_consequence,
                    },
                )
            db.commit()
        except Exception:
            db.rollback()
            raise
        db.refresh(application)
        db.refresh(entry)
        return CreditApplicationResult(
            application=application,
            ledger_entry=entry,
            consumption_ledger_entry=consumption_entry,
            preview=preview,
        )

    @staticmethod
    def apply(db: Session, credit_note_id: str, payload: CreditNoteApplyRequest):
        """Apply a confirmed owner-produced preview and return its application."""
        return CreditNotes.apply_with_evidence(db, credit_note_id, payload).application


class CreditNoteLines(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: CreditNoteLineCreate):
        credit_note = get_by_id(db, CreditNote, payload.credit_note_id)
        if not credit_note:
            raise HTTPException(status_code=404, detail="Credit note not found")
        if credit_note.status != CreditNoteStatus.draft:
            raise HTTPException(
                status_code=409, detail="Only draft credit notes can be edited"
            )
        _resolve_tax_rate(db, str(payload.tax_rate_id) if payload.tax_rate_id else None)
        data = payload.model_dump(exclude={"amount"})
        amount = _validate_invoice_line_amount(
            payload.quantity, payload.unit_price, payload.amount
        )
        line = CreditNoteLine(**data, amount=amount)
        try:
            db.add(line)
            db.flush()
            _recalculate_credit_note_totals(db, credit_note)
            db.commit()
        except SQLAlchemyError:
            db.rollback()
            raise
        db.refresh(line)
        return line

    @staticmethod
    def get(db: Session, line_id: str):
        line = get_by_id(db, CreditNoteLine, line_id)
        if not line:
            raise HTTPException(status_code=404, detail="Credit note line not found")
        return line

    @staticmethod
    def list(
        db: Session,
        credit_note_id: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(CreditNoteLine)
        if credit_note_id:
            query = query.filter(CreditNoteLine.credit_note_id == credit_note_id)
        if is_active is None:
            query = query.filter(CreditNoteLine.is_active.is_(True))
        else:
            query = query.filter(CreditNoteLine.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": CreditNoteLine.created_at, "amount": CreditNoteLine.amount},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, line_id: str, payload: CreditNoteLineUpdate):
        line = get_by_id(db, CreditNoteLine, line_id)
        if not line:
            raise HTTPException(status_code=404, detail="Credit note line not found")
        credit_note = get_by_id(db, CreditNote, line.credit_note_id)
        if not credit_note or credit_note.status != CreditNoteStatus.draft:
            raise HTTPException(
                status_code=409, detail="Only draft credit notes can be edited"
            )
        data = payload.model_dump(exclude_unset=True)
        if "credit_note_id" in data:
            raise HTTPException(status_code=400, detail="Cannot change credit note")
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
            if credit_note:
                db.flush()
                _recalculate_credit_note_totals(db, credit_note)
            db.commit()
        except SQLAlchemyError:
            db.rollback()
            raise
        db.refresh(line)
        return line

    @staticmethod
    def delete(db: Session, line_id: str):
        line = get_by_id(db, CreditNoteLine, line_id)
        if not line:
            raise HTTPException(status_code=404, detail="Credit note line not found")
        credit_note = get_by_id(db, CreditNote, line.credit_note_id)
        if not credit_note or credit_note.status != CreditNoteStatus.draft:
            raise HTTPException(
                status_code=409, detail="Only draft credit notes can be edited"
            )
        line.is_active = False
        if credit_note:
            db.flush()
            _recalculate_credit_note_totals(db, credit_note)
        db.commit()


class CreditNoteApplications(ListResponseMixin):
    @staticmethod
    def get(db: Session, application_id: str):
        application = get_by_id(db, CreditNoteApplication, application_id)
        if not application:
            raise HTTPException(
                status_code=404, detail="Credit note application not found"
            )
        return application

    @staticmethod
    def list(
        db: Session,
        credit_note_id: str | None,
        invoice_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(CreditNoteApplication)
        if credit_note_id:
            query = query.filter(CreditNoteApplication.credit_note_id == credit_note_id)
        if invoice_id:
            query = query.filter(CreditNoteApplication.invoice_id == invoice_id)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": CreditNoteApplication.created_at,
                "amount": CreditNoteApplication.amount,
            },
        )
        return apply_pagination(query, limit, offset).all()
