"""Owner-output chain behavior for the sales → service delivery lifecycle.

Every producing owner stages its output event atomically with its
transition; the registered ``SalesLifecycleProjectionHandler`` applies the
consequence after commit with durable retry. These tests assert the durable
staging, the applied consequences, replay idempotency, and that a failed
consequence stays a visible failed delivery instead of a warning log.
"""

from __future__ import annotations

import uuid
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models.billing_contract import BillingContract, BillingObligation
from app.models.billing_shadow_verification import BillingShadowDeliveryEvidence
from app.models.catalog import (
    BillingCycle,
    BillingMode,
    Subscription,
    SubscriptionStatus,
)
from app.models.customer_experience import (
    CustomerExperienceHandoff,
    CustomerExperienceHandoffStatus,
)
from app.models.event_store import EventStatus, EventStore
from app.models.owner_output import OwnerOutputReceipt
from app.models.project import Project, ProjectStatus
from app.models.provisioning import ServiceOrder, ServiceOrderStatus, ServiceOrderType
from app.models.sales import (
    SalesOrder,
    SalesOrderLine,
    SalesOrderPaymentStatus,
    SalesOrderStatus,
)
from app.models.subscriber import Subscriber
from app.models.vendor_routes import InstallationProject, InstallationProjectStatus
from app.schemas.sales import QuoteCreate
from app.schemas.sales_order import (
    SalesOrderCreate,
    SalesOrderLineCreate,
    SalesOrderUpdate,
)
from app.services import crm_api, customer_experience_handoffs
from app.services import sales as sales_service
from app.services import sales_orders as sales_order_service
from app.services.events.handlers.billing_lifecycle_projection import (
    BillingLifecycleProjectionHandler,
)
from app.services.events.handlers.sales_lifecycle_projection import (
    SalesLifecycleProjectionHandler,
)
from app.services.events.types import Event, EventType
from app.services.sales import selfserve


def _make_subscriber(db) -> Subscriber:
    subscriber = Subscriber(
        first_name="Chidi",
        last_name="Okoro",
        email=f"chidi-{uuid.uuid4().hex}@example.com",
    )
    db.add(subscriber)
    db.commit()
    db.refresh(subscriber)
    return subscriber


@pytest.fixture()
def chain_billing(monkeypatch, catalog_offer):
    """Record billing calls while persisting a real pending Subscription.

    Unlike the pure-mock fixture in test_sales_orders_services, the fake
    subscription is written to the database so the funding consumer can bind
    a ServiceOrder to it and replays resolve it through the line metadata.
    """
    calls: list[tuple[str, dict]] = []
    # Plain UUID: the fake runs inside the dispatch session's callback, where
    # touching an ORM object bound to the committed test session would fail.
    offer_id = catalog_offer.id

    def fake_create_subscription(db, **kwargs):
        calls.append(("create_subscription", kwargs))
        subscription = Subscription(
            subscriber_id=uuid.UUID(str(kwargs["subscriber_id"])),
            offer_id=offer_id,
            status=SubscriptionStatus.pending,
            billing_cycle=BillingCycle(
                str(kwargs.get("billing_cycle") or BillingCycle.monthly.value)
            ),
            billing_mode=BillingMode.prepaid,
            start_at=datetime.now(UTC),
            unit_price=Decimal(str(kwargs.get("unit_price") or "0")),
        )
        db.add(subscription)
        db.flush()
        return {
            "subscription": subscription,
            "invoice": SimpleNamespace(id=uuid.uuid4()),
            "created": True,
        }

    def fake_record_external_payment(db, **kwargs):
        calls.append(("record_external_payment", kwargs))
        return SimpleNamespace(id=uuid.uuid4())

    def fake_create_installation_invoice(db, **kwargs):
        calls.append(("create_installation_invoice", kwargs))
        return SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr(crm_api, "create_subscription", fake_create_subscription)
    monkeypatch.setattr(
        crm_api, "record_external_payment", fake_record_external_payment
    )
    monkeypatch.setattr(
        crm_api, "create_installation_invoice", fake_create_installation_invoice
    )
    return calls


def _funding_events(db, sales_order_id):
    return (
        db.execute(
            select(EventStore).where(
                EventStore.event_type == "sales_order.funding_satisfied",
            )
        )
        .scalars()
        .all()
    )


