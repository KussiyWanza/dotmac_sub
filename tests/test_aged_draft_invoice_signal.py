"""Drafts nobody came back to are reported, never acted on.

A draft is a legitimate holding state — awaiting confirmation, provisioning, or
a quote nobody accepted. The data cannot distinguish a deliberate hold from an
abandoned one, so this signal reports and stops there. Auto-issuing would send
bills nobody meant to send.

What it prevents is the third case: a hand-made invoice for over a million naira
sat unissued for days because nothing counted drafts, so nothing could notice.
Issue it, void it, or keep holding it are all fine. Not knowing it exists is not.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models.billing import Invoice, InvoiceStatus
from app.services.billing_health import (
    AGED_DRAFT_DAYS,
    STALLED_DRAFT_ALERT_COUNT,
    aged_draft_invoices,
    stalled_draft_invoice_cohort,
)

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)


def _draft(db_session, subscriber, *, age_days: int, total: str = "1000.00", **kw):
    invoice = Invoice(
        account_id=subscriber.id,
        status=kw.pop("status", InvoiceStatus.draft),
        currency="NGN",
        subtotal=Decimal(total),
        tax_total=Decimal("0.00"),
        total=Decimal(total),
        balance_due=Decimal(total),
        created_at=NOW - timedelta(days=age_days),
        **kw,
    )
    db_session.add(invoice)
    db_session.commit()
    return invoice


def test_an_aged_draft_is_counted_with_its_value(db_session, subscriber):
    _draft(db_session, subscriber, age_days=AGED_DRAFT_DAYS + 1, total="1213406.25")

    count, total = aged_draft_invoices(db_session, now=NOW)

    assert count == 1
    assert total == Decimal("1213406.25")


def test_a_recent_draft_is_not_counted(db_session, subscriber):
    """Drafts are meant to exist; only forgotten ones are worth surfacing."""
    _draft(db_session, subscriber, age_days=AGED_DRAFT_DAYS - 1)

    count, total = aged_draft_invoices(db_session, now=NOW)

    assert count == 0
    assert total == Decimal("0")


def test_issued_and_paid_invoices_are_not_drafts(db_session, subscriber):
    _draft(
        db_session,
        subscriber,
        age_days=AGED_DRAFT_DAYS + 10,
        status=InvoiceStatus.issued,
    )
    _draft(
        db_session, subscriber, age_days=AGED_DRAFT_DAYS + 10, status=InvoiceStatus.paid
    )

    count, _ = aged_draft_invoices(db_session, now=NOW)

    assert count == 0


def test_a_zero_total_draft_is_ignored(db_session, subscriber):
    """Nothing is owed, so nobody is waiting on it."""
    _draft(db_session, subscriber, age_days=AGED_DRAFT_DAYS + 10, total="0.00")

    count, _ = aged_draft_invoices(db_session, now=NOW)

    assert count == 0


def test_the_signal_can_reach_zero(db_session, subscriber):
    """Voiding is as valid a disposition as issuing — both clear the signal."""
    invoice = _draft(db_session, subscriber, age_days=AGED_DRAFT_DAYS + 5)
    assert aged_draft_invoices(db_session, now=NOW)[0] == 1

    invoice.status = InvoiceStatus.void
    db_session.commit()

    assert aged_draft_invoices(db_session, now=NOW)[0] == 0


def test_recent_stalled_cohort_uses_a_fixed_creation_window(db_session, subscriber):
    current_cohort = _draft(db_session, subscriber, age_days=1, total="1250.00")
    current_cohort.created_at = NOW - timedelta(hours=30)
    db_session.commit()
    _draft(db_session, subscriber, age_days=3, total="2500.00")
    _draft(db_session, subscriber, age_days=AGED_DRAFT_DAYS + 10, total="9999.00")

    count, total = stalled_draft_invoice_cohort(db_session, now=NOW)

    assert count == 1
    assert total == Decimal("1250.00")


def test_historic_aged_stock_is_not_a_current_incident(db_session, subscriber):
    for _ in range(STALLED_DRAFT_ALERT_COUNT + 1):
        _draft(db_session, subscriber, age_days=AGED_DRAFT_DAYS + 10)

    count, total = stalled_draft_invoice_cohort(db_session, now=NOW)

    assert count == 0
    assert total == Decimal("0")
