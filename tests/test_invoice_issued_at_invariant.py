"""An invoice past draft must carry the date that makes "issued" true.

`Invoices.create_for_subscription` built `status=issued` by hand and never set
`issued_at` or `due_at`. Settlement then advanced those invoices to paid, so
they were paid without ever having been issued — no date to age them by, unable
to go overdue, invisible to anything filtering on issue date. Twelve reached
that state in production.

The fix is at the source. Settlement deliberately does *not* refuse to advance
them: leaving money applied against a zero balance while the status still reads
issued would turn a missing date into an open receivable that collections keeps
chasing. The class is surfaced by billing health instead.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.models.billing import Invoice, InvoiceStatus
from app.models.catalog import (
    BillingCycle,
    BillingMode,
    OfferPrice,
    PriceType,
    SubscriptionStatus,
)
from app.models.subscriber import AccountStatus
from app.services.billing.invoices import Invoices
from app.services.billing_health import invoices_past_draft_without_issue_date


def _invoice(db_session, subscriber, *, status, issued_at) -> Invoice:
    invoice = Invoice(
        account_id=subscriber.id,
        status=status,
        currency="NGN",
        subtotal=Decimal("5000.00"),
        tax_total=Decimal("0.00"),
        total=Decimal("5000.00"),
        balance_due=Decimal("0.00"),
        issued_at=issued_at,
    )
    db_session.add(invoice)
    db_session.commit()
    return invoice


def test_create_for_subscription_issues_with_both_dates(
    db_session, subscription, subscriber_account
):
    """The path that produced the twelve.

    It builds `status=issued` directly rather than going through
    `issue_draft_system`, so it must set the same dates that owner would.
    """
    db_session.add(
        OfferPrice(
            offer_id=subscription.offer_id,
            price_type=PriceType.recurring,
            amount=Decimal("17500.00"),
            currency="NGN",
            billing_cycle=BillingCycle.monthly,
            is_active=True,
        )
    )
    subscription.status = SubscriptionStatus.active
    subscription.billing_mode = BillingMode.postpaid
    subscriber_account.status = AccountStatus.active
    db_session.commit()

    invoice = Invoices.create_for_subscription(
        db_session, str(subscriber_account.id), str(subscription.id)
    )

    assert invoice.status == InvoiceStatus.issued
    assert invoice.issued_at is not None
    # Without a due date the invoice can never age or go overdue.
    assert invoice.due_at is not None
    assert invoice.due_at > invoice.issued_at


def test_health_counts_an_invoice_that_left_draft_without_an_issue_date(
    db_session, subscriber
):
    _invoice(db_session, subscriber, status=InvoiceStatus.paid, issued_at=None)

    assert invoices_past_draft_without_issue_date(db_session) == 1


def test_health_ignores_a_properly_issued_invoice(db_session, subscriber):
    _invoice(
        db_session,
        subscriber,
        status=InvoiceStatus.paid,
        issued_at=datetime(2026, 6, 16, tzinfo=UTC),
    )

    assert invoices_past_draft_without_issue_date(db_session) == 0


def test_health_ignores_drafts_and_voids(db_session, subscriber):
    """A draft is pre-issue by definition, and void is terminal from any state."""
    _invoice(db_session, subscriber, status=InvoiceStatus.draft, issued_at=None)
    _invoice(db_session, subscriber, status=InvoiceStatus.void, issued_at=None)

    assert invoices_past_draft_without_issue_date(db_session) == 0
