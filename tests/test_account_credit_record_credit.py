"""Minting account credit and offering it are one command.

The account-credit owner had a consume half and a read half but no creation
half, so credit was minted through the generic ledger writer and the owner never
learned it existed. Nothing then offered it to the account's open invoices, and
the account was dunned on a receivable it had already funded.

These tests pin the two halves together: after ``record_credit`` there is no
state in which payment-backed credit exists and an eligible invoice is still
payable against it.
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
    PaymentStatus,
)
from app.services.billing._common import get_account_credit_balance
from app.services.billing.account_credit import AccountCreditApplications


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


def _unallocated_credit_entries(db_session, subscriber) -> list[LedgerEntry]:
    return (
        db_session.query(LedgerEntry)
        .filter(LedgerEntry.account_id == subscriber.id)
        .filter(LedgerEntry.entry_type == LedgerEntryType.credit)
        .filter(LedgerEntry.invoice_id.is_(None))
        .all()
    )


def test_payment_credit_is_offered_to_an_open_invoice_as_it_is_minted(
    db_session, subscriber
):
    invoice = _invoice(db_session, subscriber, "8000.00")
    payment = _payment(db_session, subscriber, "8000.00")

    result = AccountCreditApplications.record_credit(
        db_session,
        str(subscriber.id),
        amount=Decimal("8000.00"),
        currency="NGN",
        source=LedgerSource.payment,
        memo=f"Payment {payment.id}",
        payment_id=payment.id,
    )

    assert result.offered is True
    assert result.ledger_entry is not None
    assert result.applied == Decimal("8000.00")

    db_session.refresh(invoice)
    assert invoice.status == InvoiceStatus.paid
    assert invoice.balance_due == Decimal("0.00")
    # The whole point: no spendable credit is left sitting against an account
    # that was, a moment ago, carrying a payable invoice.
    assert get_account_credit_balance(
        db_session, str(subscriber.id), currency="NGN"
    ) == Decimal("0.00")


def test_surplus_beyond_the_invoice_stays_as_credit(db_session, subscriber):
    """Offering is not over-applying — only the payable amount is consumed."""
    invoice = _invoice(db_session, subscriber, "3000.00")
    payment = _payment(db_session, subscriber, "5000.00")

    result = AccountCreditApplications.record_credit(
        db_session,
        str(subscriber.id),
        amount=Decimal("5000.00"),
        currency="NGN",
        source=LedgerSource.payment,
        payment_id=payment.id,
    )

    assert result.applied == Decimal("3000.00")
    db_session.refresh(invoice)
    assert invoice.status == InvoiceStatus.paid
    assert get_account_credit_balance(
        db_session, str(subscriber.id), currency="NGN"
    ) == Decimal("2000.00")


def test_credit_with_no_payable_invoice_is_simply_held(db_session, subscriber):
    payment = _payment(db_session, subscriber, "4000.00")

    result = AccountCreditApplications.record_credit(
        db_session,
        str(subscriber.id),
        amount=Decimal("4000.00"),
        currency="NGN",
        source=LedgerSource.payment,
        payment_id=payment.id,
    )

    assert result.offered is True
    assert result.applied == Decimal("0.00")
    assert get_account_credit_balance(
        db_session, str(subscriber.id), currency="NGN"
    ) == Decimal("4000.00")


def test_non_payment_credit_is_minted_and_reports_that_it_was_not_offered(
    db_session, subscriber
):
    """Credit-note credit is a different instrument, and says so.

    `apply` settles by composing PaymentAllocations against succeeded payments,
    so there is nothing to allocate credit-note credit from. Reporting
    ``offered=False`` keeps that visible instead of leaving a caller to assume
    the offer happened.
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

    assert result.offered is False
    assert result.application is None
    assert result.ledger_entry is not None
    assert len(_unallocated_credit_entries(db_session, subscriber)) == 1


def test_zero_or_negative_amount_writes_nothing(db_session, subscriber):
    for amount in (Decimal("0.00"), Decimal("-500.00")):
        result = AccountCreditApplications.record_credit(
            db_session,
            str(subscriber.id),
            amount=amount,
            currency="NGN",
            source=LedgerSource.payment,
        )
        assert result.ledger_entry is None
        assert result.offered is False

    assert _unallocated_credit_entries(db_session, subscriber) == []


def test_the_command_does_not_commit(db_session, subscriber):
    """The caller owns the boundary so money and consequence land together."""
    payment = _payment(db_session, subscriber, "1000.00")

    AccountCreditApplications.record_credit(
        db_session,
        str(subscriber.id),
        amount=Decimal("1000.00"),
        currency="NGN",
        source=LedgerSource.payment,
        payment_id=payment.id,
    )
    db_session.rollback()

    assert _unallocated_credit_entries(db_session, subscriber) == []
