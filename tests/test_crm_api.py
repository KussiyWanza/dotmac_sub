from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.parse import urlencode

import pytest
from fastapi import HTTPException
from sqlalchemy import event
from starlette.requests import Request

from app.api import crm as crm_routes
from app.models.audit import AuditEvent
from app.models.auth import ApiKey
from app.models.billing import (
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    Payment,
    PaymentStatus,
)
from app.models.catalog import (
    AccessType,
    BillingMode,
    CatalogOffer,
    OfferPrice,
    PriceBasis,
    PriceType,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.collections import DunningCase
from app.models.enforcement_lock import EnforcementLock, EnforcementReason
from app.models.lifecycle import SubscriptionLifecycleEvent
from app.models.service_extension import (
    ServiceExtension,
    ServiceExtensionAnchorBasis,
    ServiceExtensionEntry,
    ServiceExtensionScope,
    ServiceExtensionStatus,
)
from app.models.subscriber import Subscriber, SubscriberStatus
from app.models.system_user import SystemUser
from app.models.usage import AccountingStatus, RadiusAccountingSession
from app.services import crm_api
from app.services.auth import hash_api_key

TOKEN = "crm-test-token"


@pytest.fixture()
def crm_auth(db_session):
    db_session.add(
        ApiKey(
            label="crm-test",
            key_hash=hash_api_key(TOKEN),
            scopes=["integration:crm"],
            is_active=True,
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
    )
    db_session.commit()
    return TOKEN


def _headers() -> dict[str, str]:
    return {"X-Api-Key": TOKEN}


class _RouteResponse:
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body

    def json(self) -> dict:
        return self._body


def _call_route(func, *args, **kwargs) -> _RouteResponse:
    try:
        body = func(*args, **kwargs)
    except HTTPException as exc:
        return _RouteResponse(exc.status_code, {"detail": exc.detail})
    return _RouteResponse(200, body)


def _request(query: dict[str, str] | str | None = None) -> Request:
    if isinstance(query, dict):
        query_string = urlencode(query).encode()
    elif query:
        query_string = str(query).encode()
    else:
        query_string = b""
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/crm",
            "query_string": query_string,
            "headers": [],
        }
    )


def _subscriber(db_session, *, status=SubscriberStatus.active) -> Subscriber:
    subscriber = Subscriber(
        first_name="Ada",
        last_name="Lovelace",
        display_name="Ada Lovelace",
        email=f"ada-{uuid.uuid4().hex}@example.com",
        phone="08030000000",
        subscriber_number=f"SUB-{uuid.uuid4().hex[:8]}",
        account_number=f"ACC-{uuid.uuid4().hex[:8]}",
        city="Lekki",
        region="Lagos",
        status=status,
        account_start_date=datetime(2026, 1, 1, tzinfo=UTC),
    )
    db_session.add(subscriber)
    db_session.flush()
    return subscriber


def _offer(db_session) -> CatalogOffer:
    offer = CatalogOffer(
        name="Fiber 50",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        speed_download_mbps=50,
        speed_upload_mbps=10,
    )
    db_session.add(offer)
    db_session.flush()
    db_session.add(
        OfferPrice(
            offer_id=offer.id,
            price_type=PriceType.recurring,
            amount=Decimal("15000.00"),
        )
    )
    db_session.flush()
    return offer


def _subscription(
    db_session, subscriber: Subscriber, offer: CatalogOffer
) -> Subscription:
    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=SubscriptionStatus.active,
        start_at=datetime(2026, 1, 1, tzinfo=UTC),
        next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    db_session.add(subscription)
    db_session.flush()
    return subscription


