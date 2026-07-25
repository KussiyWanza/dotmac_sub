"""Regression: the dry-run billing snapshot leaves no changes at all.

``run_invoice_cycle(dry_run=True)`` neither commits nor dirties ORM objects: the
``next_billing_at`` fast-forward is now guarded behind ``if not dry_run``, so a
dry run is fully side-effect-free in the session rather than relying on a
follow-up ``db.rollback()`` to discard in-session dirt. This test asserts a
past-due subscription's ``next_billing_at`` is untouched by the dry run, both
in-session and after reloading from the database.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import patch

from app.models.catalog import (
    BillingCycle,
    BillingMode,
    Subscription,
    SubscriptionStatus,
)
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services.billing_automation import run_invoice_cycle


def test_dry_run_then_rollback_restores_next_billing_at(db_session, catalog_offer):
    past = datetime(2020, 1, 1, tzinfo=UTC)
    subscriber = Subscriber(
        first_name="D",
        last_name="R",
        email="dryrun@e.com",
        status=SubscriberStatus.active,
    )
    db_session.add(subscriber)
    db_session.flush()
    sub = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=SubscriptionStatus.active,
        # Postpaid: prepaid is excluded from invoice generation, so this test
        # of the cycle's in-session fast-forward must use a postpaid sub.
        billing_mode=BillingMode.postpaid,
        next_billing_at=past,
        start_at=past,
    )
    db_session.add(sub)
    db_session.commit()
    original = sub.next_billing_at

    # Force a resolvable price so the past-due sub reaches the fast-forward path
    # (billing_automation.py:855) — we are testing the rollback contract, not
    # price resolution.
    with patch(
        "app.services.billing_automation._resolve_price",
        return_value=(Decimal("1000.00"), "NGN", BillingCycle.monthly),
    ):
        run_invoice_cycle(db_session, dry_run=True)

    # The dry run is side-effect-free: it does not even dirty next_billing_at in
    # the session (the fast-forward is guarded behind `if not dry_run`).
    assert sub.next_billing_at == original, (
        "dry run must not mutate next_billing_at in the session"
    )

    # And nothing was committed either: reloading from the DB yields the original.
    db_session.expire(sub)
    assert sub.next_billing_at == original
