"""Common helper functions for billing services.

This module provides validation and calculation helpers shared across
billing service modules.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.models.billing import (
    CollectionAccount,
    CreditNote,
    CreditNoteApplication,
    CreditNoteLine,
    CreditNoteStatus,
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentAllocation,
    PaymentChannel,
    PaymentChannelAccount,
    PaymentMethod,
    PaymentProvider,
    PaymentStatus,
    TaxApplication,
    TaxRate,
)
from app.models.subscriber import Subscriber
from app.services.common import get_by_id, round_money, to_decimal
from app.services.locking import lock_for_update

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InvoiceSettlementAmounts:
    """Canonical invoice settlement components used by writes and read models."""

    payments_applied: Decimal
    credits_applied: Decimal


def resolve_invoice_settlement_amounts(
    db: Session, invoice_id: object
) -> InvoiceSettlementAmounts:
    """Resolve cash and credit-note effects without mixing their meanings."""
    # A partially-refunded payment still paid for most of this invoice. The
    # refund is attributed proportionally across allocations because one
    # payment may settle several invoices.
    allocation_rows = (
        db.query(
            PaymentAllocation.amount,
            Payment.amount.label("payment_amount"),
            Payment.refunded_amount,
            Payment.status,
        )
        .join(Payment, Payment.id == PaymentAllocation.payment_id)
        .filter(PaymentAllocation.invoice_id == invoice_id)
        .filter(PaymentAllocation.is_active.is_(True))
        .filter(Payment.is_active.is_(True))
        .filter(
            Payment.status.in_(
                [PaymentStatus.succeeded, PaymentStatus.partially_refunded]
            )
        )
        .all()
    )
    paid_amount = Decimal("0.00")
    for row in allocation_rows:
        allocated = to_decimal(row.amount)
        if row.status != PaymentStatus.partially_refunded:
            paid_amount += allocated
            continue
        gross = to_decimal(row.payment_amount)
        refunded = to_decimal(row.refunded_amount)
        if gross <= 0:
            continue
        paid_amount += allocated * (gross - refunded) / gross

    credit_amount = (
        db.query(func.coalesce(func.sum(CreditNoteApplication.amount), 0))
        .filter(CreditNoteApplication.invoice_id == invoice_id)
        .scalar()
    )
    return InvoiceSettlementAmounts(
        payments_applied=round_money(paid_amount),
        credits_applied=round_money(to_decimal(credit_amount)),
    )


def _validate_account(db: Session, account_id: str):
    """Validate that a subscriber exists."""
    account = get_by_id(db, Subscriber, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Subscriber account not found")
    return account


def get_account_credit_balance(
    db: Session, account_id: str, *, currency: str | None = "NGN"
) -> Decimal:
    """Calculate the credit balance for an account from ledger entries.

    Credit balance is calculated as:
    - Sum of credit entries with no invoice (unallocated payments)
    - Minus any debit entries for refunds/adjustments

    Args:
        db: Database session
        account_id: The account to check
        currency: Currency to read. Defaults to NGN so payment credit in one
            currency cannot accidentally cover debt in another. Pass ``None``
            only for explicit all-currency reporting, never enforcement.

    Returns:
        Credit balance (positive means customer has credit)
    """
    from app.services.common import coerce_uuid

    # Get unallocated credits (payments not applied to invoices)
    credit_query = (
        db.query(func.coalesce(func.sum(LedgerEntry.amount), Decimal("0.00")))
        .filter(LedgerEntry.account_id == coerce_uuid(account_id))
        .filter(LedgerEntry.invoice_id.is_(None))
        .filter(LedgerEntry.entry_type == LedgerEntryType.credit)
        .filter(LedgerEntry.is_active.is_(True))
    )
    if currency is not None:
        credit_query = credit_query.filter(LedgerEntry.currency == currency)
    credit_total = credit_query.scalar() or Decimal("0.00")

    # Get debits against unallocated credits (refunds)
    debit_query = (
        db.query(func.coalesce(func.sum(LedgerEntry.amount), Decimal("0.00")))
        .filter(LedgerEntry.account_id == coerce_uuid(account_id))
        .filter(LedgerEntry.invoice_id.is_(None))
        .filter(LedgerEntry.entry_type == LedgerEntryType.debit)
        .filter(LedgerEntry.is_active.is_(True))
        # Payment-linked refund rows are the exact accounting evidence for the
        # total refund. The payment document already reduces customer funding;
        # only the separate internal consumption debit affects unallocated
        # account credit. Counting both would double-debit the wallet.
        .filter(
            or_(
                LedgerEntry.source.is_(None),
                LedgerEntry.source.notin_([LedgerSource.refund, LedgerSource.payment]),
                LedgerEntry.payment_id.is_(None),
            )
        )
    )
    if currency is not None:
        debit_query = debit_query.filter(LedgerEntry.currency == currency)
    debit_total = debit_query.scalar() or Decimal("0.00")

    return round_money(to_decimal(credit_total) - to_decimal(debit_total))


def lock_account(db: Session, account_id: str) -> None:
    """Serialize wallet read-modify-write for one account so two writers can't
    both read the same credit balance and overspend it.

    Any code that reads ``get_account_credit_balance`` and then writes a debit
    based on it (plan change, add-on purchase, autopay) must call this first.
    Postgres takes a row lock on the subscriber; SQLite serializes writes
    globally so it is a no-op there.
    """
    from app.models.subscriber import Subscriber
    from app.services.common import coerce_uuid

    bind = db.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        db.query(Subscriber).filter(
            Subscriber.id == coerce_uuid(account_id)
        ).with_for_update().first()


def _validate_invoice_totals(data: dict):
    """Validate invoice monetary totals are consistent."""
    subtotal = data.get("subtotal")
    tax_total = data.get("tax_total")
    total = data.get("total")
    balance_due = data.get("balance_due")
    if subtotal is not None and subtotal < 0:
        raise HTTPException(status_code=400, detail="Subtotal must be non-negative")
    if tax_total is not None and tax_total < 0:
        raise HTTPException(status_code=400, detail="Tax total must be non-negative")
    if total is not None and total < 0:
        raise HTTPException(status_code=400, detail="Total must be non-negative")
    if balance_due is not None and balance_due < 0:
        raise HTTPException(status_code=400, detail="Balance due must be non-negative")
    if subtotal is not None and tax_total is not None and total is not None:
        if round_money(subtotal + tax_total) > round_money(total):
            raise HTTPException(
                status_code=400, detail="Total must cover subtotal and tax"
            )
    if balance_due is not None and total is not None and balance_due > total:
        raise HTTPException(status_code=400, detail="Balance due exceeds total")


def _validate_credit_note_totals(data: dict):
    """Validate credit note monetary totals are consistent."""
    subtotal = data.get("subtotal")
    tax_total = data.get("tax_total")
    total = data.get("total")
    applied_total = data.get("applied_total")
    if subtotal is not None and subtotal < 0:
        raise HTTPException(status_code=400, detail="Subtotal must be non-negative")
    if tax_total is not None and tax_total < 0:
        raise HTTPException(status_code=400, detail="Tax total must be non-negative")
    if total is not None and total < 0:
        raise HTTPException(status_code=400, detail="Total must be non-negative")
    if applied_total is not None and applied_total < 0:
        raise HTTPException(
            status_code=400, detail="Applied total must be non-negative"
        )
    if subtotal is not None and tax_total is not None and total is not None:
        if round_money(subtotal + tax_total) > round_money(total):
            raise HTTPException(
                status_code=400, detail="Total must cover subtotal and tax"
            )
    if applied_total is not None and total is not None and applied_total > total:
        raise HTTPException(status_code=400, detail="Applied total exceeds total")


def _validate_invoice_line_amount(
    quantity: Decimal, unit_price: Decimal, amount: Decimal | None
):
    """Validate and calculate invoice line amount."""
    if quantity <= 0:
        raise HTTPException(status_code=400, detail="Quantity must be greater than 0")
    if unit_price < 0:
        raise HTTPException(status_code=400, detail="Unit price must be non-negative")
    expected = round_money(quantity * unit_price)
    if amount is None:
        return expected
    if amount < 0:
        raise HTTPException(status_code=400, detail="Amount must be non-negative")
    if round_money(amount) != expected:
        raise HTTPException(
            status_code=400, detail="Amount must equal quantity * unit price"
        )
    return round_money(amount)


def _resolve_tax_rate(db: Session, tax_rate_id: str | None) -> TaxRate | None:
    """Look up a tax rate by ID."""
    if not tax_rate_id:
        return None
    rate = get_by_id(db, TaxRate, tax_rate_id)
    if not rate:
        raise HTTPException(status_code=404, detail="Tax rate not found")
    return rate


def _calculate_tax_amount(
    amount: Decimal,
    rate_percent: Decimal,
    tax_application: TaxApplication,
) -> Decimal:
    """Calculate the tax amount for a line.

    Inclusive tax extracts the tax portion from the gross amount. Exclusive tax
    calculates tax to add on top. Exempt lines carry no tax.
    """
    amount = round_money(to_decimal(amount))
    rate_percent = to_decimal(rate_percent)
    if tax_application == TaxApplication.exempt or rate_percent <= 0:
        return Decimal("0.00")
    if tax_application == TaxApplication.inclusive:
        return round_money(
            amount - (amount / (Decimal("1.00") + rate_percent / Decimal("100.00")))
        )
    return round_money(amount * rate_percent / Decimal("100.00"))


def _validate_invoice_currency(invoice: Invoice, currency: str | None):
    """Validate that currency matches the invoice."""
    if currency and invoice.currency != currency:
        raise HTTPException(status_code=400, detail="Currency does not match invoice")


# Allowed non-financial manual invoice status transitions (the raw
# ``Invoices.update`` / PATCH path). Payment settlement, void, and write-off
# transitions are absent: their named owners write exact evidence and update
# status atomically. Ledger-driven reopen after refund/reversal goes through
# ``_recalculate_invoice_totals``, not this table.
ALLOWED_INVOICE_TRANSITIONS: dict[InvoiceStatus, set[InvoiceStatus]] = {
    InvoiceStatus.draft: {InvoiceStatus.issued},
    InvoiceStatus.issued: {InvoiceStatus.overdue},
    InvoiceStatus.partially_paid: {
        InvoiceStatus.overdue,
        InvoiceStatus.issued,
    },
    InvoiceStatus.overdue: {InvoiceStatus.issued},
    InvoiceStatus.paid: set(),
    InvoiceStatus.void: set(),
    # Bad debt is closed; no transitions out (the ledger records any recovery).
    InvoiceStatus.written_off: set(),
}


def assert_legal_invoice_transition(
    from_status: InvoiceStatus | None, to_status: InvoiceStatus | None
) -> None:
    """Reject an illegal manual invoice status transition (409)."""
    if to_status is None or from_status is None or from_status == to_status:
        return
    if to_status not in ALLOWED_INVOICE_TRANSITIONS.get(from_status, set()):
        raise HTTPException(
            status_code=409,
            detail=(
                f"Illegal invoice transition {from_status.value} → {to_status.value}"
            ),
        )


def _assert_invoice_allocatable(invoice: Invoice) -> None:
    """Reject allocating a payment to a void or draft invoice.

    A void invoice is terminal (settling money against it would resurrect it to
    'paid' — see ``_recalculate_invoice_totals``); a draft is pre-issue and not
    yet a payable obligation. Both must refuse allocations.
    """
    if invoice.status in (InvoiceStatus.void, InvoiceStatus.draft):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot allocate a payment to a {invoice.status.value} invoice",
        )


def _recalculate_invoice_totals(db: Session, invoice: Invoice):
    """Recalculate invoice totals from lines and payments."""
    # Serialize concurrent recalculation of the same invoice. The summary
    # fields written below (subtotal/tax_total/total/balance_due/status) are
    # denormalized from SUM() aggregates over lines, payment allocations and
    # credit applications. Those feeder rows can be inserted by several
    # requests/workers at once (e.g. two payments allocated to one invoice);
    # under READ COMMITTED each recalc reads a snapshot that misses a sibling's
    # not-yet-committed rows, so the last writer leaves balance_due/status
    # stale. Taking a row lock here forces the read-modify-write of the summary
    # to run serially per invoice, so the final committer always recomputes
    # from the full committed set. Held until the caller's commit. Real lock on
    # PostgreSQL; a harmless no-op on SQLite (tests) and for a not-yet-flushed
    # new invoice (which has no concurrency).
    lock_for_update(db, Invoice, invoice.id)
    lines = (
        db.query(InvoiceLine)
        .filter(InvoiceLine.invoice_id == invoice.id)
        .filter(InvoiceLine.is_active.is_(True))
        .all()
    )
    if not lines:
        total_lines = (
            db.query(func.count(InvoiceLine.id))
            .filter(InvoiceLine.invoice_id == invoice.id)
            .scalar()
        )
        if total_lines == 0:
            subtotal = round_money(to_decimal(invoice.subtotal))
            tax_total = round_money(to_decimal(invoice.tax_total))
            total = round_money(to_decimal(invoice.total, default=subtotal + tax_total))
            invoice.subtotal = subtotal
            invoice.tax_total = tax_total
            invoice.total = total
        else:
            invoice.subtotal = Decimal("0.00")
            invoice.tax_total = Decimal("0.00")
            invoice.total = Decimal("0.00")
    else:
        # Pre-fetch all tax rates used by lines to avoid N+1 queries
        tax_rate_ids = {line.tax_rate_id for line in lines if line.tax_rate_id}
        tax_rates_map = {}
        if tax_rate_ids:
            tax_rates = db.query(TaxRate).filter(TaxRate.id.in_(tax_rate_ids)).all()
            tax_rates_map = {rate.id: rate for rate in tax_rates}

        subtotal = Decimal("0.00")
        tax_total = Decimal("0.00")
        for line in lines:
            amount = round_money(line.amount)
            if line.tax_rate_id:
                rate = tax_rates_map.get(line.tax_rate_id)
                if rate:
                    rate_percent = to_decimal(rate.rate)
                    if line.tax_application == TaxApplication.inclusive:
                        # Inclusive: amount already contains tax — extract it so
                        # the gross stays the customer-facing total
                        # (subtotal + tax == gross). Mirrors the credit-note path.
                        tax_amount = _calculate_tax_amount(
                            amount, rate_percent, line.tax_application
                        )
                        subtotal += round_money(amount - tax_amount)
                        tax_total += tax_amount
                    elif line.tax_application == TaxApplication.exempt:
                        subtotal += amount
                    else:
                        # Exclusive: tax is added on top
                        tax_amount = _calculate_tax_amount(
                            amount, rate_percent, line.tax_application
                        )
                        subtotal += amount
                        tax_total += tax_amount
                else:
                    subtotal += amount
            else:
                subtotal += amount
        subtotal = round_money(subtotal)
        tax_total = round_money(tax_total)
        invoice.subtotal = subtotal
        invoice.tax_total = tax_total
        invoice.total = round_money(subtotal + tax_total)

    settlement = resolve_invoice_settlement_amounts(db, invoice.id)
    # Void and written_off are terminal: never recompute their balance or
    # derive a status — both carry balance_due 0, and the paid-branch below
    # would otherwise resurrect them to 'paid' (the void→paid bug; the same
    # would silently turn a bad-debt write-off into a cash payment). Leave them
    # exactly as set.
    if invoice.status in (InvoiceStatus.void, InvoiceStatus.written_off):
        return
    invoice.balance_due = max(
        Decimal("0.00"),
        round_money(
            invoice.total - settlement.payments_applied - settlement.credits_applied
        ),
    )
    # A draft is pre-issue: keep its computed balance for display but do NOT
    # auto-advance it to paid/partially_paid (it must be explicitly issued).
    if invoice.status == InvoiceStatus.draft:
        return
    if invoice.balance_due <= 0:
        invoice.status = InvoiceStatus.paid
        if not invoice.paid_at:
            invoice.paid_at = datetime.now(UTC)
    elif settlement.payments_applied > 0 or settlement.credits_applied > 0:
        invoice.status = InvoiceStatus.partially_paid
    elif invoice.status in (InvoiceStatus.paid, InvoiceStatus.partially_paid):
        # The invoice is fully unpaid again (e.g. its payment was refunded or
        # removed). Without this branch the status stayed a stale 'paid' while
        # balance_due rose back above zero. Revert to overdue/issued; leave
        # draft and void invoices untouched.
        invoice.paid_at = None
        now = datetime.now(UTC)
        due_at = invoice.due_at
        if due_at is not None and due_at.tzinfo is None:
            due_at = due_at.replace(tzinfo=UTC)
        invoice.status = (
            InvoiceStatus.overdue if due_at and due_at < now else InvoiceStatus.issued
        )


def _validate_payment_channel(
    db: Session, channel_id: str | None
) -> PaymentChannel | None:
    if not channel_id:
        return None
    channel = get_by_id(db, PaymentChannel, channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="Payment channel not found")
    if not channel.is_active:
        raise HTTPException(status_code=400, detail="Payment channel is inactive")
    return channel


def _validate_collection_account(
    db: Session, collection_account_id: str | None, currency: str | None
) -> CollectionAccount | None:
    if not collection_account_id:
        return None
    account = get_by_id(db, CollectionAccount, collection_account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Collection account not found")
    if not account.is_active:
        raise HTTPException(status_code=400, detail="Collection account is inactive")
    if currency and account.currency != currency:
        raise HTTPException(
            status_code=400, detail="Collection account currency does not match payment"
        )
    return account


def _resolve_payment_channel(
    db: Session,
    payment_channel_id: str | None,
    payment_method_id: str | None,
    provider_id: str | None,
) -> PaymentChannel | None:
    if payment_channel_id:
        return _validate_payment_channel(db, payment_channel_id)
    if payment_method_id:
        method = get_by_id(db, PaymentMethod, payment_method_id)
        if method and method.payment_channel_id:
            return _validate_payment_channel(db, str(method.payment_channel_id))
    if provider_id:
        query = (
            db.query(PaymentChannel)
            .filter(PaymentChannel.provider_id == provider_id)
            .filter(PaymentChannel.is_active.is_(True))
        )
        channel = query.filter(PaymentChannel.is_default.is_(True)).first()
        if channel:
            return channel
        candidates = query.all()
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            raise HTTPException(
                status_code=400,
                detail="Multiple payment channels match provider; set a default",
            )
    return None


def _resolve_collection_account(
    db: Session,
    channel: PaymentChannel | None,
    currency: str | None,
    collection_account_id: str | None,
) -> CollectionAccount | None:
    if collection_account_id:
        return _validate_collection_account(db, collection_account_id, currency)
    if not channel:
        return None
    account_query = (
        db.query(PaymentChannelAccount)
        .filter(PaymentChannelAccount.channel_id == channel.id)
        .filter(PaymentChannelAccount.is_active.is_(True))
    )
    if currency:
        exact = (
            account_query.filter(PaymentChannelAccount.currency == currency)
            .order_by(
                PaymentChannelAccount.is_default.desc(),
                PaymentChannelAccount.priority.desc(),
            )
            .first()
        )
        if exact:
            return _validate_collection_account(
                db, str(exact.collection_account_id), currency
            )
    fallback = (
        account_query.filter(PaymentChannelAccount.currency.is_(None))
        .order_by(
            PaymentChannelAccount.is_default.desc(),
            PaymentChannelAccount.priority.desc(),
        )
        .first()
    )
    if fallback:
        return _validate_collection_account(
            db, str(fallback.collection_account_id), currency
        )
    return None


def _recalculate_credit_note_totals(db: Session, credit_note: CreditNote):
    """Recalculate credit note totals from lines and applications."""
    lines = (
        db.query(CreditNoteLine)
        .filter(CreditNoteLine.credit_note_id == credit_note.id)
        .filter(CreditNoteLine.is_active.is_(True))
        .all()
    )
    if lines:
        # Pre-fetch all tax rates used by lines to avoid N+1 queries
        tax_rate_ids = {line.tax_rate_id for line in lines if line.tax_rate_id}
        tax_rates_map = {}
        if tax_rate_ids:
            tax_rates = db.query(TaxRate).filter(TaxRate.id.in_(tax_rate_ids)).all()
            tax_rates_map = {rate.id: rate for rate in tax_rates}

        subtotal = Decimal("0.00")
        tax_total = Decimal("0.00")
        for line in lines:
            amount = round_money(line.amount)
            if line.tax_rate_id:
                rate = tax_rates_map.get(line.tax_rate_id)
                if rate:
                    rate_percent = to_decimal(rate.rate)
                    if line.tax_application == TaxApplication.inclusive:
                        # Inclusive: amount already contains tax — extract it
                        tax_amount = _calculate_tax_amount(
                            amount, rate_percent, line.tax_application
                        )
                        subtotal += round_money(amount - tax_amount)
                        tax_total += tax_amount
                    elif line.tax_application == TaxApplication.exempt:
                        subtotal += amount
                    else:
                        # Exclusive: tax is added on top
                        tax_amount = _calculate_tax_amount(
                            amount, rate_percent, line.tax_application
                        )
                        subtotal += amount
                        tax_total += tax_amount
                else:
                    subtotal += amount
            else:
                subtotal += amount
        subtotal = round_money(subtotal)
        tax_total = round_money(tax_total)
        credit_note.subtotal = subtotal
        credit_note.tax_total = tax_total
        credit_note.total = round_money(subtotal + tax_total)
    else:
        credit_note.subtotal = round_money(to_decimal(credit_note.subtotal))
        credit_note.tax_total = round_money(to_decimal(credit_note.tax_total))
        credit_note.total = round_money(to_decimal(credit_note.total))
    applied_total = (
        db.query(func.coalesce(func.sum(CreditNoteApplication.amount), 0))
        .filter(CreditNoteApplication.credit_note_id == credit_note.id)
        .scalar()
    )
    applied_total = round_money(to_decimal(applied_total))
    credit_note.applied_total = applied_total
    if applied_total > credit_note.total:
        raise HTTPException(
            status_code=400, detail="Applied total exceeds credit note total"
        )
    if credit_note.status not in {CreditNoteStatus.draft, CreditNoteStatus.void}:
        if credit_note.issued_at is None:
            credit_note.issued_at = datetime.now(UTC)
        if applied_total <= 0:
            credit_note.status = CreditNoteStatus.issued
        elif applied_total < credit_note.total:
            credit_note.status = CreditNoteStatus.partially_applied
        else:
            credit_note.status = CreditNoteStatus.applied


def _validate_payment_linkages(
    db: Session, account_id: str, invoice_id: str | None, payment_method_id: str | None
):
    """Validate payment relationships to account, invoice, and method."""
    _validate_account(db, account_id)
    if invoice_id:
        invoice = get_by_id(db, Invoice, invoice_id)
        if not invoice:
            raise HTTPException(status_code=404, detail="Invoice not found")
        if str(invoice.account_id) != account_id:
            raise HTTPException(
                status_code=400, detail="Invoice does not belong to account"
            )
    if payment_method_id:
        method = get_by_id(db, PaymentMethod, payment_method_id)
        if not method:
            raise HTTPException(status_code=404, detail="Payment method not found")
        if str(method.account_id) != account_id:
            raise HTTPException(
                status_code=400, detail="Payment method does not belong to account"
            )


def _validate_payment_provider(db: Session, provider_id: str | None):
    """Validate that a payment provider exists and is active."""
    if not provider_id:
        return None
    try:
        provider = get_by_id(db, PaymentProvider, provider_id)
    except ValueError:
        # Invalid UUID format (e.g., "provider-1" instead of a UUID)
        raise HTTPException(
            status_code=400,
            detail=f"Invalid provider_id format: {provider_id!r} is not a valid UUID",
        )
    if not provider or not provider.is_active:
        raise HTTPException(status_code=404, detail="Payment provider not found")
    return provider


def _validate_ledger_linkages(
    db: Session, account_id: str, invoice_id: str | None, payment_id: str | None
):
    """Validate ledger entry relationships to account, invoice, and payment."""
    _validate_account(db, account_id)
    if invoice_id:
        invoice = get_by_id(db, Invoice, invoice_id)
        if not invoice:
            raise HTTPException(status_code=404, detail="Invoice not found")
        if str(invoice.account_id) != account_id:
            raise HTTPException(
                status_code=400, detail="Invoice does not belong to account"
            )
    if payment_id:
        payment = get_by_id(db, Payment, payment_id)
        if not payment:
            raise HTTPException(status_code=404, detail="Payment not found")
        if str(payment.account_id) != account_id:
            raise HTTPException(
                status_code=400, detail="Payment does not belong to account"
            )
