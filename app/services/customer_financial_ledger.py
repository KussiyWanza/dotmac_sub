"""Canonical customer financial ledger.

This module is the customer-facing native money source of truth. It emits
succeeded payments, service charges, credit notes, refunds, and approved manual
adjustments. The archived Splynx mirror is migration evidence only and is never
queried by this runtime projection. Reviewed opening positions are owned by
``financial.prepaid_funding_reconstruction``.
"""

from __future__ import annotations

import enum
import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from sqlalchemy import and_, case, func, or_, select, union_all
from sqlalchemy.orm import Session

from app.models.billing import (
    AccountAdjustment,
    CreditNote,
    CreditNoteApplication,
    CreditNoteStatus,
    Invoice,
    InvoiceClosure,
    InvoiceClosureOrigin,
    InvoiceClosureType,
    InvoiceLine,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentAllocation,
    PaymentSettlement,
    PaymentStatus,
    ServiceEntitlement,
    ServiceEntitlementStatus,
)
from app.models.prepaid_funding import (
    PrepaidFundingBaseline,
    PrepaidOpeningFundingConsumption,
)
from app.services.common import coerce_uuid, round_money
from app.services.invoice_classification import (
    collectible_ar_invoice_filter,
    prepaid_subscription_invoice_ids,
)


@dataclass(frozen=True)
class CustomerFinancialEvent:
    id: str
    account_id: UUID
    entry_type: LedgerEntryType
    source: LedgerSource
    amount: Decimal
    currency: str
    memo: str
    occurred_at: datetime
    raw: Any | None = None

    @property
    def signed_amount(self) -> Decimal:
        amount = Decimal(str(self.amount or 0))
        return amount if self.entry_type == LedgerEntryType.credit else -amount

    @property
    def created_at(self) -> datetime:
        return self.occurred_at

    @property
    def effective_date(self) -> datetime:
        return self.occurred_at

    def statement_entry(self) -> SimpleNamespace:
        return SimpleNamespace(
            id=self.id,
            account_id=self.account_id,
            entry_type=SimpleNamespace(value=self.entry_type.value),
            source=SimpleNamespace(value=self.source.value),
            amount=self.amount,
            currency=self.currency,
            memo=self.memo,
            effective_date=self.occurred_at,
            created_at=self.occurred_at,
            is_active=True,
        )


class PrepaidInvoiceConsumptionDisposition(str, enum.Enum):
    projected = "projected"
    already_represented = "already_represented"
    quarantined = "quarantined"


@dataclass(frozen=True)
class PrepaidInvoiceConsumptionItem:
    invoice_id: UUID
    account_id: UUID
    amount: Decimal
    currency: str
    disposition: PrepaidInvoiceConsumptionDisposition
    reason: str


@dataclass(frozen=True)
class PrepaidInvoiceConsumptionPreview:
    items: tuple[PrepaidInvoiceConsumptionItem, ...]
    fingerprint: str

    @property
    def projected_count(self) -> int:
        return sum(
            item.disposition == PrepaidInvoiceConsumptionDisposition.projected
            for item in self.items
        )

    @property
    def already_represented_count(self) -> int:
        return sum(
            item.disposition == PrepaidInvoiceConsumptionDisposition.already_represented
            for item in self.items
        )

    @property
    def quarantined_count(self) -> int:
        return sum(
            item.disposition == PrepaidInvoiceConsumptionDisposition.quarantined
            for item in self.items
        )


def _money(value: object) -> Decimal:
    return round_money(Decimal(str(value or 0)))


