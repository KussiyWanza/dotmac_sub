"""Contract tests for bounded cross-application sync feeds."""

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import event

from app.api.billing import router as billing_router
from app.api.subscribers import router as subscriber_router
from app.models.catalog import BillingCycle, SubscriptionStatus
from app.models.subscriber import UserType
from app.schemas.subscriber import SubscriberSyncRead
from app.services import subscriber as subscriber_service


def test_sync_routes_precede_dynamic_detail_routes():
    paths = [
        getattr(route, "path", "")
        for router in (subscriber_router, billing_router)
        for route in router.routes
    ]
    pairs = (
        ("/subscribers/sync", "/subscribers/{subscriber_id}"),
        ("/resellers/sync", "/resellers/{reseller_id}"),
        ("/invoices/sync", "/invoices/{invoice_id}"),
        ("/payments/sync", "/payments/{payment_id}"),
        ("/credit-notes/sync", "/credit-notes/{credit_note_id}"),
        ("/payment-channels/sync", "/payment-channels/{channel_id}"),
        ("/tax-rates/sync", "/tax-rates/{rate_id}"),
        ("/billing-accounts/sync", "/billing-accounts/{billing_account_id}"),
    )

    for sync_path, detail_path in pairs:
        assert sync_path in paths
        assert paths.index(sync_path) < paths.index(detail_path)


def test_subscriber_sync_feed_uses_one_query_and_minimal_projection(
    db_session, subscriber_account
):
    subscriber_account.user_type = UserType.customer
    subscriber_account.updated_at = datetime(2026, 1, 2, tzinfo=UTC)
    db_session.commit()
    statements: list[str] = []
    bind = db_session.get_bind()

    def count_statement(_conn, _cursor, statement, _params, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(bind, "before_cursor_execute", count_statement)
    try:
        response = subscriber_service.subscribers.sync_list_response(
            db_session,
            subscriber_type=None,
            updated_since=datetime(2026, 1, 1, tzinfo=UTC),
            limit=500,
            offset=0,
        )
        payload = SubscriberSyncRead.model_validate(response["items"][0]).model_dump()
    finally:
        event.remove(bind, "before_cursor_execute", count_statement)

    assert len(statements) == 1
    assert payload["id"] == subscriber_account.id
    assert "subscriptions" not in payload
    assert "channels" not in payload
    assert "billing_config" not in payload


def test_subscriber_sync_feed_projects_commercial_metrics_and_watermark(
    db_session, subscriber_account, subscription
):
    subscriber_updated_at = datetime(2026, 1, 1, tzinfo=UTC)
    subscription_updated_at = datetime(2026, 1, 3, tzinfo=UTC)
    next_renewal_at = datetime(2026, 2, 1, tzinfo=UTC)
    subscriber_account.user_type = UserType.customer
    subscriber_account.updated_at = subscriber_updated_at
    subscription.status = SubscriptionStatus.active
    subscription.service_status_raw = "online"
    subscription.billing_cycle = BillingCycle.quarterly
    subscription.unit_price = Decimal("300.00")
    subscription.quantity = 2
    subscription.next_billing_at = next_renewal_at
    subscription.updated_at = subscription_updated_at
    db_session.commit()

    response = subscriber_service.subscribers.sync_list_response(
        db_session,
        subscriber_type=None,
        updated_since=datetime(2026, 1, 2, tzinfo=UTC),
        limit=500,
        offset=0,
    )

    assert response["count"] == 1
    projection = response["items"][0]
    assert isinstance(projection, SubscriberSyncRead)
    assert projection.id == subscriber_account.id
    assert projection.updated_at.replace(tzinfo=UTC) == subscription_updated_at
    assert projection.service_status == "online"
    assert projection.recurring_subscription_count == 1
    assert projection.next_renewal_at is not None
    assert projection.next_renewal_at.replace(tzinfo=UTC) == next_renewal_at
    assert projection.billing_cycle == BillingCycle.quarterly.value
    assert projection.recurring_amount_monthly == Decimal("200.00")
    assert projection.annualized_recurring_revenue == Decimal("2400.00")