def _billing(db_session, subscriber: Subscriber, subscription: Subscription) -> None:
    invoice = Invoice(
        account_id=subscriber.id,
        status=InvoiceStatus.issued,
        total=Decimal("15000.00"),
        balance_due=Decimal("5000.00"),
        billing_period_start=datetime(2026, 6, 1, tzinfo=UTC),
        billing_period_end=datetime(2026, 6, 30, tzinfo=UTC),
        issued_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    db_session.add(invoice)
    db_session.flush()
    db_session.add(
        InvoiceLine(
            invoice_id=invoice.id,
            subscription_id=subscription.id,
            description="Fiber 50 monthly service",
            amount=Decimal("15000.00"),
            unit_price=Decimal("15000.00"),
        )
    )
    db_session.add(
        Payment(
            account_id=subscriber.id,
            amount=Decimal("10000.00"),
            status=PaymentStatus.succeeded,
            paid_at=datetime(2026, 6, 15, tzinfo=UTC),
        )
    )
    db_session.flush()


def _session(db_session, subscription: Subscription) -> None:
    now = datetime.now(UTC)
    db_session.add(
        RadiusAccountingSession(
            subscription_id=subscription.id,
            session_id="SID-1",
            status_type=AccountingStatus.interim,
            session_start=now - timedelta(hours=2),
            last_update_at=now - timedelta(minutes=5),
            input_octets=123,
            output_octets=456,
        )
    )
    db_session.flush()


def test_ping_requires_scoped_api_key(db_session, crm_auth):
    from types import SimpleNamespace

    def _auth(x_api_key=None):
        return _call_route(
            crm_routes.require_crm_service_auth,
            request=SimpleNamespace(state=SimpleNamespace(), cookies={}),
            x_api_key=x_api_key,
            db=db_session,
        )

    missing = _auth()
    valid = _auth(TOKEN)

    assert missing.status_code == 401
    assert valid.status_code == 200
    assert crm_routes.ping() == {"status": "ok"}


def test_subscriber_list_embeds_services_billing_and_session_state(
    db_session, crm_auth
):
    subscriber = _subscriber(db_session)
    subscriber.billing_mode = BillingMode.prepaid
    subscriber.billing_day = 7
    offer = _offer(db_session)
    subscription = _subscription(db_session, subscriber, offer)
    _billing(db_session, subscriber, subscription)
    _session(db_session, subscription)
    db_session.commit()

    response = _call_route(
        crm_routes.list_subscribers,
        _request({"include": "services,billing,session_state"}),
        db_session,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["meta"]["total"] >= 1
    row = body["data"][0]
    assert row["subscriber_number"] == subscriber.subscriber_number
    assert row["billing_mode"] == "prepaid"
    assert row["billing_day"] == 7
    assert row["services"][0]["plan_name"] == "Fiber 50"
    assert row["services"][0]["speed"] == "50/10 Mbps"
    assert row["billing"]["balance"] == 5000.0
    assert row["billing"]["total_paid"] == 10000.0
    assert row["session_state"] == "online"
    assert row["last_seen"].endswith("Z")


def test_latest_session_projection_returns_one_ranked_row_per_subscription(db_session):
    subscriber = _subscriber(db_session)
    subscription = _subscription(db_session, subscriber, _offer(db_session))
    now = datetime.now(UTC)
    for index in range(40):
        db_session.add(
            RadiusAccountingSession(
                subscription_id=subscription.id,
                session_id=f"historical-{index}",
                status_type=AccountingStatus.stop,
                session_start=now - timedelta(days=index + 1),
                last_update_at=now - timedelta(days=index + 1),
            )
        )
    newest = RadiusAccountingSession(
        subscription_id=subscription.id,
        session_id="newest",
        status_type=AccountingStatus.interim,
        session_start=now - timedelta(minutes=2),
        last_update_at=now - timedelta(minutes=1),
    )
    db_session.add(newest)
    db_session.commit()

    result = crm_api.latest_session_by_subscriber(db_session, [subscriber.id])

    assert len(result) == 1
    assert result[subscriber.id].id == newest.id


def test_subscriber_list_batches_payload_metadata(db_session, crm_auth):
    offer = _offer(db_session)
    subscribers = []
    for _ in range(5):
        subscriber = _subscriber(db_session)
        subscriber.account_start_date = None
        subscription = _subscription(db_session, subscriber, offer)
        db_session.add_all(
            [
                SubscriptionLifecycleEvent(
                    subscription_id=subscription.id,
                    to_status=SubscriptionStatus.suspended,
                    created_at=datetime(2026, 5, 1, tzinfo=UTC),
                ),
                SubscriptionLifecycleEvent(
                    subscription_id=subscription.id,
                    to_status=SubscriptionStatus.canceled,
                    created_at=datetime(2026, 6, 1, tzinfo=UTC),
                ),
            ]
        )
        subscribers.append(subscriber)
    db_session.commit()

    statements = []
    engine = db_session.get_bind()

    def count_statement(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", count_statement)
    try:
        response = _call_route(
            crm_routes.list_subscribers,
            _request({"page": "1", "per_page": "5"}),
            db_session,
        )
    finally:
        event.remove(engine, "before_cursor_execute", count_statement)

    assert response.status_code == 200
    rows = response.json()["data"]
    target_ids = {str(subscriber.id) for subscriber in subscribers}
    target_rows = [row for row in rows if row["id"] in target_ids]
    assert len(target_rows) == 5
    assert {row["activated_at"] for row in target_rows} == {"2026-01-01T00:00:00Z"}
    assert {row["suspended_at"] for row in target_rows} == {"2026-05-01T00:00:00Z"}
    assert {row["terminated_at"] for row in target_rows} == {"2026-06-01T00:00:00Z"}
    assert len(statements) <= 10


def test_crm_pagination_uses_shared_limit_and_accepts_legacy_alias(crm_auth):
    assert crm_routes._pagination(_request({"per_page": "500"}))[:2] == (1, 500)
    assert crm_routes._pagination(_request({"limit": "10"}))[:2] == (1, 10)

    response = _call_route(
        crm_routes.list_subscribers,
        _request({"page": "1", "per_page": "501"}),
        object(),
    )

    assert response.status_code == 400
    assert "per_page" in response.json()["detail"]["errors"]


def test_locations_uses_short_cache(db_session, crm_auth):
    crm_api._locations_cache = None
    subscriber = _subscriber(db_session)
    db_session.commit()

    statements = []
    engine = db_session.get_bind()

    def count_statement(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", count_statement)
    try:
        first = _call_route(crm_routes.locations, _request(), db_session)
        second = _call_route(crm_routes.locations, _request(), db_session)
    finally:
        event.remove(engine, "before_cursor_execute", count_statement)
        crm_api._locations_cache = None

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()
    assert any(item["name"] == subscriber.city for item in first.json()["data"])
    assert len(statements) == 2


def test_locations_service_owns_pagination(db_session, monkeypatch):
    rows = [
        {"id": "first", "name": "First"},
        {"id": "second", "name": "Second"},
    ]

    def page_rows(db, *, page, per_page):
        start = (page - 1) * per_page
        return rows[start : start + per_page], len(rows)

    monkeypatch.setattr(crm_api, "locations", page_rows)

    first = _call_route(
        crm_routes.locations,
        _request({"page": "1", "per_page": "1"}),
        db_session,
    )
    second = _call_route(
        crm_routes.locations,
        _request({"page": "2", "per_page": "1"}),
        db_session,
    )
    final = _call_route(
        crm_routes.locations,
        _request({"page": "3", "per_page": "1"}),
        db_session,
    )

    assert first.status_code == 200
    assert first.json() == {
        "data": [rows[0]],
        "meta": {"page": 1, "per_page": 1, "total": 2},
    }
    assert second.json() == {
        "data": [rows[1]],
        "meta": {"page": 2, "per_page": 1, "total": 2},
    }
    assert final.json()["data"] == []
    assert final.json()["meta"] == {"page": 3, "per_page": 1, "total": 2}


def test_online_subscribers_paginates_scalar_query_and_uses_latest_session(
    db_session, crm_auth
):
    now = datetime.now(UTC)
    offer = _offer(db_session)
    subscribers = [_subscriber(db_session) for _ in range(3)]
    subscriptions = [
        _subscription(db_session, subscriber, offer) for subscriber in subscribers
    ]
    for index, subscription in enumerate(subscriptions):
        db_session.add(
            RadiusAccountingSession(
                subscription_id=subscription.id,
                session_id=f"SID-{index}",
                status_type=AccountingStatus.interim,
                session_start=now - timedelta(hours=2),
                last_update_at=now - timedelta(minutes=10 - index),
            )
        )
    latest_seen = now - timedelta(minutes=1)
    db_session.add(
        RadiusAccountingSession(
            subscription_id=subscriptions[0].id,
            session_id="SID-latest",
            status_type=AccountingStatus.interim,
            session_start=now - timedelta(hours=1),
            last_update_at=latest_seen,
        )
    )
    db_session.commit()
    latest_subscriber_id = str(subscribers[0].id)

    statements: list[str] = []
    engine = db_session.get_bind()

    def capture_statement(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture_statement)
    try:
        first = _call_route(
            crm_routes.online_subscribers,
            _request({"page": "1", "per_page": "2"}),
            db_session,
        )
        second = _call_route(
            crm_routes.online_subscribers,
            _request({"page": "2", "per_page": "2"}),
            db_session,
        )
        final = _call_route(
            crm_routes.online_subscribers,
            _request({"page": "3", "per_page": "2"}),
            db_session,
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture_statement)

    assert first.json()["meta"] == {"page": 1, "per_page": 2, "total": 3}
    assert second.json()["meta"] == {"page": 2, "per_page": 2, "total": 3}
    assert final.json() == {
        "data": [],
        "meta": {"page": 3, "per_page": 2, "total": 3},
    }
    rows = [*first.json()["data"], *second.json()["data"]]
    assert len(rows) == 3
    assert len({row["id"] for row in rows}) == 3
    latest_row = next(row for row in rows if row["id"] == latest_subscriber_id)
    assert latest_row["last_seen"] == crm_api.utc_iso(latest_seen)
    assert len(statements) == 4
    assert sum(" LIMIT " in statement.upper() for statement in statements) == 3
    assert all("FROM invoices" not in statement for statement in statements)


def test_invalid_query_parameters_return_400_field_errors(crm_auth):
    response = _call_route(
        crm_routes.list_subscribers,
        _request({"include": "unknown"}),
        object(),
    )

    assert response.status_code == 400
    assert "include" in response.json()["detail"]["errors"]


def test_unknown_subscriber_returns_404(db_session, crm_auth):
    response = _call_route(
        crm_routes.subscriber_detail,
        str(uuid.uuid4()),
        db_session,
    )

    assert response.status_code == 404
    assert response.json()["detail"]["message"] == "Subscriber not found."


def test_finance_and_session_endpoints(db_session, crm_auth):
    subscriber = _subscriber(db_session)
    offer = _offer(db_session)
    subscription = _subscription(db_session, subscriber, offer)
    _billing(db_session, subscriber, subscription)
    _session(db_session, subscription)
    db_session.commit()

    transactions = _call_route(
        crm_routes.finance_transactions,
        _request({"customer_id": str(subscriber.id), "date_from": "2026-06-01"}),
        db_session,
    )
    payments = _call_route(
        crm_routes.finance_payments,
        _request({"customer_id": str(subscriber.id), "date_to": "2026-06-30"}),
        db_session,
    )
    sessions = _call_route(
        crm_routes.subscriber_sessions,
        str(subscriber.id),
        db_session,
    )

    assert transactions.status_code == 200
    assert transactions.json()["data"][0]["service_id"] == str(subscription.id)
    assert payments.status_code == 200
    assert payments.json()["data"][0]["amount"] == 10000.0
    assert sessions.status_code == 200
    assert sessions.json()["data"][0]["bytes_downloaded"] == 456


def test_billing_risk_source_batches_page_aggregates(db_session, crm_auth):
    offer = _offer(db_session)
    subscribers = []
    for _ in range(3):
        subscriber = _subscriber(db_session)
        subscription = _subscription(db_session, subscriber, offer)
        _billing(db_session, subscriber, subscription)
        subscribers.append((subscriber, subscription))

    target, target_subscription = subscribers[0]
    db_session.add(
        Payment(
            account_id=target.id,
            amount=Decimal("12500.00"),
            status=PaymentStatus.succeeded,
            paid_at=datetime(2026, 6, 20, tzinfo=UTC),
        )
    )
    db_session.add(
        EnforcementLock(
            subscriber_id=target.id,
            subscription_id=target_subscription.id,
            reason=EnforcementReason.overdue,
            source="test",
            created_at=datetime(2026, 6, 18, tzinfo=UTC),
        )
    )
    db_session.add(
        DunningCase(
            account_id=target.id,
            started_at=datetime(2026, 6, 19, tzinfo=UTC),
        )
    )
    db_session.commit()

    statements = []
    engine = db_session.get_bind()

    def count_statement(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", count_statement)
    try:
        response = _call_route(
            crm_routes.billing_risk_source,
            _request({"page": "1", "per_page": "3"}),
            db_session,
        )
    finally:
        event.remove(engine, "before_cursor_execute", count_statement)

    assert response.status_code == 200
    body = response.json()
    assert body["meta"]["total"] >= 3
    row = next(item for item in body["data"] if item["id"] == str(target.id))
    assert row["balance"] == 5000.0
    assert row["total_paid"] == 22500.0
    assert row["last_payment_amount"] == 12500.0
    assert row["last_payment_date"] == "2026-06-20T00:00:00Z"
    assert row["blocked_date"] == "2026-06-19T00:00:00Z"
    assert row["service_plan"] == "Fiber 50"
    # Primary-service monthly price exposed in bulk so omni reads MRR here
    # instead of an N+1 per-subscriber GET /subscribers/{id}/services.
    assert row["service_mrr"] == 15000.0
    assert len(statements) <= 20


def test_service_extension_endpoints_expose_compensation_for_crm(db_session, crm_auth):
    subscriber = _subscriber(db_session)
    subscriber.splynx_customer_id = 11038
    offer = _offer(db_session)
    subscription = _subscription(db_session, subscriber, offer)
    actor = SystemUser(
        first_name="Aisha",
        last_name="Ibrahim",
        email=f"aisha-{uuid.uuid4().hex}@example.com",
    )
    db_session.add(actor)
    db_session.flush()
    extension = ServiceExtension(
        reason="Customer Link disconnection",
        window_start=datetime(2026, 6, 1, 8, 0, tzinfo=UTC),
        window_end=datetime(2026, 6, 2, 8, 0, tzinfo=UTC),
        days=2,
        scope_type=ServiceExtensionScope.subscribers,
        scope_subscriber_ids=[str(subscriber.id)],
        status=ServiceExtensionStatus.applied,
        affected_count=1,
        skipped_count=0,
        created_by=str(actor.id),
        applied_by=str(actor.id),
        applied_at=datetime(2026, 6, 3, 9, 0, tzinfo=UTC),
    )
    db_session.add(extension)
    db_session.flush()
    db_session.add(
        ServiceExtensionEntry(
            extension_id=extension.id,
            subscriber_id=subscriber.id,
            subscription_id=subscription.id,
            previous_next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
            grant_starts_at=datetime(2026, 7, 1, tzinfo=UTC),
            grant_ends_at=datetime(2026, 7, 3, tzinfo=UTC),
            anchor_basis=ServiceExtensionAnchorBasis.legacy_previous_anchor,
            new_next_billing_at=datetime(2026, 7, 3, tzinfo=UTC),
        )
    )
    db_session.commit()

    listing = _call_route(
        crm_routes.service_extensions,
        _request({"page": "1", "per_page": "10"}),
        db_session,
    )
    detail = _call_route(
        crm_routes.service_extension_detail,
        str(extension.id),
        db_session,
    )
    subscriber_rows = _call_route(
        crm_routes.subscriber_service_extensions,
        str(subscriber.id),
        db_session,
    )

    assert listing.status_code == 200
    listed = next(
        item for item in listing.json()["data"] if item["id"] == str(extension.id)
    )
    assert listed["reason"] == "Customer Link disconnection"
    assert listed["created_by"]["name"] == "Aisha Ibrahim"
    assert detail.status_code == 200
    detail_row = detail.json()["data"]
    assert (
        detail_row["affected_customers"][0]["customer_id"]
        == subscriber.splynx_customer_id
    )
    assert (
        detail_row["affected_customers"][0]["new_next_billing_at"]
        == "2026-07-03T00:00:00Z"
    )
    assert (
        detail_row["affected_customers"][0]["grant_starts_at"] == "2026-07-01T00:00:00Z"
    )
    assert (
        detail_row["affected_customers"][0]["anchor_basis"] == "legacy_previous_anchor"
    )
    assert subscriber_rows.status_code == 200
    assert (
        subscriber_rows.json()["data"][0]["entry"]["previous_next_billing_at"]
        == "2026-07-01T00:00:00Z"
    )


def test_status_writeback_rejects_active_and_logs(db_session, crm_auth):
    subscriber = _subscriber(db_session, status=SubscriberStatus.active)
    db_session.commit()

    response = _call_route(
        crm_routes.update_subscriber_status,
        str(subscriber.id),
        payload={"status": "disabled", "reason": "retention_lost", "source": "crm"},
        x_crm_actor="crm-user-1",
        db=db_session,
    )

    assert response.status_code == 409
    event = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.entity_id == str(subscriber.id))
        .one()
    )
    assert event.metadata_["result"] == "rejected_transition"
    assert event.actor_id == "crm-user-1"


def test_status_writeback_disables_suspended_subscriber(db_session, crm_auth):
    subscriber = _subscriber(db_session, status=SubscriberStatus.suspended)
    offer = _offer(db_session)
    subscription = _subscription(db_session, subscriber, offer)
    subscription.status = SubscriptionStatus.suspended
    db_session.commit()

    response = _call_route(
        crm_routes.update_subscriber_status,
        str(subscriber.id),
        payload={"status": "disabled", "reason": "retention_lost", "source": "crm"},
        x_crm_actor="crm-user-2",
        db=db_session,
    )

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "disabled"
    db_session.refresh(subscriber)
    db_session.refresh(subscription)
    assert subscriber.status == SubscriberStatus.disabled
    assert subscription.status == SubscriptionStatus.disabled


def test_ncc_subscriber_report_endpoint_returns_aggregate(db_session, crm_auth):
    subscriber = _subscriber(db_session)
    offer = _offer(db_session)
    _subscription(db_session, subscriber, offer)
    db_session.commit()

    body = crm_routes.ncc_subscriber_report(
        as_of="2026-06-30",
        points_of_presence="28",
        db=db_session,
    )

    report = body["data"]
    assert report["total_active_subscriptions"] == 1
    assert report["parameters"]["active_statuses"] == ["active"]
    assert report["network_capacity"]["points_of_presence"] == 28
    # picker params flow through untouched
    assert report["parameters"]["as_of"].startswith("2026-06-30")
