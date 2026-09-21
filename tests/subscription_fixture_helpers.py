"""Schema-honest subscription setup shared by database-backed tests."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.catalog import Subscription, SubscriptionStatus
from app.services.account_lifecycle import activate_subscription


def activate_test_subscription(
    db: Session,
    subscription: Subscription,
) -> Subscription:
    """Activate a pending test subscription through the lifecycle owner.

    PostgreSQL migration ``539_active_sub_billing_anchor`` rejects a new or
    changed active row without ``start_at`` and ``next_billing_at``.  Tests
    must therefore use the same owner as production instead of assigning the
    status enum directly and relying on SQLite to miss the database invariant.
    """

    if subscription.status == SubscriptionStatus.pending:
        activate_subscription(db, str(subscription.id), emit=False)
    elif subscription.status != SubscriptionStatus.active:
        raise AssertionError(
            "Test subscription must be pending or active before activation; "
            f"got {subscription.status.value}"
        )

    db.flush()
    if subscription.start_at is None or subscription.next_billing_at is None:
        raise AssertionError(
            "Active test subscription is missing its required billing anchor"
        )
    return subscription
