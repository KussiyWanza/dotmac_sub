"""Tax source facts, reporting semantics, and the WHT evidence lifecycle.

Sub owns the billing facts: invoice and credit-note tax treatments, proof-backed
WHT amounts, and the official WHT evidence timeline. Dotmac ERP owns TaxCode
account mappings, double-entry journals, tax transactions, and financial
statements. This module therefore never stores account codes or creates ledger
postings.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models.audit import AuditActorType
from app.models.billing import (
    CreditNote,
    CreditNoteStatus,
    Invoice,
    InvoiceStatus,
    Payment,
)
from app.models.payment_proof import (
    WithholdingTaxRecord,
    WithholdingTaxStatus,
    WithholdingTaxTransition,
)
from app.models.subscriber import Reseller, Subscriber
from app.schemas.status_presentation import StatusPresentation
from app.services.audit_adapter import stage_audit_event
from app.services.domain_errors import DomainError
from app.services.events import EventType, emit_event
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.status_presentation import (
    credit_note_status_presentation,
    invoice_status_presentation,
    withholding_tax_status_presentation,
)

TAX_REPORT_ROW_LIMIT = 200
TAX_OPERATIONS_PAGE_SIZE = 25

REPORTABLE_INVOICE_STATUSES = (
    InvoiceStatus.issued,
    InvoiceStatus.partially_paid,
    InvoiceStatus.paid,
    InvoiceStatus.overdue,
    InvoiceStatus.written_off,
)
ADJUSTING_CREDIT_NOTE_STATUSES = (
    CreditNoteStatus.issued,
    CreditNoteStatus.partially_applied,
    CreditNoteStatus.applied,
)
OUTSTANDING_WHT_STATUSES = (
    WithholdingTaxStatus.pending,
    WithholdingTaxStatus.certified,
)

_MONEY_QUANTUM = Decimal("0.01")
_OWNER = "financial.tax_accounting"
_TRANSITION_DEFINITION = OwnerCommandDefinition(
    owner=_OWNER,
    concern="withholding-tax lifecycle",
    name="transition_withholding_tax",
)


class TaxAccountingError(DomainError, ValueError):
    """A tax source fact or lifecycle request is invalid."""


class TaxSourceNotFound(TaxAccountingError):
    """A requested source record does not exist."""


@dataclass(frozen=True, slots=True)
class OutputTaxInvoiceRow:
    invoice_id: uuid.UUID
    invoice_number: str | None
    tax_point_at: datetime
    currency: str
    tax_amount: Decimal
    gross_amount: Decimal
    status: str
    status_presentation: StatusPresentation


@dataclass(frozen=True, slots=True)
class OutputTaxTotal:
    currency: str
    invoice_count: int
    tax_amount: Decimal
    gross_amount: Decimal


@dataclass(frozen=True, slots=True)
class CreditNoteTaxRow:
    credit_note_id: uuid.UUID
    credit_number: str | None
    recognized_at: datetime
    currency: str
    tax_adjustment_amount: Decimal
    gross_credit_amount: Decimal
    status: str
    status_presentation: StatusPresentation


@dataclass(frozen=True, slots=True)
class CreditNoteTaxTotal:
    currency: str
    credit_note_count: int
    tax_adjustment_amount: Decimal
    gross_credit_amount: Decimal


@dataclass(frozen=True, slots=True)
class NetOutputTaxTotal:
    currency: str
    invoice_count: int
    credit_note_count: int
    output_tax_invoiced: Decimal
    credit_note_tax_adjustments: Decimal
    net_output_tax_liability: Decimal


@dataclass(frozen=True, slots=True)
class WithholdingTaxReportRow:
    record_id: uuid.UUID
    recognized_at: datetime
    currency: str
    gross_amount: Decimal
    net_cash_amount: Decimal
    wht_amount: Decimal
    wht_rate: Decimal | None
    status: WithholdingTaxStatus
    status_presentation: StatusPresentation
    account_id: uuid.UUID | None
    billing_account_id: uuid.UUID | None
    reseller_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class WithholdingTaxStatusTotal:
    status: WithholdingTaxStatus
    amount: Decimal


@dataclass(frozen=True, slots=True)
class WithholdingTaxTotal:
    currency: str
    record_count: int
    gross_amount: Decimal
    net_cash_amount: Decimal
    wht_amount: Decimal
    outstanding_wht_amount: Decimal
    by_status: tuple[WithholdingTaxStatusTotal, ...]


@dataclass(slots=True)
class _MutableWithholdingTaxTotal:
    record_count: int = 0
    gross_amount: Decimal = Decimal("0.00")
    net_cash_amount: Decimal = Decimal("0.00")
    wht_amount: Decimal = Decimal("0.00")
    outstanding_wht_amount: Decimal = Decimal("0.00")
    by_status: dict[WithholdingTaxStatus, Decimal] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TaxReportResult:
    report_basis: str
    date_from: str
    date_to: str
    invoice_rows: tuple[OutputTaxInvoiceRow, ...]
    output_tax_totals: tuple[OutputTaxTotal, ...]
    output_tax_invoice_count: int
    credit_note_rows: tuple[CreditNoteTaxRow, ...]
    credit_note_tax_totals: tuple[CreditNoteTaxTotal, ...]
    credit_note_count: int
    net_output_tax_totals: tuple[NetOutputTaxTotal, ...]
    wht_rows: tuple[WithholdingTaxReportRow, ...]
    wht_totals: tuple[WithholdingTaxTotal, ...]
    wht_record_count: int

    def to_context(self) -> dict[str, object]:
        """Serialize the typed read model only at the web presentation boundary."""

        return {
            "report_basis": self.report_basis,
            "date_from": self.date_from,
            "date_to": self.date_to,
            "invoice_rows": self.invoice_rows,
            "output_tax_totals": self.output_tax_totals,
            "output_tax_invoice_count": self.output_tax_invoice_count,
            "output_tax_rows_truncated": (
                self.output_tax_invoice_count > len(self.invoice_rows)
            ),
            "credit_note_rows": self.credit_note_rows,
            "credit_note_tax_totals": self.credit_note_tax_totals,
            "credit_note_count": self.credit_note_count,
            "credit_note_rows_truncated": (
                self.credit_note_count > len(self.credit_note_rows)
            ),
            "net_output_tax_totals": self.net_output_tax_totals,
            "wht_rows": self.wht_rows,
            "wht_totals": self.wht_totals,
            "wht_record_count": self.wht_record_count,
            "wht_rows_truncated": self.wht_record_count > len(self.wht_rows),
        }


@dataclass(frozen=True, slots=True)
class WithholdingTaxRecordSummary:
    record_id: uuid.UUID
    account_id: uuid.UUID | None
    billing_account_id: uuid.UUID | None
    reseller_id: uuid.UUID | None
    payment_id: uuid.UUID | None
    gross_amount: Decimal
    net_amount: Decimal
    wht_amount: Decimal
    wht_rate: Decimal | None
    currency: str
    status: WithholdingTaxStatus
    created_at: datetime


@dataclass(frozen=True, slots=True)
class WithholdingTaxOperationRecord:
    record_id: uuid.UUID
    account_id: uuid.UUID | None
    billing_account_id: uuid.UUID | None
    reseller_id: uuid.UUID | None
    reseller_name: str | None
    account_name: str | None
    currency: str
    wht_amount: Decimal
    status: WithholdingTaxStatus
    certificate_reference: str | None
    created_at: datetime
    resolved_at: datetime | None
    status_presentation: StatusPresentation


@dataclass(frozen=True, slots=True)
class TaxOperationsPagination:
    page: int
    page_size: int
    total: int
    page_count: int
    has_previous: bool
    has_next: bool


@dataclass(frozen=True, slots=True)
class TaxOperationsState:
    accounting_owner: str
    wht_records: tuple[WithholdingTaxOperationRecord, ...]
    wht_statuses: tuple[WithholdingTaxStatus, ...]
    wht_filter_status: str
    wht_search: str
    wht_pagination: TaxOperationsPagination
    date_from: str
    date_to: str

    def to_context(self) -> dict[str, object]:
        """Serialize the typed operator state at the web presentation boundary."""

        return {
            "accounting_owner": self.accounting_owner,
            "wht_records": self.wht_records,
            "wht_status_presentations": {
                str(record.record_id): record.status_presentation
                for record in self.wht_records
            },
            "wht_statuses": self.wht_statuses,
            "wht_filter_status": self.wht_filter_status,
            "wht_search": self.wht_search,
            "wht_pagination": self.wht_pagination,
            "date_from": self.date_from,
            "date_to": self.date_to,
        }


@dataclass(frozen=True, slots=True)
class TransitionWithholdingTaxCommand:
    record_id: uuid.UUID
    target_status: WithholdingTaxStatus
    certificate_reference: str | None = None
    notes: str | None = None


@dataclass(frozen=True, slots=True)
class WithholdingTaxTransitionResult:
    record_id: uuid.UUID
    previous_status: WithholdingTaxStatus
    status: WithholdingTaxStatus
    certificate_reference: str | None
    transitioned_at: datetime
    replayed: bool


def _error(code: str, message: str, **details: object) -> TaxAccountingError:
    return TaxAccountingError(
        code=f"{_OWNER}.{code}",
        message=message,
        details=details,
    )


def _not_found(record_id: uuid.UUID) -> TaxSourceNotFound:
    return TaxSourceNotFound(
        code=f"{_OWNER}.record_not_found",
        message="Withholding-tax record does not exist.",
        details={"record_id": str(record_id)},
    )


def _money(value: object) -> Decimal:
    return Decimal(str(value or 0)).quantize(_MONEY_QUANTUM)


def _currency(value: object) -> str:
    code = str(value or "").strip().upper()
    if len(code) != 3 or not code.isalpha():
        raise _error(
            "currency_invalid",
            "Tax source currency must be a three-letter code.",
        )
    return code


def _status_value(value: object) -> str:
    return str(getattr(value, "value", value))


def _date_filter(value: str | None, *, field: str) -> datetime | None:
    text_value = (value or "").strip()
    if not text_value:
        return None
    try:
        parsed = date.fromisoformat(text_value)
    except ValueError as exc:
        raise _error(
            "date_filter_invalid",
            "Tax report dates must use YYYY-MM-DD.",
            field=field,
        ) from exc
    return datetime.combine(parsed, time.min, tzinfo=UTC)


def _window_filters(date_from: str | None, date_to: str | None):
    start = _date_filter(date_from, field="date_from")
    end_date = _date_filter(date_to, field="date_to")
    end = end_date + timedelta(days=1) if end_date is not None else None
    if start is not None and end is not None and end <= start:
        end = start + timedelta(days=1)
    return start, end


def _invoice_filters(date_from: str | None, date_to: str | None):
    start, end = _window_filters(date_from, date_to)
    tax_point = func.coalesce(Invoice.issued_at, Invoice.created_at)
    filters = [
        Invoice.is_active.is_(True),
        Invoice.is_proforma.is_(False),
        Invoice.tax_total > 0,
        Invoice.status.in_(REPORTABLE_INVOICE_STATUSES),
    ]
    if start is not None:
        filters.append(tax_point >= start)
    if end is not None:
        filters.append(tax_point < end)
    return tax_point, filters


def _credit_note_filters(date_from: str | None, date_to: str | None):
    start, end = _window_filters(date_from, date_to)
    tax_point = func.coalesce(CreditNote.issued_at, CreditNote.created_at)
    filters = [
        CreditNote.is_active.is_(True),
        CreditNote.tax_total > 0,
        CreditNote.status.in_(ADJUSTING_CREDIT_NOTE_STATUSES),
    ]
    if start is not None:
        filters.append(tax_point >= start)
    if end is not None:
        filters.append(tax_point < end)
    return tax_point, filters


def _wht_filters(date_from: str | None, date_to: str | None):
    start, end = _window_filters(date_from, date_to)
    filters = [WithholdingTaxRecord.wht_amount > 0]
    if start is not None:
        filters.append(WithholdingTaxRecord.created_at >= start)
    if end is not None:
        filters.append(WithholdingTaxRecord.created_at < end)
    return filters


def _output_tax_projection(
    db: Session,
    *,
    date_from: str | None,
    date_to: str | None,
) -> tuple[tuple[OutputTaxInvoiceRow, ...], tuple[OutputTaxTotal, ...], int]:
    tax_point, filters = _invoice_filters(date_from, date_to)
    aggregates = db.execute(
        select(
            Invoice.currency,
            func.count(Invoice.id),
            func.sum(Invoice.tax_total),
            func.sum(Invoice.total),
        )
        .where(*filters)
        .group_by(Invoice.currency)
    ).all()
    totals = tuple(
        OutputTaxTotal(
            currency=_currency(currency),
            invoice_count=int(count or 0),
            tax_amount=_money(tax),
            gross_amount=_money(gross),
        )
        for currency, count, tax, gross in aggregates
    )
    totals = tuple(sorted(totals, key=lambda item: item.currency))
    invoices = list(
        db.scalars(
            select(Invoice)
            .where(*filters)
            .order_by(tax_point.desc(), Invoice.id.desc())
            .limit(TAX_REPORT_ROW_LIMIT)
        ).all()
    )
    rows = tuple(
        OutputTaxInvoiceRow(
            invoice_id=invoice.id,
            invoice_number=invoice.invoice_number,
            tax_point_at=invoice.issued_at or invoice.created_at,
            currency=_currency(invoice.currency),
            tax_amount=_money(invoice.tax_total),
            gross_amount=_money(invoice.total),
            status=_status_value(invoice.status),
            status_presentation=invoice_status_presentation(invoice.status),
        )
        for invoice in invoices
    )
    return rows, totals, sum(item.invoice_count for item in totals)


def _credit_note_tax_projection(
    db: Session, *, date_from: str | None, date_to: str | None
) -> tuple[tuple[CreditNoteTaxRow, ...], tuple[CreditNoteTaxTotal, ...], int]:
    tax_point, filters = _credit_note_filters(date_from, date_to)
    aggregates = db.execute(
        select(
            CreditNote.currency,
            func.count(CreditNote.id),
            func.sum(CreditNote.tax_total),
            func.sum(CreditNote.total),
        )
        .where(*filters)
        .group_by(CreditNote.currency)
    ).all()
    totals = tuple(
        CreditNoteTaxTotal(
            currency=_currency(currency),
            credit_note_count=int(count or 0),
            tax_adjustment_amount=_money(tax),
            gross_credit_amount=_money(gross),
        )
        for currency, count, tax, gross in aggregates
    )
    totals = tuple(sorted(totals, key=lambda item: item.currency))
    notes = list(
        db.scalars(
            select(CreditNote)
            .where(*filters)
            .order_by(tax_point.desc(), CreditNote.id.desc())
            .limit(TAX_REPORT_ROW_LIMIT)
        ).all()
    )
    rows = tuple(
        CreditNoteTaxRow(
            credit_note_id=note.id,
            credit_number=note.credit_number,
            recognized_at=note.issued_at or note.created_at,
            currency=_currency(note.currency),
            tax_adjustment_amount=_money(note.tax_total),
            gross_credit_amount=_money(note.total),
            status=_status_value(note.status),
            status_presentation=credit_note_status_presentation(note.status),
        )
        for note in notes
    )
    return rows, totals, sum(item.credit_note_count for item in totals)


def _net_output_tax_totals(
    output_totals: tuple[OutputTaxTotal, ...],
    credit_totals: tuple[CreditNoteTaxTotal, ...],
) -> tuple[NetOutputTaxTotal, ...]:
    currencies = {item.currency for item in output_totals} | {
        item.currency for item in credit_totals
    }
    rows: list[NetOutputTaxTotal] = []
    for currency in sorted(currencies):
        output = next(
            (item for item in output_totals if item.currency == currency), None
        )
        credit = next(
            (item for item in credit_totals if item.currency == currency), None
        )
        invoiced = output.tax_amount if output else Decimal("0.00")
        adjusted = credit.tax_adjustment_amount if credit else Decimal("0.00")
        rows.append(
            NetOutputTaxTotal(
                currency=currency,
                invoice_count=output.invoice_count if output else 0,
                credit_note_count=credit.credit_note_count if credit else 0,
                output_tax_invoiced=invoiced,
                credit_note_tax_adjustments=adjusted,
                net_output_tax_liability=invoiced - adjusted,
            )
        )
    return tuple(rows)


def _wht_projection(
    db: Session,
    *,
    date_from: str | None,
    date_to: str | None,
) -> tuple[tuple[WithholdingTaxReportRow, ...], tuple[WithholdingTaxTotal, ...], int]:
    filters = _wht_filters(date_from, date_to)
    aggregates = db.execute(
        select(
            WithholdingTaxRecord.currency,
            WithholdingTaxRecord.status,
            func.count(WithholdingTaxRecord.id),
            func.sum(WithholdingTaxRecord.gross_amount),
            func.sum(WithholdingTaxRecord.net_amount),
            func.sum(WithholdingTaxRecord.wht_amount),
        )
        .where(*filters)
        .group_by(WithholdingTaxRecord.currency, WithholdingTaxRecord.status)
    ).all()
    by_currency: dict[str, _MutableWithholdingTaxTotal] = {}
    for currency, status, count, gross, net, wht in aggregates:
        code = _currency(currency)
        bucket = by_currency.setdefault(code, _MutableWithholdingTaxTotal())
        amount = _money(wht)
        bucket.record_count += int(count or 0)
        bucket.gross_amount += _money(gross)
        bucket.net_cash_amount += _money(net)
        bucket.wht_amount += amount
        if status in OUTSTANDING_WHT_STATUSES:
            bucket.outstanding_wht_amount += amount
        status_key = WithholdingTaxStatus(status)
        bucket.by_status[status_key] = (
            bucket.by_status.get(status_key, Decimal("0.00")) + amount
        )
    records = list(
        db.scalars(
            select(WithholdingTaxRecord)
            .where(*filters)
            .order_by(
                WithholdingTaxRecord.created_at.desc(),
                WithholdingTaxRecord.id.desc(),
            )
            .limit(TAX_REPORT_ROW_LIMIT)
        ).all()
    )
    rows = tuple(
        WithholdingTaxReportRow(
            record_id=record.id,
            recognized_at=record.created_at,
            currency=_currency(record.currency),
            gross_amount=_money(record.gross_amount),
            net_cash_amount=_money(record.net_amount),
            wht_amount=_money(record.wht_amount),
            wht_rate=record.wht_rate,
            status=record.status,
            status_presentation=withholding_tax_status_presentation(record.status),
            account_id=record.account_id,
            billing_account_id=record.billing_account_id,
            reseller_id=record.reseller_id,
        )
        for record in records
    )
    totals = tuple(
        WithholdingTaxTotal(
            currency=key,
            record_count=by_currency[key].record_count,
            gross_amount=by_currency[key].gross_amount,
            net_cash_amount=by_currency[key].net_cash_amount,
            wht_amount=by_currency[key].wht_amount,
            outstanding_wht_amount=by_currency[key].outstanding_wht_amount,
            by_status=tuple(
                WithholdingTaxStatusTotal(status=status, amount=amount)
                for status, amount in sorted(
                    by_currency[key].by_status.items(),
                    key=lambda item: item[0].value,
                )
            ),
        )
        for key in sorted(by_currency)
    )
    return rows, totals, sum(item.record_count for item in totals)


def build_tax_report(
    db: Session, *, date_from: str | None = None, date_to: str | None = None
) -> TaxReportResult:
    """Build the read-only source-document tax register, grouped by currency."""
    invoice_rows, output_totals, invoice_count = _output_tax_projection(
        db, date_from=date_from, date_to=date_to
    )
    credit_rows, credit_totals, credit_count = _credit_note_tax_projection(
        db, date_from=date_from, date_to=date_to
    )
    wht_rows, wht_totals, wht_count = _wht_projection(
        db, date_from=date_from, date_to=date_to
    )
    return TaxReportResult(
        report_basis=(
            "output_tax_invoiced_less_credit_note_adjustments_and_wht_receivable"
        ),
        date_from=date_from or "",
        date_to=date_to or "",
        invoice_rows=invoice_rows,
        output_tax_totals=output_totals,
        output_tax_invoice_count=invoice_count,
        credit_note_rows=credit_rows,
        credit_note_tax_totals=credit_totals,
        credit_note_count=credit_count,
        net_output_tax_totals=_net_output_tax_totals(output_totals, credit_totals),
        wht_rows=wht_rows,
        wht_totals=wht_totals,
        wht_record_count=wht_count,
    )


def _initialize_withholding_tax_lifecycle(
    db: Session,
    record_id: uuid.UUID,
    *,
    actor_id: str | None,
) -> WithholdingTaxRecord:
    """Append initial pending-state evidence in the source transaction."""
    record = db.get(WithholdingTaxRecord, record_id)
    if record is None:
        raise _not_found(record_id)
    existing = db.scalar(
        select(WithholdingTaxTransition.id)
        .where(WithholdingTaxTransition.record_id == record_id)
        .limit(1)
    )
    if existing is None:
        db.add(
            WithholdingTaxTransition(
                record_id=record.id,
                from_status=None,
                to_status=record.status,
                actor_id=actor_id,
                certificate_reference=record.certificate_reference,
                notes="WHT receivable recognized",
                occurred_at=record.created_at or datetime.now(UTC),
            )
        )
        db.flush()
    return record


def stage_withholding_tax_receivable(
    db: Session,
    *,
    account_id: uuid.UUID | None,
    billing_account_id: uuid.UUID | None,
    reseller_id: uuid.UUID | None,
    payment_id: uuid.UUID,
    payment_proof_id: uuid.UUID,
    gross_amount: Decimal,
    net_amount: Decimal,
    wht_amount: Decimal,
    wht_rate: Decimal | None,
    vat_exclusive_amount: Decimal | None = None,
    vat_amount: Decimal | None = None,
    source_invoice_id: uuid.UUID | None = None,
    policy_version: int | None = None,
    currency: str,
    context: CommandContext,
) -> WithholdingTaxRecord:
    """Stage the canonical WHT source row and initial timeline evidence."""

    normalized_currency = _currency(currency)
    gross_value = _money(gross_amount)
    net_value = _money(net_amount)
    wht_value = _money(wht_amount)
    vat_exclusive_value = (
        _money(vat_exclusive_amount) if vat_exclusive_amount is not None else None
    )
    vat_value = _money(vat_amount) if vat_amount is not None else None
    if (account_id is None) == (billing_account_id is None):
        raise _error(
            "receivable_invalid",
            "WHT source evidence requires exactly one target account scope.",
        )
    if gross_value <= 0 or net_value <= 0 or wht_value <= 0:
        raise _error(
            "receivable_invalid",
            "WHT gross, net, and receivable amounts must all be positive.",
        )
    if gross_value != net_value + wht_value:
        raise _error(
            "receivable_invalid",
            "WHT gross amount must equal net cash plus the WHT receivable.",
        )
    if wht_rate is not None and not (Decimal("0") < wht_rate < Decimal("100")):
        raise _error(
            "receivable_invalid",
            "WHT rate must be greater than zero and less than 100 percent.",
        )

    existing = db.scalar(
        select(WithholdingTaxRecord)
        .where(WithholdingTaxRecord.payment_id == payment_id)
        .with_for_update()
    )
    if existing is not None:
        exact_replay = (
            existing.account_id == account_id
            and existing.billing_account_id == billing_account_id
            and existing.source_invoice_id == source_invoice_id
            and existing.reseller_id == reseller_id
            and existing.payment_proof_id == payment_proof_id
            and _money(existing.gross_amount) == gross_value
            and _money(existing.net_amount) == net_value
            and _money(existing.wht_amount) == wht_value
            and existing.wht_rate == wht_rate
            and existing.policy_version == policy_version
            and (
                _money(existing.vat_exclusive_amount)
                if existing.vat_exclusive_amount is not None
                else None
            )
            == vat_exclusive_value
            and (
                _money(existing.vat_amount) if existing.vat_amount is not None else None
            )
            == vat_value
            and _currency(existing.currency) == normalized_currency
        )
        if not exact_replay:
            raise _error(
                "receivable_conflict",
                "The payment already has different WHT source evidence.",
                payment_id=str(payment_id),
            )
        return existing

    record = WithholdingTaxRecord(
        account_id=account_id,
        billing_account_id=billing_account_id,
        reseller_id=reseller_id,
        payment_id=payment_id,
        payment_proof_id=payment_proof_id,
        gross_amount=gross_value,
        net_amount=net_value,
        wht_amount=wht_value,
        wht_rate=wht_rate,
        vat_exclusive_amount=vat_exclusive_value,
        vat_amount=vat_value,
        source_invoice_id=source_invoice_id,
        policy_version=policy_version,
        currency=normalized_currency,
        status=WithholdingTaxStatus.pending,
    )
    db.add(record)
    db.flush()
    _initialize_withholding_tax_lifecycle(db, record.id, actor_id=context.actor)
    emit_event(
        db,
        EventType.withholding_tax_receivable_recorded,
        {
            "schema_version": 1,
            "aggregate_type": "withholding_tax_record",
            "aggregate_id": str(record.id),
            "aggregate_version": str(context.command_id),
            "payment_proof_id": str(payment_proof_id),
            "payment_id": str(payment_id),
            "account_id": str(account_id) if account_id else None,
            "billing_account_id": (
                str(billing_account_id) if billing_account_id else None
            ),
            "source_invoice_id": str(source_invoice_id) if source_invoice_id else None,
            "policy_version": policy_version,
            "gross_amount": str(gross_value),
            "net_amount": str(net_value),
            "wht_amount": str(wht_value),
            "vat_exclusive_amount": (
                str(vat_exclusive_value) if vat_exclusive_value is not None else None
            ),
            "vat_amount": str(vat_value) if vat_value is not None else None,
            "currency": normalized_currency,
            "command_id": str(context.command_id),
            "correlation_id": str(context.correlation_id),
            "causation_id": (
                str(context.causation_id) if context.causation_id else None
            ),
        },
        actor=context.actor,
    )
    return record


def list_withholding_tax_records(
    db: Session,
    *,
    billing_account_id: str | None = None,
    reseller_id: str | None = None,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[WithholdingTaxRecordSummary, ...]:
    """Return canonical WHT source records for bounded admin/reseller views."""

    if limit < 1 or limit > TAX_REPORT_ROW_LIMIT or offset < 0:
        raise _error(
            "pagination_invalid",
            "WHT pagination is outside the supported bounds.",
        )

    query = db.query(WithholdingTaxRecord).order_by(
        WithholdingTaxRecord.created_at.desc()
    )
    if billing_account_id:
        try:
            normalized_billing_account_id = uuid.UUID(billing_account_id)
        except ValueError as exc:
            raise _error(
                "filter_invalid",
                "Billing account filter must be a UUID.",
                field="billing_account_id",
            ) from exc
        query = query.filter(
            WithholdingTaxRecord.billing_account_id == normalized_billing_account_id
        )
    if reseller_id:
        try:
            normalized_reseller_id = uuid.UUID(reseller_id)
        except ValueError as exc:
            raise _error(
                "filter_invalid",
                "Reseller filter must be a UUID.",
                field="reseller_id",
            ) from exc
        query = query.filter(WithholdingTaxRecord.reseller_id == normalized_reseller_id)
    if status:
        try:
            normalized_status = WithholdingTaxStatus(status)
        except ValueError as exc:
            raise _error(
                "filter_invalid",
                "Unknown withholding-tax status filter.",
                field="status",
            ) from exc
        query = query.filter(WithholdingTaxRecord.status == normalized_status)
    return tuple(
        WithholdingTaxRecordSummary(
            record_id=record.id,
            account_id=record.account_id,
            billing_account_id=record.billing_account_id,
            reseller_id=record.reseller_id,
            payment_id=record.payment_id,
            gross_amount=_money(record.gross_amount),
            net_amount=_money(record.net_amount),
            wht_amount=_money(record.wht_amount),
            wht_rate=record.wht_rate,
            currency=_currency(record.currency),
            status=record.status,
            created_at=record.created_at,
        )
        for record in query.offset(offset).limit(limit).all()
    )


def transition_withholding_tax(
    db: Session,
    command: TransitionWithholdingTaxCommand,
    *,
    context: CommandContext,
) -> WithholdingTaxTransitionResult:
    """Execute one atomic WHT lifecycle transition on a transaction-free session."""

    return execute_owner_command(
        db,
        definition=_TRANSITION_DEFINITION,
        context=context,
        operation=lambda: _stage_withholding_tax_transition(
            db, command, context=context
        ),
    )


def _stage_withholding_tax_transition(
    db: Session,
    command: TransitionWithholdingTaxCommand,
    *,
    context: CommandContext,
) -> WithholdingTaxTransitionResult:
    """Lock, validate, and stage WHT state, timeline, audit, and event evidence."""

    record = db.scalar(
        select(WithholdingTaxRecord)
        .where(WithholdingTaxRecord.id == command.record_id)
        .with_for_update()
    )
    if record is None:
        raise _not_found(command.record_id)
    _initialize_withholding_tax_lifecycle(db, record.id, actor_id=None)
    previous = record.status
    reference = (
        command.certificate_reference.strip() if command.certificate_reference else None
    )
    normalized_notes = command.notes.strip() if command.notes else None
    if previous == command.target_status:
        if reference is not None and reference != record.certificate_reference:
            raise _error(
                "replay_conflict",
                "The WHT transition was already completed with different evidence.",
                record_id=str(record.id),
                status=record.status.value,
            )
        return WithholdingTaxTransitionResult(
            record_id=record.id,
            previous_status=previous,
            status=record.status,
            certificate_reference=record.certificate_reference,
            transitioned_at=record.updated_at,
            replayed=True,
        )
    allowed = {
        WithholdingTaxStatus.pending: {
            WithholdingTaxStatus.certified,
            WithholdingTaxStatus.written_off,
        },
        WithholdingTaxStatus.certified: {
            WithholdingTaxStatus.reclaimed,
            WithholdingTaxStatus.written_off,
        },
        WithholdingTaxStatus.reclaimed: set(),
        WithholdingTaxStatus.written_off: set(),
    }
    if command.target_status not in allowed[previous]:
        raise _error(
            "illegal_transition",
            (
                "Illegal WHT transition: "
                f"{previous.value} -> {command.target_status.value}"
            ),
            record_id=str(record.id),
            previous_status=previous.value,
            target_status=command.target_status.value,
        )
    if command.target_status == WithholdingTaxStatus.certified and not (
        reference or record.certificate_reference or record.certificate_path
    ):
        raise _error(
            "certificate_required",
            "A certificate reference or stored certificate is required.",
            record_id=str(record.id),
        )
    if command.target_status == WithholdingTaxStatus.reclaimed and previous != (
        WithholdingTaxStatus.certified
    ):
        raise _error(
            "certification_required",
            "WHT must be certified before it is reclaimed.",
            record_id=str(record.id),
        )
    if (
        command.target_status == WithholdingTaxStatus.written_off
        and not normalized_notes
    ):
        raise _error(
            "write_off_reason_required",
            "A write-off reason is required.",
            record_id=str(record.id),
        )

    now = datetime.now(UTC)
    if reference:
        record.certificate_reference = reference
    if command.target_status == WithholdingTaxStatus.certified:
        record.certified_at = now
    if command.target_status in {
        WithholdingTaxStatus.reclaimed,
        WithholdingTaxStatus.written_off,
    }:
        record.resolved_at = now
    if normalized_notes:
        record.notes = (
            normalized_notes
            if not record.notes
            else f"{record.notes.rstrip()}\n{normalized_notes}"
        )
    record.status = command.target_status
    record.updated_at = now
    if record.payment_id is not None:
        payment = db.scalar(
            select(Payment).where(Payment.id == record.payment_id).with_for_update()
        )
        if payment is not None:
            payment.updated_at = now
    db.add(
        WithholdingTaxTransition(
            record_id=record.id,
            from_status=previous,
            to_status=command.target_status,
            actor_id=context.actor,
            certificate_reference=record.certificate_reference,
            notes=normalized_notes,
            occurred_at=now,
        )
    )
    stage_audit_event(
        db,
        action="transition",
        entity_type="withholding_tax_record",
        entity_id=str(record.id),
        actor_type=AuditActorType.user,
        actor_id=context.actor,
        request_id=str(context.correlation_id),
        metadata={
            "owner": _OWNER,
            "from_status": previous.value,
            "to_status": command.target_status.value,
            "certificate_reference": record.certificate_reference,
            "command_id": str(context.command_id),
            "command_scope": context.scope,
            "command_reason": context.reason,
        },
    )
    emit_event(
        db,
        EventType.withholding_tax_status_changed,
        {
            "schema_version": 1,
            "aggregate_type": "withholding_tax_record",
            "aggregate_id": str(record.id),
            "aggregate_version": str(context.command_id),
            "account_id": str(record.account_id) if record.account_id else None,
            "billing_account_id": (
                str(record.billing_account_id) if record.billing_account_id else None
            ),
            "payment_id": str(record.payment_id) if record.payment_id else None,
            "from_status": previous.value,
            "to_status": command.target_status.value,
            "occurred_at": now.isoformat(),
            "command_id": str(context.command_id),
            "correlation_id": str(context.correlation_id),
            "causation_id": (
                str(context.causation_id) if context.causation_id else None
            ),
        },
        actor=context.actor,
    )
    db.flush()
    return WithholdingTaxTransitionResult(
        record_id=record.id,
        previous_status=previous,
        status=record.status,
        certificate_reference=record.certificate_reference,
        transitioned_at=now,
        replayed=False,
    )


def build_tax_operations_state(
    db: Session,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    wht_status: WithholdingTaxStatus | None = None,
    wht_search: str | None = None,
    wht_page: int = 1,
) -> TaxOperationsState:
    """Build the bounded WHT operator queue; accounting remains in ERP."""
    if wht_page < 1:
        raise _error(
            "pagination_invalid",
            "WHT page must be at least 1.",
            page=wht_page,
        )
    normalized_search = (wht_search or "").strip()
    filters = []
    if wht_status is not None:
        filters.append(WithholdingTaxRecord.status == wht_status)
    if normalized_search:
        pattern = f"%{normalized_search}%"
        filters.append(
            or_(
                cast(WithholdingTaxRecord.id, String).ilike(pattern),
                cast(WithholdingTaxRecord.account_id, String).ilike(pattern),
                cast(WithholdingTaxRecord.billing_account_id, String).ilike(pattern),
                WithholdingTaxRecord.certificate_reference.ilike(pattern),
                Reseller.name.ilike(pattern),
                Subscriber.company_name.ilike(pattern),
                Subscriber.first_name.ilike(pattern),
                Subscriber.last_name.ilike(pattern),
                Subscriber.email.ilike(pattern),
            )
        )
    total = int(
        db.scalar(
            select(func.count(WithholdingTaxRecord.id))
            .select_from(WithholdingTaxRecord)
            .outerjoin(Reseller, Reseller.id == WithholdingTaxRecord.reseller_id)
            .outerjoin(Subscriber, Subscriber.id == WithholdingTaxRecord.account_id)
            .where(*filters)
        )
        or 0
    )
    records = list(
        db.scalars(
            select(WithholdingTaxRecord)
            .outerjoin(Reseller, Reseller.id == WithholdingTaxRecord.reseller_id)
            .outerjoin(Subscriber, Subscriber.id == WithholdingTaxRecord.account_id)
            .options(selectinload(WithholdingTaxRecord.account))
            .options(selectinload(WithholdingTaxRecord.reseller))
            .where(*filters)
            .order_by(
                WithholdingTaxRecord.created_at.desc(),
                WithholdingTaxRecord.id.desc(),
            )
            .offset((wht_page - 1) * TAX_OPERATIONS_PAGE_SIZE)
            .limit(TAX_OPERATIONS_PAGE_SIZE)
        ).all()
    )
    page_count = max(
        1, (total + TAX_OPERATIONS_PAGE_SIZE - 1) // TAX_OPERATIONS_PAGE_SIZE
    )
    return TaxOperationsState(
        accounting_owner="dotmac_erp",
        wht_records=tuple(
            WithholdingTaxOperationRecord(
                record_id=record.id,
                account_id=record.account_id,
                billing_account_id=record.billing_account_id,
                reseller_id=record.reseller_id,
                reseller_name=record.reseller.name if record.reseller else None,
                account_name=record.account.name if record.account else None,
                currency=_currency(record.currency),
                wht_amount=_money(record.wht_amount),
                status=record.status,
                certificate_reference=record.certificate_reference,
                created_at=record.created_at,
                resolved_at=record.resolved_at,
                status_presentation=withholding_tax_status_presentation(record.status),
            )
            for record in records
        ),
        wht_statuses=tuple(WithholdingTaxStatus),
        wht_filter_status=wht_status.value if wht_status is not None else "",
        wht_search=normalized_search,
        wht_pagination=TaxOperationsPagination(
            page=wht_page,
            page_size=TAX_OPERATIONS_PAGE_SIZE,
            total=total,
            page_count=page_count,
            has_previous=wht_page > 1,
            has_next=wht_page * TAX_OPERATIONS_PAGE_SIZE < total,
        ),
        date_from=date_from or "",
        date_to=date_to or "",
    )
