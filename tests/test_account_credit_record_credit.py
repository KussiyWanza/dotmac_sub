"""Minting account credit and offering it are one owner's business.

The account-credit owner had a consume half and a read half but no creation
half, so credit was minted through the generic ledger writer and the owner never
learned it existed. Nothing then offered it to the account's open invoices, and
the account was dunned on a receivable it had already funded.

The two halves are split across the settlement boundary, and that split is the
subtle part: credit is spendable only once its ``PaymentSettlement`` exists.
``PaymentAllocations.available_amount`` returns zero before then, so an offer at
mint time finds nothing backed and applies nothing while looking like success.
These tests pin both the minting door and the timing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.models.billing import (
    Invoice,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentSettlement,
    PaymentSettlementOrigin,
    PaymentStatus,
)
from app.services.billing._common import get_account_credit_balance
from app.services.billing.account_credit import AccountCreditApplications
from app.services.billing.payments import _offer_settled_account_credit


def _payment(db_session, subscriber, amount: str) -> Payment:
    payment = Payment(
        account_id=subscriber.id,
        amount=Decimal(amount),
        currency="NGN",
        status=PaymentStatus.succeeded,
        paid_at=datetime.now(UTC),
    )
    db_session.add(payment)
    db_session.flush()
    return payment


def _invoice(db_session, subscriber, total: str) -> Invoice:
    invoice = Invoice(
        account_id=subscriber.id,
        status=InvoiceStatus.issued,
        currency="NGN",
        total=Decimal(total),
        balance_due=Decimal(total),
    )
    db_session.add(invoice)
    db_session.flush()
    return invoice


def _settle(db_session, payment: Payment, entry: LedgerEntry) -> PaymentSettlement:
    """The evidence that makes the credit spendable."""
    settlement = PaymentSettlement(
        payment_id=payment.id,
        unallocated_ledger_entry_id=entry.id,
        amount=payment.amount,
        unallocated_amount=payment.amount,
        prepaid_amount=Decimal("0.00"),
        currency=payment.currency,
        origin=PaymentSettlementOrigin.system,
        preview_fingerprint=f"test-{payment.id}",
    )
    db_session.add(settlement)
    db_session.flush()
    return settlement


def _mint(db_session, subscriber, payment, amount: str):
    return AccountCreditApplications.record_credit(
        db_session,
        str(subscriber.id),
        amount=Decimal(amount),
        currency="NGN",
        source=LedgerSource.payment,
        memo=f"Payment {payment.id}",
        payment_id=payment.id,
    )


def _unallocated_credit_entries(db_session, subscriber) -> list[LedgerEntry]:
    return (
        db_session.query(LedgerEntry)
        .filter(LedgerEntry.account_id == subscriber.id)
        .filter(LedgerEntry.entry_type == LedgerEntryType.credit)
        .filter(LedgerEntry.invoice_id.is_(None))
        .all()
    )


def test_settled_credit_is_offered_to_an_open_invoice(db_session, subscriber):
    invoice = _invoice(db_session, subscriber, "8000.00")
    payment = _payment(db_session, subscriber, "8000.00")
    record = _mint(db_session, subscriber, payment, "8000.00")
    _settle(db_session, payment, record.ledger_entry)

    result = AccountCreditApplications.offer_available_credit(
        db_session, str(subscriber.id), payments=(payment,)
    )

    assert result.applied == Decimal("8000.00")
    db_session.refresh(invoice)
    assert invoice.status == InvoiceStatus.paid
    assert invoice.balance_due == Decimal("0.00")
    # The whole point: no spendable credit is left sitting against an account
    # that was, a moment ago, carrying a payable invoice.
    assert get_account_credit_balance(
        db_session, str(subscriber.id), currency="NGN"
    ) == Decimal("0.00")


def test_credit_is_not_spendable_before_its_settlement_exists(db_session, subscriber):
    """Offering too early is a silent no-op, which is why the halves are split.

    This is the failure the first version of this change shipped: the offer ran
    at mint time, found the credit unbacked because no settlement row existed
    yet, and applied nothing while reporting success.
    """
    invoice = _invoice(db_session, subscriber, "8000.00")
    payment = _payment(db_session, subscriber, "8000.00")
    _mint(db_session, subscriber, payment, "8000.00")

    result = AccountCreditApplications.offer_available_credit(
        db_session, str(subscriber.id), payments=(payment,)
    )

    assert result.applied == Decimal("0.00")
    assert result.unbacked_credit == Decimal("8000.00")
    db_session.refresh(invoice)
    assert invoice.status == InvoiceStatus.issued


def test_a_stale_settlement_read_does_not_suppress_the_offer(db_session, subscriber):
    """Reading `payment.settlement` before the row exists must not poison it.

    SQLAlchemy caches the absent relationship on the instance, so a later
    correctly-timed offer would read the stale ``None``, treat the credit as
    unbacked, and apply nothing. That broke the deposit settlement path.
    """
    invoice = _invoice(db_session, subscriber, "5000.00")
    payment = _payment(db_session, subscriber, "5000.00")
    record = _mint(db_session, subscriber, payment, "5000.00")

    assert payment.settlement is None  # the poisoning read
    _settle(db_session, payment, record.ledger_entry)

    result = AccountCreditApplications.offer_available_credit(
        db_session, str(subscriber.id), payments=(payment,)
    )

    assert result.applied == Decimal("5000.00")
    db_session.refresh(invoice)
    assert invoice.status == InvoiceStatus.paid


def test_surplus_beyond_the_invoice_stays_as_credit(db_session, subscriber):
    """Offering is not over-applying — only the payable amount is consumed."""
    invoice = _invoice(db_session, subscriber, "3000.00")
    payment = _payment(db_session, subscriber, "5000.00")
    record = _mint(db_session, subscriber, payment, "5000.00")
    _settle(db_session, payment, record.ledger_entry)

    result = AccountCreditApplications.offer_available_credit(
        db_session, str(subscriber.id), payments=(payment,)
    )

    assert result.applied == Decimal("3000.00")
    db_session.refresh(invoice)
    assert invoice.status == InvoiceStatus.paid
    assert get_account_credit_balance(
        db_session, str(subscriber.id), currency="NGN"
    ) == Decimal("2000.00")


def test_credit_with_no_payable_invoice_is_simply_held(db_session, subscriber):
    payment = _payment(db_session, subscriber, "4000.00")
    record = _mint(db_session, subscriber, payment, "4000.00")
    _settle(db_session, payment, record.ledger_entry)

    result = AccountCreditApplications.offer_available_credit(
        db_session, str(subscriber.id), payments=(payment,)
    )

    assert result.applied == Decimal("0.00")
    assert get_account_credit_balance(
        db_session, str(subscriber.id), currency="NGN"
    ) == Decimal("4000.00")


def test_non_payment_credit_is_minted_and_owes_no_offer(db_session, subscriber):
    """Credit-note credit is a different instrument, and says so.

    `apply` settles by composing PaymentAllocations against succeeded payments,
    so there is nothing to allocate credit-note credit from. Reporting
    ``offer_pending=False`` keeps that visible instead of leaving a caller to
    assume an offer is coming.
    """
    _invoice(db_session, subscriber, "6000.00")

    result = AccountCreditApplications.record_credit(
        db_session,
        str(subscriber.id),
        amount=Decimal("6000.00"),
        currency="NGN",
        source=LedgerSource.credit_note,
        memo="Service rebate",
    )

    assert result.offer_pending is False
    assert result.ledger_entry is not None
    assert len(_unallocated_credit_entries(db_session, subscriber)) == 1


def test_payment_credit_reports_that_an_offer_is_owed(db_session, subscriber):
    payment = _payment(db_session, subscriber, "1000.00")
    assert _mint(db_session, subscriber, payment, "1000.00").offer_pending is True


def test_zero_or_negative_amount_writes_nothing(db_session, subscriber):
    payment = _payment(db_session, subscriber, "1000.00")
    for amount in ("0.00", "-500.00"):
        result = _mint(db_session, subscriber, payment, amount)
        assert result.ledger_entry is None
        assert result.offer_pending is False

    assert _unallocated_credit_entries(db_session, subscriber) == []


def test_minting_does_not_commit(db_session, subscriber):
    """The caller owns the boundary so money and consequence land together."""
    payment = _payment(db_session, subscriber, "1000.00")
    _mint(db_session, subscriber, payment, "1000.00")

    # Staged, not committed: the entry is visible in this transaction and the
    # transaction is still open for the caller to commit or roll back.
    assert len(_unallocated_credit_entries(db_session, subscriber)) == 1
    assert db_session.in_transaction()


def test_an_explicit_hold_is_respected(db_session, subscriber):
    """`auto_allocate_on_settlement=False` means hold it, not forgot to apply it.

    Verifying a payment proof with auto_allocate=False, and the provider
    settlement path that runs its own application afterwards, both set this.
    Offering anyway would override a decision somebody made on purpose.
    """
    invoice = _invoice(db_session, subscriber, "3000.00")
    payment = _payment(db_session, subscriber, "5000.00")
    payment.auto_allocate_on_settlement = False
    db_session.flush()
    record = _mint(db_session, subscriber, payment, "5000.00")
    settlement = _settle(db_session, payment, record.ledger_entry)

    _offer_settled_account_credit(db_session, payment, settlement)

    db_session.refresh(invoice)
    assert invoice.status == InvoiceStatus.issued
    assert get_account_credit_balance(
        db_session, str(subscriber.id), currency="NGN"
    ) == Decimal("5000.00")