def test_full_funding_chains_subscription_and_service_order(
    db_session, catalog_offer, chain_billing
):
    subscriber = _make_subscriber(db_session)
    order = sales_order_service.sales_orders.create(
        db_session, SalesOrderCreate(subscriber_id=subscriber.id)
    )
    line = sales_order_service.sales_order_lines.create(
        db_session,
        SalesOrderLineCreate(
            sales_order_id=order.id,
            description="Fiber service",
            quantity=Decimal("1"),
            unit_price=Decimal("25000.00"),
            metadata_={"sub_offer_id": str(catalog_offer.id)},
        ),
    )
    assert _funding_events(db_session, order.id) == []

    order = sales_order_service.sales_orders.update(
        db_session,
        str(order.id),
        SalesOrderUpdate(
            payment_status=SalesOrderPaymentStatus.paid,
            paid_at=datetime.now(UTC),
        ),
    )

    # The output event committed atomically with the paid transition and the
    # after-commit dispatch already delivered it.
    events = _funding_events(db_session, order.id)
    assert len(events) == 1
    event = events[0]
    assert event.status == EventStatus.completed
    assert event.payload["sales_order_id"] == str(order.id)
    assert event.payload["record_order_payment"] is True

    # The consumer applied the funded consequences: one pending Subscription
    # and one draft ServiceOrder bound to the line, metadata tagged.
    subscription = db_session.query(Subscription).one()
    assert subscription.status == SubscriptionStatus.pending
    service_order = (
        db_session.query(ServiceOrder)
        .filter(ServiceOrder.sales_order_line_id == line.id)
        .one()
    )
    assert service_order.status == ServiceOrderStatus.draft
    assert service_order.subscription_id == subscription.id
    db_session.refresh(line)
    assert (line.metadata_ or {}).get("selfcare_subscription_id") == str(
        subscription.id
    )
    names = [name for name, _ in chain_billing]
    assert names.index("create_subscription") < names.index("record_external_payment")
    receipt = (
        db_session.query(OwnerOutputReceipt)
        .filter(
            OwnerOutputReceipt.consumer == "sales.fulfillment",
            OwnerOutputReceipt.event_id == event.event_id,
        )
        .one()
    )
    assert receipt.outcome.value == "succeeded"
    assert db_session.query(BillingContract).count() == 1
    assert db_session.query(BillingObligation).count() == 1
    assert db_session.query(BillingShadowDeliveryEvidence).count() == 1
    output_events = (
        db_session.query(EventStore)
        .filter(EventStore.event_type == EventType.custom.value)
        .all()
    )
    for consumer, output in (
        ("billing.contracts", "sales.fulfillment.funding_applied"),
        ("billing.obligations", "billing.contracts.shadow_recorded"),
        (
            "billing.shadow_verification",
            "billing.obligations.shadow_scheduled",
        ),
    ):
        matching = [
            item for item in output_events if item.payload.get("output") == output
        ]
        assert len(matching) == 1
        assert (
            db_session.query(OwnerOutputReceipt)
            .filter(
                OwnerOutputReceipt.consumer == consumer,
                OwnerOutputReceipt.event_id == matching[0].event_id,
            )
            .count()
            == 1
        )

    # Contract output v2 carries identity only. During the shadow rollout the
    # consumer still accepts a v1 envelope, but ignores its legacy money fields
    # and asks billing.rating for the amount again.
    contract_output = next(
        item
        for item in output_events
        if item.payload.get("output") == "billing.contracts.shadow_recorded"
    )
    assert contract_output.payload["envelope"]["schema_version"] == 2
    identity_record = contract_output.payload["obligations"][0]
    assert "net_amount" not in identity_record
    assert "tax_amount" not in identity_record

    legacy_payload = deepcopy(contract_output.payload)
    legacy_payload["envelope"]["schema_version"] = 1
    legacy_payload["obligations"][0]["net_amount"] = "999999.00"
    legacy_payload["obligations"][0]["tax_amount"] = "999999.00"
    db_session.commit()
    BillingLifecycleProjectionHandler().handle(
        db_session,
        Event(
            EventType.custom,
            legacy_payload,
            event_id=uuid.uuid4(),
            actor="pytest",
        ),
    )
    db_session.expire_all()
    obligation = db_session.query(BillingObligation).one()
    assert obligation.net_amount == Decimal("25000.00")
    assert obligation.tax_amount == Decimal("0.00")

    # Redelivering the same owner output is an exact no-op because the
    # consumer effect and its receipt committed atomically.
    chain_billing.clear()
    SalesLifecycleProjectionHandler().handle(
        db_session,
        Event(
            EventType.sales_order_funding_satisfied,
            event.payload,
            event_id=event.event_id,
            actor="pytest",
        ),
    )
    assert db_session.query(Subscription).count() == 1
    assert db_session.query(ServiceOrder).count() == 1
    assert "create_subscription" not in [name for name, _ in chain_billing]


def test_unresolved_offer_keeps_delivery_failed_and_visible(db_session):
    subscriber = _make_subscriber(db_session)
    order = sales_order_service.sales_orders.create(
        db_session, SalesOrderCreate(subscriber_id=subscriber.id)
    )
    sales_order_service.sales_order_lines.create(
        db_session,
        SalesOrderLineCreate(
            sales_order_id=order.id,
            description="Fiber service",
            quantity=Decimal("1"),
            unit_price=Decimal("25000.00"),
            metadata_={"sub_offer_id": str(uuid.uuid4())},
        ),
    )

    order = sales_order_service.sales_orders.update(
        db_session,
        str(order.id),
        SalesOrderUpdate(
            payment_status=SalesOrderPaymentStatus.paid,
            paid_at=datetime.now(UTC),
        ),
    )

    # The sale itself is committed; the unresolved consequence is a durable
    # failed delivery, not a swallowed warning and not a silent skip.
    assert order.payment_status == SalesOrderPaymentStatus.paid.value
    events = _funding_events(db_session, order.id)
    assert len(events) == 1
    assert events[0].status == EventStatus.failed
    failed_handlers = [
        item.get("handler") for item in (events[0].failed_handlers or [])
    ]
    assert "SalesLifecycleProjectionHandler" in failed_handlers
    assert db_session.query(Subscription).count() == 0
    assert db_session.query(ServiceOrder).count() == 0