def _event_date(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _recorded_at(event: CustomerFinancialEvent) -> datetime:
    """Return when Sub learned the fact, distinct from economic occurrence."""
    recorded = getattr(event.raw, "created_at", None)
    return _event_date(recorded or event.occurred_at)


def _crosses_position_boundary(
    event: CustomerFinancialEvent, *, position_at: datetime
) -> bool:
    """Include facts occurring OR first recorded after an opening position."""
    boundary = _event_date(position_at)
    return event.occurred_at > boundary or _recorded_at(event) > boundary


def _in_window(
    occurred_at: datetime, *, start: datetime | None, end: datetime | None
) -> bool:
    if start is not None and occurred_at < start:
        return False
    return not (end is not None and occurred_at >= end)


def _payment_event(payment: Payment) -> CustomerFinancialEvent | None:
    # A structurally evidenced settlement owns the customer value credited by
    # a payment. ``Payment.amount`` is the gross gateway charge and may include
    # a provider fee that never becomes customer funding. Historical payments
    # without settlement evidence retain the bounded gross fallback until they
    # are explicitly reconciled; guessing net value from ``provider_fee`` would
    # assign authority to an unproved field combination.
    settled_value = (
        _money(payment.settlement.amount)
        if payment.settlement is not None
        else _money(payment.amount)
    )
    customer_value = settled_value - _money(payment.refunded_amount)
    if customer_value <= 0 or payment.account_id is None:
        return None
    return CustomerFinancialEvent(
        id=f"payment:{payment.id}",
        account_id=payment.account_id,
        entry_type=LedgerEntryType.credit,
        source=LedgerSource.payment,
        amount=customer_value,
        currency=(
            payment.settlement.currency
            if payment.settlement is not None
            else payment.currency
        )
        or "NGN",
        memo=payment.memo or payment.receipt_number or "Payment received",
        occurred_at=_event_date(payment.paid_at or payment.created_at),
        raw=payment,
    )


def _external_allocation_event(
    allocation: PaymentAllocation,
) -> CustomerFinancialEvent | None:
    invoice = allocation.invoice
    payment = allocation.payment
    if invoice is None or payment is None:
        return None
    if payment.account_id == invoice.account_id:
        return None
    return CustomerFinancialEvent(
        id=f"external-allocation:{allocation.id}",
        account_id=invoice.account_id,
        entry_type=LedgerEntryType.credit,
        source=LedgerSource.payment,
        amount=_money(allocation.amount),
        currency=payment.currency or invoice.currency or "NGN",
        memo=allocation.memo or payment.memo or "Payment allocated to invoice",
        occurred_at=_event_date(allocation.created_at),
        raw=allocation,
    )


def _invoice_event(invoice: Invoice) -> CustomerFinancialEvent:
    return CustomerFinancialEvent(
        id=f"invoice:{invoice.id}",
        account_id=invoice.account_id,
        entry_type=LedgerEntryType.debit,
        source=LedgerSource.invoice,
        amount=_money(invoice.total),
        currency=invoice.currency or "NGN",
        memo=invoice.memo
        or (
            f"Invoice {invoice.invoice_number}"
            if invoice.invoice_number
            else "Service invoice"
        ),
        occurred_at=_event_date(invoice.issued_at or invoice.created_at),
        raw=invoice,
    )


def _paid_prepaid_invoice_consumption_event(
    invoice: Invoice,
) -> CustomerFinancialEvent:
    """Project one fully funded prepaid invoice as spent customer value.

    A prepaid invoice never becomes collectible AR, but once it is fully paid
    it is authoritative evidence that the customer consumed that amount for a
    service period. The ordinary invoice event cannot represent this because it
    intentionally excludes every prepaid invoice from receivables.
    """
    return CustomerFinancialEvent(
        id=f"prepaid-invoice-consumption:{invoice.id}",
        account_id=invoice.account_id,
        entry_type=LedgerEntryType.debit,
        source=LedgerSource.invoice,
        amount=_money(invoice.total),
        currency=invoice.currency or "NGN",
        memo=(
            f"Prepaid service consumed by invoice "
            f"{invoice.invoice_number or invoice.id}"
        ),
        occurred_at=_event_date(
            invoice.paid_at or invoice.issued_at or invoice.created_at
        ),
        raw=invoice,
    )


def _direct_renewal_documentary_invoice_ids():
    """Paid invoice ids whose value is already owned by an exact renewal debit.

    Some reviewed repairs attach invoice documentation after the canonical
    prepaid renewal adjustment and entitlement already consumed the money. The
    exact account, subscription, period, amount and currency match prevents the
    documentary invoice from becoming a second customer-position debit.
    """
    return (
        select(InvoiceLine.invoice_id)
        .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
        .join(
            ServiceEntitlement,
            ServiceEntitlement.subscription_id == InvoiceLine.subscription_id,
        )
        .join(
            AccountAdjustment,
            AccountAdjustment.ledger_entry_id
            == ServiceEntitlement.source_ledger_entry_id,
        )
        .where(
            InvoiceLine.is_active.is_(True),
            ServiceEntitlement.status == ServiceEntitlementStatus.active,
            AccountAdjustment.origin == "prepaid_service_renewal",
            AccountAdjustment.reversed_at.is_(None),
            AccountAdjustment.account_id == Invoice.account_id,
            AccountAdjustment.amount == Invoice.total,
            AccountAdjustment.currency == Invoice.currency,
            ServiceEntitlement.starts_at == Invoice.billing_period_start,
            ServiceEntitlement.ends_at == Invoice.billing_period_end,
        )
    )


def _exactly_settled_invoice_ids():
    """Invoice ids backed by exact active payment or credit applications.

    ``Invoice.status == paid`` is not funding evidence by itself. Requiring the
    canonical settlement rows prevents imported or manually toggled invoices
    from creating a customer-position debit without the matching credit that
    funded it.
    """
    payment_rows = (
        select(
            PaymentAllocation.invoice_id.label("invoice_id"),
            PaymentAllocation.amount.label("amount"),
        )
        .join(Payment, Payment.id == PaymentAllocation.payment_id)
        .where(
            PaymentAllocation.is_active.is_(True),
            Payment.is_active.is_(True),
            Payment.status.in_(
                (
                    PaymentStatus.succeeded,
                    PaymentStatus.partially_refunded,
                    PaymentStatus.refunded,
                )
            ),
            PaymentAllocation.amount > Decimal("0.00"),
        )
    )
    credit_rows = (
        select(
            CreditNoteApplication.invoice_id.label("invoice_id"),
            CreditNoteApplication.amount.label("amount"),
        )
        .join(CreditNote, CreditNote.id == CreditNoteApplication.credit_note_id)
        .where(
            CreditNote.is_active.is_(True),
            CreditNote.status.in_(
                (
                    CreditNoteStatus.issued,
                    CreditNoteStatus.partially_applied,
                    CreditNoteStatus.applied,
                )
            ),
            CreditNoteApplication.amount > Decimal("0.00"),
        )
    )
    opening_funding_rows = select(
        PrepaidOpeningFundingConsumption.invoice_id.label("invoice_id"),
        PrepaidOpeningFundingConsumption.amount.label("amount"),
    )
    settlement_rows = union_all(
        payment_rows,
        credit_rows,
        opening_funding_rows,
    ).subquery()
    return (
        select(settlement_rows.c.invoice_id)
        .join(Invoice, Invoice.id == settlement_rows.c.invoice_id)
        .group_by(settlement_rows.c.invoice_id, Invoice.total)
        .having(func.sum(settlement_rows.c.amount) >= Invoice.total)
    )


def _paid_prepaid_consumption_filter():
    """Exact paid prepaid invoices that own one customer-position debit."""
    return and_(
        Invoice.id.in_(prepaid_subscription_invoice_ids()),
        Invoice.id.in_(_exactly_settled_invoice_ids()),
        Invoice.id.notin_(_direct_renewal_documentary_invoice_ids()),
        Invoice.status == InvoiceStatus.paid,
        Invoice.balance_due <= Decimal("0.00"),
        Invoice.total > Decimal("0.00"),
        Invoice.currency.is_not(None),
        func.length(func.trim(Invoice.currency)) == 3,
    )


def preview_paid_prepaid_invoice_consumption(
    db: Session,
    *,
    account_ids: Iterable[str | UUID] | None = None,
    recorded_after: datetime | None = None,
) -> PrepaidInvoiceConsumptionPreview:
    """Classify the exact paid-prepaid-invoice position cohort read-only.

    The projection itself is a deterministic rebuild and therefore needs no
    compensating customer write. This preview exists for rollout evidence and
    monitoring: structurally invalid paid invoices remain visible instead of
    being silently treated as spendable-funding corrections.
    """
    query = (
        db.query(Invoice)
        .filter(Invoice.is_active.is_(True))
        .filter(Invoice.is_proforma.is_(False))
        .filter(Invoice.status == InvoiceStatus.paid)
        .filter(Invoice.id.in_(prepaid_subscription_invoice_ids()))
    )
    if account_ids is not None:
        ids = {coerce_uuid(account_id) for account_id in account_ids}
        if not ids:
            return PrepaidInvoiceConsumptionPreview(
                items=(), fingerprint=hashlib.sha256(b"[]").hexdigest()
            )
        query = query.filter(Invoice.account_id.in_(ids))
    if recorded_after is not None:
        query = query.filter(
            or_(
                Invoice.created_at > recorded_after,
                func.coalesce(
                    Invoice.paid_at,
                    Invoice.issued_at,
                    Invoice.created_at,
                )
                > recorded_after,
            )
        )
    invoices = query.order_by(Invoice.account_id, Invoice.id).all()
    invoice_ids = {invoice.id for invoice in invoices}
    represented_ids = (
        set(
            db.scalars(
                _direct_renewal_documentary_invoice_ids().where(
                    InvoiceLine.invoice_id.in_(invoice_ids)
                )
            ).all()
        )
        if invoice_ids
        else set()
    )
    settled_ids = (
        set(
            db.scalars(
                _exactly_settled_invoice_ids().where(Invoice.id.in_(invoice_ids))
            ).all()
        )
        if invoice_ids
        else set()
    )
    items: list[PrepaidInvoiceConsumptionItem] = []
    for invoice in invoices:
        amount = _money(invoice.total)
        currency = str(invoice.currency or "").strip().upper()
        if amount <= Decimal("0.00"):
            disposition = PrepaidInvoiceConsumptionDisposition.quarantined
            reason = "nonpositive_total"
        elif _money(invoice.balance_due) > Decimal("0.00"):
            disposition = PrepaidInvoiceConsumptionDisposition.quarantined
            reason = "paid_invoice_has_balance"
        elif len(currency) != 3:
            disposition = PrepaidInvoiceConsumptionDisposition.quarantined
            reason = "invalid_currency"
        elif invoice.id in represented_ids:
            disposition = PrepaidInvoiceConsumptionDisposition.already_represented
            reason = "exact_direct_renewal_debit_precedence"
        elif invoice.id not in settled_ids:
            disposition = PrepaidInvoiceConsumptionDisposition.quarantined
            reason = "missing_exact_settlement_evidence"
        else:
            disposition = PrepaidInvoiceConsumptionDisposition.projected
            reason = "paid_prepaid_invoice_consumption"
        items.append(
            PrepaidInvoiceConsumptionItem(
                invoice_id=invoice.id,
                account_id=invoice.account_id,
                amount=amount,
                currency=currency,
                disposition=disposition,
                reason=reason,
            )
        )
    payload = [
        {
            "invoice_id": str(item.invoice_id),
            "account_id": str(item.account_id),
            "amount": str(item.amount),
            "currency": item.currency,
            "disposition": item.disposition.value,
            "reason": item.reason,
        }
        for item in items
    ]
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return PrepaidInvoiceConsumptionPreview(items=tuple(items), fingerprint=fingerprint)


def _invoice_writeoff_event(closure: InvoiceClosure) -> CustomerFinancialEvent:
    invoice = closure.invoice
    return CustomerFinancialEvent(
        id=f"invoice-writeoff:{closure.id}",
        account_id=invoice.account_id,
        entry_type=LedgerEntryType.credit,
        source=LedgerSource.adjustment,
        amount=_money(closure.amount),
        currency=closure.currency or invoice.currency or "NGN",
        memo=closure.reason
        or f"Write-off invoice {invoice.invoice_number or invoice.id}",
        occurred_at=_event_date(closure.created_at),
        raw=closure,
    )


def _credit_note_event(note: CreditNote) -> CustomerFinancialEvent:
    return CustomerFinancialEvent(
        id=f"credit-note:{note.id}",
        account_id=note.account_id,
        entry_type=LedgerEntryType.credit,
        source=LedgerSource.credit_note,
        amount=_money(note.total),
        currency=note.currency or "NGN",
        memo=note.memo or note.credit_number or "Credit note",
        occurred_at=_event_date(note.created_at),
        raw=note,
    )


def _ledger_event(entry: LedgerEntry) -> CustomerFinancialEvent | None:
    if not entry.affects_customer_position:
        return None
    source = entry.source or LedgerSource.other
    return CustomerFinancialEvent(
        id=f"ledger:{entry.id}",
        account_id=entry.account_id,
        entry_type=entry.entry_type,
        source=source,
        amount=_money(entry.amount),
        currency=entry.currency or "NGN",
        memo=entry.memo or source.value.replace("_", " ").title(),
        occurred_at=_event_date(entry.effective_date or entry.created_at),
        raw=entry,
    )


def _baseline_event(baseline: PrepaidFundingBaseline) -> CustomerFinancialEvent:
    amount = _money(baseline.amount)
    return CustomerFinancialEvent(
        id=f"prepaid-opening:{baseline.id}",
        account_id=baseline.account_id,
        entry_type=(LedgerEntryType.credit if amount >= 0 else LedgerEntryType.debit),
        source=LedgerSource.adjustment,
        amount=abs(amount),
        currency=baseline.currency,
        memo="Reviewed prepaid opening position",
        occurred_at=_event_date(baseline.position_at),
        raw=baseline,
    )


def list_customer_financial_events(
    db: Session,
    account_id: str | UUID,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    currency: str | None = "NGN",
) -> list[CustomerFinancialEvent]:
    """List reviewed opening positions plus canonical native events.

    An active prepaid opening position replaces facts both economically occurred
    and recorded by Sub through its timestamp. It is not a fallback: the
    position is a signed, materialized Sub fact. A late-recorded, backdated fact
    still changes it because ``created_at`` crossed the authority boundary even
    when the business occurrence timestamp did not.
    """
    account_uuid = coerce_uuid(account_id)
    baseline_query = db.query(PrepaidFundingBaseline).filter(
        PrepaidFundingBaseline.account_id == account_uuid,
        PrepaidFundingBaseline.is_active.is_(True),
    )
    if currency is not None:
        baseline_query = baseline_query.filter(
            PrepaidFundingBaseline.currency == currency
        )
    baselines = baseline_query.all()
    baseline_by_currency = {baseline.currency: baseline for baseline in baselines}
    events: list[CustomerFinancialEvent] = []
    payment_query = (
        db.query(Payment)
        .filter(Payment.account_id == account_uuid)
        .filter(Payment.is_active.is_(True))
        .filter(
            Payment.status.in_(
                [
                    PaymentStatus.succeeded,
                    PaymentStatus.partially_refunded,
                    PaymentStatus.refunded,
                ]
            )
        )
    )
    if currency is not None:
        payment_query = payment_query.filter(Payment.currency == currency)
    events.extend(
        event for payment in payment_query.all() if (event := _payment_event(payment))
    )

    allocation_query = (
        db.query(PaymentAllocation)
        .join(Invoice, Invoice.id == PaymentAllocation.invoice_id)
        .join(Payment, Payment.id == PaymentAllocation.payment_id)
        .filter(Invoice.account_id == account_uuid)
        .filter(PaymentAllocation.is_active.is_(True))
        .filter(Payment.is_active.is_(True))
        .filter(
            Payment.status.in_(
                [
                    PaymentStatus.succeeded,
                    PaymentStatus.partially_refunded,
                    PaymentStatus.refunded,
                ]
            )
        )
        .filter(or_(Payment.account_id.is_(None), Payment.account_id != account_uuid))
    )
    if currency is not None:
        allocation_query = allocation_query.filter(Payment.currency == currency)
    events.extend(
        event
        for allocation in allocation_query.all()
        if (event := _external_allocation_event(allocation))
    )

    invoice_query = (
        db.query(Invoice)
        .filter(Invoice.account_id == account_uuid)
        .filter(Invoice.is_active.is_(True))
        .outerjoin(InvoiceClosure, InvoiceClosure.invoice_id == Invoice.id)
        .filter(
            or_(
                Invoice.status.in_(
                    [
                        InvoiceStatus.issued,
                        InvoiceStatus.partially_paid,
                        InvoiceStatus.overdue,
                        InvoiceStatus.paid,
                    ]
                ),
                and_(
                    Invoice.status == InvoiceStatus.written_off,
                    InvoiceClosure.closure_type == InvoiceClosureType.write_off,
                    InvoiceClosure.origin
                    != InvoiceClosureOrigin.historical_reconciliation,
                ),
            )
        )
        .filter(Invoice.is_proforma.is_(False))
        .filter(collectible_ar_invoice_filter())
    )
    if currency is not None:
        invoice_query = invoice_query.filter(Invoice.currency == currency)
    events.extend(_invoice_event(invoice) for invoice in invoice_query.all())

    prepaid_consumption_query = (
        db.query(Invoice)
        .filter(Invoice.account_id == account_uuid)
        .filter(Invoice.is_active.is_(True))
        .filter(Invoice.is_proforma.is_(False))
        .filter(_paid_prepaid_consumption_filter())
    )
    if currency is not None:
        prepaid_consumption_query = prepaid_consumption_query.filter(
            Invoice.currency == currency
        )
    events.extend(
        _paid_prepaid_invoice_consumption_event(invoice)
        for invoice in prepaid_consumption_query.all()
    )

    writeoff_query = (
        db.query(InvoiceClosure)
        .join(Invoice, Invoice.id == InvoiceClosure.invoice_id)
        .filter(Invoice.account_id == account_uuid)
        .filter(Invoice.is_active.is_(True))
        .filter(InvoiceClosure.closure_type == InvoiceClosureType.write_off)
        .filter(InvoiceClosure.origin != InvoiceClosureOrigin.historical_reconciliation)
    )
    if currency is not None:
        writeoff_query = writeoff_query.filter(InvoiceClosure.currency == currency)
    events.extend(_invoice_writeoff_event(closure) for closure in writeoff_query.all())

    credit_note_query = (
        db.query(CreditNote)
        .filter(CreditNote.account_id == account_uuid)
        .filter(CreditNote.is_active.is_(True))
        .filter(
            CreditNote.status.in_(
                [
                    CreditNoteStatus.issued,
                    CreditNoteStatus.partially_applied,
                    CreditNoteStatus.applied,
                ]
            )
        )
    )
    if currency is not None:
        credit_note_query = credit_note_query.filter(CreditNote.currency == currency)
    events.extend(_credit_note_event(note) for note in credit_note_query.all())

    ledger_query = (
        db.query(LedgerEntry)
        .filter(LedgerEntry.account_id == account_uuid)
        .filter(LedgerEntry.invoice_id.is_(None))
        .filter(LedgerEntry.is_active.is_(True))
        .filter(LedgerEntry.affects_customer_position.is_(True))
        .filter(
            or_(
                LedgerEntry.source.in_([LedgerSource.adjustment, LedgerSource.other]),
                # A refund entry that carries a payment_id is the LEDGER's
                # representation of a refund this reader has already counted on the
                # Payment document, via ``net_amount = amount - refunded_amount``
                # (see _payment_event). Counting both subtracted the refund TWICE:
                # refunding NGN X dropped the customer's available balance by NGN 2X,
                # which on a prepaid account is a false under-funding and a wrongful
                # suspension of someone we had just refunded.
                #
                # This mirrors the payment clause immediately below, which has always
                # required payment_id IS NULL for exactly the same reason. A refund
                # NOT linked to a payment (a bare ledger adjustment) is still counted.
                and_(
                    LedgerEntry.source == LedgerSource.refund,
                    LedgerEntry.payment_id.is_(None),
                ),
                and_(
                    LedgerEntry.source == LedgerSource.invoice,
                    LedgerEntry.entry_type == LedgerEntryType.debit,
                ),
                and_(
                    LedgerEntry.source == LedgerSource.payment,
                    LedgerEntry.payment_id.is_(None),
                ),
                # Credit-note documents are the customer-facing fact. Their
                # funding, application-transfer, and void-reversal ledger rows
                # are structural evidence and must not be counted a second time.
            )
        )
    )
    if currency is not None:
        ledger_query = ledger_query.filter(LedgerEntry.currency == currency)
    events.extend(
        event for entry in ledger_query.all() if (event := _ledger_event(entry))
    )

    events = [
        event
        for event in events
        if (
            event.currency not in baseline_by_currency
            or _crosses_position_boundary(
                event,
                position_at=baseline_by_currency[event.currency].position_at,
            )
        )
    ]
    events.extend(_baseline_event(baseline) for baseline in baselines)
    return sorted(
        (
            event
            for event in events
            if _in_window(event.occurred_at, start=start, end=end)
        ),
        key=lambda event: (event.occurred_at, event.id),
    )


def customer_financial_balances_by_currency(
    db: Session,
    account_ids: Iterable[str | UUID],
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> dict[UUID, dict[str, Decimal]]:
    """Aggregate native balances without materializing events."""
    account_uuids = sorted(
        {coerce_uuid(account_id) for account_id in account_ids}, key=str
    )
    if not account_uuids:
        return {}

    balances: dict[UUID, dict[str, Decimal]] = {
        account_id: {} for account_id in account_uuids
    }

    def add(rows) -> None:  # noqa: ANN001
        for account_id, currency, amount in rows:
            code = str(currency or "NGN")
            account_balances = balances[account_id]
            account_balances[code] = account_balances.get(
                code, Decimal("0.00")
            ) + round_money(Decimal(str(amount or 0)))

    payment_currency = func.coalesce(
        PaymentSettlement.currency,
        Payment.currency,
        "NGN",
    )
    payment_customer_value = func.coalesce(
        PaymentSettlement.amount,
        Payment.amount,
        0,
    ) - func.coalesce(Payment.refunded_amount, 0)
    payment_query = (
        db.query(
            Payment.account_id,
            payment_currency.label("currency"),
            func.sum(payment_customer_value).label("balance"),
        )
        .outerjoin(PaymentSettlement, PaymentSettlement.payment_id == Payment.id)
        .filter(Payment.account_id.in_(account_uuids))
        .filter(Payment.is_active.is_(True))
        .filter(
            Payment.status.in_(
                [
                    PaymentStatus.succeeded,
                    PaymentStatus.partially_refunded,
                    PaymentStatus.refunded,
                ]
            )
        )
        .filter(payment_customer_value > 0)
    )
    if start is not None:
        payment_query = payment_query.filter(
            or_(
                Payment.created_at > start,
                func.coalesce(Payment.paid_at, Payment.created_at) > start,
            )
        )
    if end is not None:
        payment_query = payment_query.filter(
            Payment.created_at <= end,
            func.coalesce(Payment.paid_at, Payment.created_at) <= end,
        )
    add(payment_query.group_by(Payment.account_id, payment_currency).all())

    allocation_currency = func.coalesce(Payment.currency, Invoice.currency, "NGN")
    allocation_query = (
        db.query(
            Invoice.account_id,
            allocation_currency.label("currency"),
            func.sum(PaymentAllocation.amount).label("balance"),
        )
        .join(Invoice, Invoice.id == PaymentAllocation.invoice_id)
        .join(Payment, Payment.id == PaymentAllocation.payment_id)
        .filter(Invoice.account_id.in_(account_uuids))
        .filter(PaymentAllocation.is_active.is_(True))
        .filter(Payment.is_active.is_(True))
        .filter(
            Payment.status.in_(
                [
                    PaymentStatus.succeeded,
                    PaymentStatus.partially_refunded,
                    PaymentStatus.refunded,
                ]
            )
        )
        .filter(
            or_(Payment.account_id.is_(None), Payment.account_id != Invoice.account_id)
        )
    )
    if start is not None:
        allocation_query = allocation_query.filter(PaymentAllocation.created_at > start)
    if end is not None:
        allocation_query = allocation_query.filter(PaymentAllocation.created_at <= end)
    add(allocation_query.group_by(Invoice.account_id, allocation_currency).all())

    invoice_currency = func.coalesce(Invoice.currency, "NGN")
    invoice_query = (
        db.query(
            Invoice.account_id,
            invoice_currency.label("currency"),
            (-func.sum(Invoice.total)).label("balance"),
        )
        .filter(Invoice.account_id.in_(account_uuids))
        .filter(Invoice.is_active.is_(True))
        .outerjoin(InvoiceClosure, InvoiceClosure.invoice_id == Invoice.id)
        .filter(
            or_(
                Invoice.status.in_(
                    [
                        InvoiceStatus.issued,
                        InvoiceStatus.partially_paid,
                        InvoiceStatus.overdue,
                        InvoiceStatus.paid,
                    ]
                ),
                and_(
                    Invoice.status == InvoiceStatus.written_off,
                    InvoiceClosure.closure_type == InvoiceClosureType.write_off,
                    InvoiceClosure.origin
                    != InvoiceClosureOrigin.historical_reconciliation,
                ),
            )
        )
        .filter(Invoice.is_proforma.is_(False))
        .filter(collectible_ar_invoice_filter())
    )
    if start is not None:
        invoice_query = invoice_query.filter(
            or_(
                Invoice.created_at > start,
                func.coalesce(Invoice.issued_at, Invoice.created_at) > start,
            )
        )
    if end is not None:
        invoice_query = invoice_query.filter(
            Invoice.created_at <= end,
            func.coalesce(Invoice.issued_at, Invoice.created_at) <= end,
        )
    add(invoice_query.group_by(Invoice.account_id, invoice_currency).all())

    prepaid_consumption_query = (
        db.query(
            Invoice.account_id,
            invoice_currency.label("currency"),
            (-func.sum(Invoice.total)).label("balance"),
        )
        .filter(Invoice.account_id.in_(account_uuids))
        .filter(Invoice.is_active.is_(True))
        .filter(Invoice.is_proforma.is_(False))
        .filter(_paid_prepaid_consumption_filter())
    )
    if start is not None:
        prepaid_consumption_query = prepaid_consumption_query.filter(
            or_(
                Invoice.created_at > start,
                func.coalesce(
                    Invoice.paid_at,
                    Invoice.issued_at,
                    Invoice.created_at,
                )
                > start,
            )
        )
    if end is not None:
        prepaid_consumption_query = prepaid_consumption_query.filter(
            Invoice.created_at <= end,
            func.coalesce(
                Invoice.paid_at,
                Invoice.issued_at,
                Invoice.created_at,
            )
            <= end,
        )
    add(prepaid_consumption_query.group_by(Invoice.account_id, invoice_currency).all())

    writeoff_currency = func.coalesce(InvoiceClosure.currency, "NGN")
    writeoff_query = (
        db.query(
            Invoice.account_id,
            writeoff_currency.label("currency"),
            func.sum(InvoiceClosure.amount).label("balance"),
        )
        .join(Invoice, Invoice.id == InvoiceClosure.invoice_id)
        .filter(Invoice.account_id.in_(account_uuids))
        .filter(Invoice.is_active.is_(True))
        .filter(InvoiceClosure.closure_type == InvoiceClosureType.write_off)
        .filter(InvoiceClosure.origin != InvoiceClosureOrigin.historical_reconciliation)
    )
    if start is not None:
        writeoff_query = writeoff_query.filter(InvoiceClosure.created_at > start)
    if end is not None:
        writeoff_query = writeoff_query.filter(InvoiceClosure.created_at <= end)
    add(writeoff_query.group_by(Invoice.account_id, writeoff_currency).all())

    note_currency = func.coalesce(CreditNote.currency, "NGN")
    note_query = (
        db.query(
            CreditNote.account_id,
            note_currency.label("currency"),
            func.sum(CreditNote.total).label("balance"),
        )
        .filter(CreditNote.account_id.in_(account_uuids))
        .filter(CreditNote.is_active.is_(True))
        .filter(
            CreditNote.status.in_(
                [
                    CreditNoteStatus.issued,
                    CreditNoteStatus.partially_applied,
                    CreditNoteStatus.applied,
                ]
            )
        )
    )
    if start is not None:
        note_query = note_query.filter(CreditNote.created_at > start)
    if end is not None:
        note_query = note_query.filter(CreditNote.created_at <= end)
    add(note_query.group_by(CreditNote.account_id, note_currency).all())

    ledger_currency = func.coalesce(LedgerEntry.currency, "NGN")
    ledger_signed = case(
        (LedgerEntry.entry_type == LedgerEntryType.credit, LedgerEntry.amount),
        else_=-LedgerEntry.amount,
    )
    ledger_query = (
        db.query(
            LedgerEntry.account_id,
            ledger_currency.label("currency"),
            func.sum(ledger_signed).label("balance"),
        )
        .filter(LedgerEntry.account_id.in_(account_uuids))
        .filter(LedgerEntry.invoice_id.is_(None))
        .filter(LedgerEntry.is_active.is_(True))
        .filter(LedgerEntry.affects_customer_position.is_(True))
        .filter(
            or_(
                LedgerEntry.source.in_([LedgerSource.adjustment, LedgerSource.other]),
                and_(
                    LedgerEntry.source == LedgerSource.refund,
                    LedgerEntry.payment_id.is_(None),
                ),
                and_(
                    LedgerEntry.source == LedgerSource.invoice,
                    LedgerEntry.entry_type == LedgerEntryType.debit,
                ),
                and_(
                    LedgerEntry.source == LedgerSource.payment,
                    LedgerEntry.payment_id.is_(None),
                ),
                # Credit-note ledger rows are structural evidence; the document
                # query above owns their customer-position effect.
            )
        )
    )
    if start is not None:
        ledger_query = ledger_query.filter(
            or_(
                LedgerEntry.created_at > start,
                func.coalesce(LedgerEntry.effective_date, LedgerEntry.created_at)
                > start,
            )
        )
    if end is not None:
        ledger_query = ledger_query.filter(
            LedgerEntry.created_at <= end,
            func.coalesce(LedgerEntry.effective_date, LedgerEntry.created_at) <= end,
        )
    add(ledger_query.group_by(LedgerEntry.account_id, ledger_currency).all())

    return balances


def native_customer_financial_balances_by_currency(
    db: Session,
    account_ids: Iterable[str | UUID],
    *,
    after: datetime,
    before: datetime | None = None,
) -> dict[UUID, dict[str, Decimal]]:
    """Aggregate native events crossing a reviewed opening-position boundary.

    This explicit post-baseline reader never queries the archived Splynx mirror
    and never activates legacy cut-off heuristics because mirror rows exist. A
    fact crosses the boundary when its economic occurrence OR its Sub
    ``created_at`` is later, so late-entered backdated money cannot disappear.
    """
    return customer_financial_balances_by_currency(
        db,
        account_ids,
        start=_event_date(after),
        end=_event_date(before) if before is not None else None,
    )


def calculate_customer_balance(
    db: Session, account_id: str | UUID, *, currency: str | None = "NGN"
) -> Decimal:
    return round_money(
        sum(
            (
                event.signed_amount
                for event in list_customer_financial_events(
                    db, account_id, currency=currency
                )
            ),
            Decimal("0.00"),
        )
    )