def test_selfserve_full_deposit_stages_funding_without_order_payment(
    db_session, catalog_offer, chain_billing
):
    subscriber = _make_subscriber(db_session)
    quote = sales_service.quotes.create(
        db_session, QuoteCreate(subscriber_id=subscriber.id)
    )
    order = sales_order_service.sales_orders.create(
        db_session,
        SalesOrderCreate(subscriber_id=subscriber.id, quote_id=quote.id),
    )
    sales_order_service.sales_order_lines.create(
        db_session,
        SalesOrderLineCreate(
            sales_order_id=order.id,
            description="Fiber service",
            quantity=Decimal("1"),
            unit_price=Decimal("50000.00"),
            metadata_={"sub_offer_id": str(catalog_offer.id)},
        ),
    )
    chain_billing.clear()

    selfserve._record_deposit_on_sales_order(db_session, quote, Decimal("50000.00"))

    db_session.refresh(order)
    assert order.payment_status == SalesOrderPaymentStatus.paid.value
    events = _funding_events(db_session, order.id)
    assert len(events) == 1
    assert events[0].status == EventStatus.completed
    assert events[0].payload["record_order_payment"] is False
    # The deposit's only ledger event stays the verified deposit-invoice
    # payment; the consumer still creates the funded service artifacts.
    assert db_session.query(Subscription).count() == 1
    assert db_session.query(ServiceOrder).count() == 1
    assert "record_external_payment" not in [name for name, _ in chain_billing]


def test_cx_acceptance_fulfils_order_through_committed_output(
    db_session, subscriber, catalog_offer
):
    order = SalesOrder(
        subscriber_id=subscriber.id,
        order_number=f"SO-CHAIN-{uuid.uuid4().hex[:8]}",
        status=SalesOrderStatus.paid.value,
        payment_status=SalesOrderPaymentStatus.paid.value,
        total=100,
        amount_paid=100,
        balance_due=0,
    )
    db_session.add(order)
    db_session.flush()
    line = SalesOrderLine(
        sales_order_id=order.id,
        description="Fiber service",
        quantity=1,
        unit_price=100,
        amount=100,
    )
    project = Project(
        name="Chain implementation",
        subscriber_id=subscriber.id,
        sales_order_id=order.id,
        status=ProjectStatus.completed.value,
    )
    db_session.add_all([line, project])
    db_session.flush()
    installation = InstallationProject(
        project_id=project.id,
        subscriber_id=subscriber.id,
        status=InstallationProjectStatus.verified.value,
    )
    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=SubscriptionStatus.active,
    )
    db_session.add_all([installation, subscription])
    db_session.flush()
    service_order = ServiceOrder(
        subscriber_id=subscriber.id,
        subscription_id=subscription.id,
        sales_order_id=order.id,
        sales_order_line_id=line.id,
        project_id=project.id,
        installation_project_id=installation.id,
        status=ServiceOrderStatus.active,
        order_type=ServiceOrderType.new_install,
    )
    db_session.add(service_order)
    db_session.flush()
    handoff = CustomerExperienceHandoff(
        subscriber_id=subscriber.id,
        subscription_id=subscription.id,
        sales_order_id=order.id,
        project_id=project.id,
        installation_project_id=installation.id,
        service_order_id=service_order.id,
        status=CustomerExperienceHandoffStatus.ready.value,
        policy_version=customer_experience_handoffs.POLICY_VERSION,
        readiness_evidence={"eligible": True},
        ready_at=datetime.now(UTC),
    )
    db_session.add(handoff)
    db_session.commit()

    customer_experience_handoffs.accept_handoff(
        db_session,
        handoff_id=handoff.id,
        actor_type="staff_user",
        actor_id="pytest",
        reason="Welcome call complete",
        commit=False,
    )
    # The CX owner no longer writes sales state inline: before its fact
    # commits, the order is untouched.
    assert order.status == SalesOrderStatus.paid.value

    db_session.commit()
    db_session.refresh(order)
    assert order.status == SalesOrderStatus.fulfilled.value
    assert (order.metadata_ or {}).get("cx_handoff_id") == str(handoff.id)
    accepted = (
        db_session.execute(
            select(EventStore).where(
                EventStore.event_type == "customer_experience.accepted"
            )
        )
        .scalars()
        .one()
    )
    assert accepted.status == EventStatus.completed
    # The fulfilment effect committed atomically with its unique receipt;
    # a redelivery of the same event is an exact no-op.
    from app.models.owner_output import OwnerOutputReceipt

    receipt = (
        db_session.query(OwnerOutputReceipt)
        .filter(
            OwnerOutputReceipt.consumer == "sales.fulfillment",
            OwnerOutputReceipt.event_id == accepted.event_id,
        )
        .one()
    )
    assert receipt.outcome.value == "succeeded"
