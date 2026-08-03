"""Sales-order service tests (Phase 3 port), including the §2.3 native
financial-side-effect rewiring and the crm#233 account_id fix."""

import inspect
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.party import Party
from app.models.project import Project, ProjectTask, ProjectTemplate
from app.models.provisioning import ServiceOrder, ServiceOrderStatus
from app.models.sales import (
    QuoteStatus,
    SalesOrder,
    SalesOrderPaymentStatus,
    SalesOrderStatus,
)
from app.models.sequence import DocumentSequence
from app.models.subscriber import Subscriber
from app.schemas.sales import (
    LeadCreate,
    QuoteCreate,
    QuoteLineItemCreate,
    QuoteUpdate,
)
from app.schemas.sales_order import (
    SalesOrderCreate,
    SalesOrderLineCreate,
    SalesOrderUpdate,
)
from app.services import crm_api
from app.services import sales as sales_service
from app.services import sales_orders as sales_order_service


def _make_subscriber(db) -> Subscriber:
    party = Party(
        display_name="Bola Ade",
        party_type="person",
        status="active",
    )
    db.add(party)
    db.flush()
    subscriber = Subscriber(
        first_name="Bola",
        last_name="Ade",
        email=f"bola-{uuid.uuid4().hex}@example.com",
        party_id=party.id,
        party_bound_at=datetime.now(UTC),
        party_binding_source="pytest",
        party_binding_reason="Sales order fixture Party binding",
    )
    db.add(subscriber)
    db.commit()
    db.refresh(subscriber)
    return subscriber


def _make_quote(db, subscriber: Subscriber):
    if (
        db.query(ProjectTemplate)
        .filter_by(project_type="fiber_optics_installation", is_active=True)
        .first()
        is None
    ):
        db.add(
            ProjectTemplate(
                name="Sales-order service installation",
                project_type="fiber_optics_installation",
                is_active=True,
            )
        )
        db.commit()
    lead = sales_service.leads.create(db, LeadCreate(subscriber_id=subscriber.id))
    return sales_service.quotes.create(
        db,
        QuoteCreate(
            subscriber_id=subscriber.id,
            lead_id=lead.id,
            project_type="fiber_optics_installation",
        ),
    )


def _accept_quote(db, quote):
    sales_service.quote_line_items.create(
        db,
        QuoteLineItemCreate(
            quote_id=quote.id,
            description="Accepted implementation scope",
            quantity=Decimal("1"),
            unit_price=Decimal("1.00"),
        ),
    )
    sales_service.quotes.update(
        db, str(quote.id), QuoteUpdate(status=QuoteStatus.accepted)
    )
    return db.query(SalesOrder).filter_by(quote_id=quote.id).one()


@pytest.fixture()
def billing_calls(monkeypatch):
    """Record the §2.3 in-process billing calls instead of hitting the real
    invoice/subscription/payment services."""
    calls: list[tuple[str, dict]] = []

    def fake_create_subscription(db, **kwargs):
        calls.append(("create_subscription", kwargs))
        return {
            "subscription": SimpleNamespace(id=uuid.uuid4()),
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


# ---------------------------------------------------------------------------
# Numbering (SO-%06d via document_sequences)
# ---------------------------------------------------------------------------


def test_order_number_continues_document_sequence(db_session):
    # The backfill imports the CRM row's next_value under the same key; the
    # generator must continue that sequence (§1.5, risk #10).
    db_session.add(DocumentSequence(key="sales_order_number", next_value=1234))
    db_session.commit()

    subscriber = _make_subscriber(db_session)
    first = sales_order_service.sales_orders.create(
        db_session, SalesOrderCreate(subscriber_id=subscriber.id)
    )
    second = sales_order_service.sales_orders.create(
        db_session, SalesOrderCreate(subscriber_id=subscriber.id)
    )
    assert first.order_number == "SO-001234"
    assert second.order_number == "SO-001235"


# ---------------------------------------------------------------------------
# The crm#233 fix — fixed list shape, no account_id slot to mis-plumb
# ---------------------------------------------------------------------------


def test_list_signature_has_no_account_id_slot():
    params = inspect.signature(sales_order_service.SalesOrders.list).parameters
    assert "account_id" not in params
    assert "subscriber_id" in params
    assert "quote_id" in params


def test_list_filters_by_subscriber_quote_and_status(db_session):
    subscriber = _make_subscriber(db_session)
    other = _make_subscriber(db_session)
    quote = _make_quote(db_session, subscriber)
    with_quote = _accept_quote(db_session, quote)
    sales_order_service.sales_orders.create(
        db_session, SalesOrderCreate(subscriber_id=other.id)
    )

    by_subscriber = sales_order_service.sales_orders.list(
        db_session, subscriber_id=str(subscriber.id)
    )
    assert [so.id for so in by_subscriber] == [with_quote.id]

    by_quote = sales_order_service.sales_orders.list(db_session, quote_id=str(quote.id))
    assert [so.id for so in by_quote] == [with_quote.id]

    by_status = sales_order_service.sales_orders.list(
        db_session, status=SalesOrderStatus.confirmed.value
    )
    assert {so.id for so in by_status} >= {with_quote.id}


def test_second_sales_order_for_quote_rejected(db_session):
    subscriber = _make_subscriber(db_session)
    quote = _make_quote(db_session, subscriber)
    _accept_quote(db_session, quote)
    with pytest.raises(HTTPException) as exc:
        sales_order_service.sales_orders.create(
            db_session,
            SalesOrderCreate(subscriber_id=subscriber.id, quote_id=quote.id),
        )
    assert exc.value.status_code == 400


def test_create_from_quote_is_idempotent(db_session):
    subscriber = _make_subscriber(db_session)
    quote = _make_quote(db_session, subscriber)
    _accept_quote(db_session, quote)
    first = sales_order_service.sales_orders.create_from_quote(
        db_session, str(quote.id)
    )
    second = sales_order_service.sales_orders.create_from_quote(
        db_session, str(quote.id)
    )
    assert first.id == second.id


# ---------------------------------------------------------------------------
# Payment-field state machine
# ---------------------------------------------------------------------------


def test_payment_field_transitions(db_session):
    subscriber = _make_subscriber(db_session)
    order = sales_order_service.sales_orders.create(
        db_session,
        SalesOrderCreate(subscriber_id=subscriber.id, total=Decimal("100.00")),
    )
    assert order.payment_status == SalesOrderPaymentStatus.pending.value
    assert order.balance_due == Decimal("100.00")

    order = sales_order_service.sales_orders.update(
        db_session, str(order.id), SalesOrderUpdate(amount_paid=Decimal("40.00"))
    )
    assert order.payment_status == SalesOrderPaymentStatus.partial.value
    assert order.balance_due == Decimal("60.00")

    order = sales_order_service.sales_orders.update(
        db_session,
        str(order.id),
        SalesOrderUpdate(
            payment_status=SalesOrderPaymentStatus.paid,
            paid_at=datetime.now(UTC),
        ),
    )
    assert order.payment_status == SalesOrderPaymentStatus.paid.value
    assert order.amount_paid == Decimal("100.00")
    assert order.balance_due == Decimal("0.00")
    assert order.status == SalesOrderStatus.paid.value
    assert order.paid_at is not None


def test_waived_payment_confirms_draft_order(db_session):
    subscriber = _make_subscriber(db_session)
    order = sales_order_service.sales_orders.create(
        db_session, SalesOrderCreate(subscriber_id=subscriber.id)
    )
    assert order.status == SalesOrderStatus.draft.value
    order = sales_order_service.sales_orders.update(
        db_session,
        str(order.id),
        SalesOrderUpdate(payment_status=SalesOrderPaymentStatus.waived),
    )
    assert order.payment_status == SalesOrderPaymentStatus.waived.value
    assert order.status == SalesOrderStatus.confirmed.value


def test_update_from_input_parses_strings(db_session):
    subscriber = _make_subscriber(db_session)
    order = sales_order_service.sales_orders.create(
        db_session, SalesOrderCreate(subscriber_id=subscriber.id)
    )
    updated = sales_order_service.sales_orders.update_from_input(
        db_session,
        str(order.id),
        payment_status="paid",
        total="250.00",
        notes="  settled in cash  ",
    )
    assert updated.total == Decimal("250.00")
    assert updated.payment_status == SalesOrderPaymentStatus.paid.value
    assert updated.notes == "settled in cash"


# ---------------------------------------------------------------------------
# §2.3 — native financial side-effects
# ---------------------------------------------------------------------------


def test_paid_order_pushes_subscription_then_payment(db_session, billing_calls):
    subscriber = _make_subscriber(db_session)
    offer_id = str(uuid.uuid4())
    order = sales_order_service.sales_orders.create(
        db_session, SalesOrderCreate(subscriber_id=subscriber.id)
    )
    line = sales_order_service.sales_order_lines.create(
        db_session,
        SalesOrderLineCreate(
            sales_order_id=order.id,
            description="Monthly plan",
            quantity=Decimal("1"),
            unit_price=Decimal("25000.00"),
            metadata_={"sub_offer_id": offer_id},
        ),
    )
    assert billing_calls == []  # pending order: no financial side-effects yet

    order = sales_order_service.sales_orders.update(
        db_session,
        str(order.id),
        SalesOrderUpdate(
            payment_status=SalesOrderPaymentStatus.paid,
            paid_at=datetime.now(UTC),
        ),
    )

    names = [name for name, _ in billing_calls]
    # Subscription (plus its first invoice) BEFORE the payment, so a single
    # account-level payment settles everything (§2.3).
    assert names == ["create_subscription", "record_external_payment"]

    sub_kwargs = billing_calls[0][1]
    assert sub_kwargs["subscriber_id"] == str(subscriber.id)
    assert sub_kwargs["offer_ref"] == offer_id
    # The idempotency keys are byte-identical to the HTTP era.
    assert sub_kwargs["external_ref"] == (
        f"sales_order:{order.id}:subscription:{line.id}"
    )

    pay_kwargs = billing_calls[1][1]
    assert pay_kwargs["subscriber_id"] == str(subscriber.id)
    assert pay_kwargs["external_ref"] == f"sales_order:{order.id}:payment"
    assert Decimal(str(pay_kwargs["amount"])) == order.amount_paid

    # The resolved ids are written back onto the line metadata (§1.5 keys).
    db_session.refresh(line)
    assert (line.metadata_ or {}).get("selfcare_subscription_id")
    assert (line.metadata_ or {}).get("selfcare_subscription_invoice_id")

    # Re-recording the payment evidence is idempotent server-side on the
    # external_ref. Full consumer replay with already-tagged lines is
    # exercised in tests/test_sales_lifecycle_chain.py with a persisted
    # subscription; the mocked one here is not in the database.
    billing_calls.clear()
    sales_order_service._record_sales_order_payment(db_session, order)
    assert [name for name, _ in billing_calls] == ["record_external_payment"]


def test_partial_order_records_money_without_creating_service(
    db_session, billing_calls
):
    subscriber = _make_subscriber(db_session)
    order = sales_order_service.sales_orders.create(
        db_session,
        SalesOrderCreate(subscriber_id=subscriber.id, total=Decimal("100.00")),
    )
    sales_order_service.sales_order_lines.create(
        db_session,
        SalesOrderLineCreate(
            sales_order_id=order.id,
            description="Monthly plan",
            metadata_={"sub_offer_id": str(uuid.uuid4())},
        ),
    )

    sales_order_service.sales_orders.update(
        db_session,
        str(order.id),
        SalesOrderUpdate(amount_paid=Decimal("40.00")),
    )

    assert [name for name, _kwargs in billing_calls] == ["record_external_payment"]
    assert db_session.query(Subscription).count() == 0
    assert db_session.query(ServiceOrder).count() == 0


def test_closed_sales_line_stages_one_bound_provisioning_order(
    db_session, catalog_offer
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
            metadata_={"sub_offer_id": str(catalog_offer.id)},
        ),
    )
    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=SubscriptionStatus.pending,
    )
    order.status = SalesOrderStatus.paid.value
    db_session.add(subscription)
    db_session.commit()

    sales_order_service._ensure_provisioning_order_for_sales_line(
        db_session,
        sales_order=order,
        line=line,
        subscription=subscription,
    )
    sales_order_service._ensure_provisioning_order_for_sales_line(
        db_session,
        sales_order=order,
        line=line,
        subscription=subscription,
    )

    service_orders = (
        db_session.query(ServiceOrder)
        .filter(ServiceOrder.sales_order_line_id == line.id)
        .all()
    )
    assert len(service_orders) == 1
    assert service_orders[0].subscription_id == subscription.id
    assert service_orders[0].status == ServiceOrderStatus.draft
    assert service_orders[0].project_id is not None
    assert service_orders[0].installation_project_id is not None
    assert service_orders[0].idempotency_key == (
        f"sales-order-line:{line.id}:new_install"
    )
    assert service_orders[0].execution_context["catalog_offer_id"] == str(
        catalog_offer.id
    )
    assert (
        service_orders[0].execution_context["device_intent"]["desired_config"][
            "wan.mode"
        ]
        == "pppoe"
    )
    staged_context = service_orders[0].execution_context
    staged_desired = staged_context["device_intent"]["desired_config"]
    assert "wan.static_ip" not in staged_desired
    assert staged_context["bng_intent"]["subscription_id"] == str(subscription.id)
    assert staged_context["bng_intent"]["ipv4"] == {
        "source": "ipam",
        "assignment_scope": "subscription",
        "nat_policy": "pool_defined",
    }
    assert staged_context["bng_intent"]["additional_routes"] == {
        "source": "subscription_add_ons",
        "radius_attribute": "Framed-Route",
        "nat_policy": "no_nat",
    }

    from app.models.catalog import AccessCredential
    from app.models.network import OLTDevice, OntSyncStatus, OntUnit, PonPort
    from app.services import network as network_service
    from app.services.network.ont_desired_config import desired_config

    credential_count = (
        db_session.query(AccessCredential)
        .filter(AccessCredential.subscription_id == subscription.id)
        .count()
    )
    assert credential_count == 1
    olt = OLTDevice(name="Order staged OLT", is_active=True)
    db_session.add(olt)
    db_session.flush()
    pon = PonPort(olt_id=olt.id, name="0/1/1", is_active=True)
    ont = OntUnit(
        serial_number="ORDER-STAGED-ONT",
        olt_device_id=olt.id,
        is_active=True,
    )
    db_session.add_all([pon, ont])
    db_session.commit()
    network_service.ont_assignment_commands.assign(
        db_session,
        ont_unit_id=ont.id,
        subscriber_id=subscriber.id,
        subscription_id=subscription.id,
        pon_port_id=pon.id,
        source="test",
    )

    db_session.refresh(ont)
    # The sales owner stages intent but does not write the ONT control plane.
    # Provisioning consumes this only after implementation verification.
    assert desired_config(ont) == {}
    assert ont.sync_status == OntSyncStatus.synced


def test_staged_order_binds_seeded_fiber_activation_gate_task(
    db_session, catalog_offer
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
            metadata_={"sub_offer_id": str(catalog_offer.id)},
        ),
    )
    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=SubscriptionStatus.pending,
    )
    order.status = SalesOrderStatus.paid.value
    db_session.add(subscription)
    db_session.commit()

    sales_order_service._ensure_provisioning_order_for_sales_line(
        db_session,
        sales_order=order,
        line=line,
        subscription=subscription,
    )

    staged = (
        db_session.query(ServiceOrder)
        .filter(ServiceOrder.sales_order_line_id == line.id)
        .one()
    )
    project = db_session.get(Project, staged.project_id)
    assert project.project_type == "fiber_optics_installation"
    gate_tasks = [
        task
        for task in db_session.query(ProjectTask)
        .filter(
            ProjectTask.project_id == staged.project_id,
            ProjectTask.is_active.is_(True),
        )
        .all()
        if (task.metadata_ or {}).get("fiber_stage_key") == "power_splicing_activation"
    ]
    assert len(gate_tasks) == 1
    assert staged.activation_project_task_id == gate_tasks[0].id


def test_staged_order_without_stage_tasks_leaves_activation_gate_unbound(
    db_session, catalog_offer
):
    subscriber = _make_subscriber(db_session)
    order = sales_order_service.sales_orders.create(
        db_session,
        SalesOrderCreate(
            subscriber_id=subscriber.id,
            metadata_={"project_type": "cross_connect"},
        ),
    )
    line = sales_order_service.sales_order_lines.create(
        db_session,
        SalesOrderLineCreate(
            sales_order_id=order.id,
            description="Cross connect service",
            metadata_={"sub_offer_id": str(catalog_offer.id)},
        ),
    )
    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=SubscriptionStatus.pending,
    )
    order.status = SalesOrderStatus.paid.value
    db_session.add(subscription)
    db_session.commit()

    sales_order_service._ensure_provisioning_order_for_sales_line(
        db_session,
        sales_order=order,
        line=line,
        subscription=subscription,
    )

    staged = (
        db_session.query(ServiceOrder)
        .filter(ServiceOrder.sales_order_line_id == line.id)
        .one()
    )
    project = db_session.get(Project, staged.project_id)
    assert project.project_type == "cross_connect"
    assert (
        db_session.query(ProjectTask)
        .filter(ProjectTask.project_id == staged.project_id)
        .count()
        == 0
    )
    assert staged.activation_project_task_id is None


def test_sales_ip_addon_allocates_subscription_scoped_route(
    db_session, subscriber, catalog_offer
):
    from app.models.catalog import (
        AddOn,
        AddOnPrice,
        AddOnType,
        OfferAddOn,
        PriceType,
        SubscriptionAddOn,
    )
    from app.models.network import IpPool, IPVersion, SubscriberAdditionalRoute

    order = sales_order_service.sales_orders.create(
        db_session, SalesOrderCreate(subscriber_id=subscriber.id)
    )
    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=SubscriptionStatus.pending,
    )
    add_on = AddOn(
        name="Public /29",
        addon_type=AddOnType.extra_ip,
        ip_is_public=True,
        ip_prefix_length=29,
        is_active=True,
    )
    pool = IpPool(
        name="Sales add-on public pool",
        ip_version=IPVersion.ipv4,
        cidr="203.0.113.0/24",
        is_active=True,
    )
    db_session.add_all([subscription, add_on, pool])
    db_session.flush()
    db_session.add_all(
        [
            OfferAddOn(offer_id=catalog_offer.id, add_on_id=add_on.id),
            AddOnPrice(
                add_on_id=add_on.id,
                price_type=PriceType.recurring,
                amount=Decimal("5000.00"),
                is_active=True,
            ),
        ]
    )
    line = sales_order_service.sales_order_lines.create(
        db_session,
        SalesOrderLineCreate(
            sales_order_id=order.id,
            description="Public /29",
            quantity=Decimal("1"),
            metadata_={
                "add_on_id": str(add_on.id),
                "subscription_id": str(subscription.id),
            },
        ),
    )

    sales_order_service._sync_sales_order_add_ons(
        db_session,
        lines=[line],
        subscriptions=[subscription],
    )

    link = (
        db_session.query(SubscriptionAddOn)
        .filter(SubscriptionAddOn.subscription_id == subscription.id)
        .one()
    )
    route = (
        db_session.query(SubscriberAdditionalRoute)
        .filter(SubscriberAdditionalRoute.subscription_id == subscription.id)
        .one()
    )
    assert link.add_on_id == add_on.id
    assert route.cidr == "203.0.113.8/29"


def test_lines_without_offer_tags_push_no_subscription(db_session, billing_calls):
    subscriber = _make_subscriber(db_session)
    order = sales_order_service.sales_orders.create(
        db_session, SalesOrderCreate(subscriber_id=subscriber.id)
    )
    sales_order_service.sales_order_lines.create(
        db_session,
        SalesOrderLineCreate(
            sales_order_id=order.id,
            description="Installation fee",
            quantity=Decimal("1"),
            unit_price=Decimal("50000.00"),
        ),
    )
    sales_order_service.sales_orders.update(
        db_session,
        str(order.id),
        SalesOrderUpdate(
            payment_status=SalesOrderPaymentStatus.paid,
            paid_at=datetime.now(UTC),
        ),
    )
    names = [name for name, _ in billing_calls]
    assert "create_subscription" not in names
    assert "record_external_payment" in names


def test_installation_invoice_created_once_for_project(db_session, billing_calls):
    subscriber = _make_subscriber(db_session)
    order = sales_order_service.sales_orders.create(
        db_session, SalesOrderCreate(subscriber_id=subscriber.id)
    )
    # SalesOrder creation establishes the structural implementation project,
    # so the installation line can invoice against it immediately.
    sales_order_service.sales_order_lines.create(
        db_session,
        SalesOrderLineCreate(
            sales_order_id=order.id,
            description="Fiber installation",
            quantity=Decimal("1"),
            unit_price=Decimal("80000.00"),
        ),
    )
    assert len(billing_calls) == 1
    project = db_session.query(Project).filter(Project.sales_order_id == order.id).one()
    name, kwargs = billing_calls[0]
    assert name == "create_installation_invoice"
    assert kwargs["subscriber_id"] == str(subscriber.id)
    assert kwargs["amount"] == Decimal("80000.00")
    # The idempotency key keeps its HTTP-era shape (§2.3).
    assert kwargs["external_ref"] == f"project:{project.id}"

    db_session.refresh(project)
    metadata = project.metadata_ or {}
    assert metadata.get("selfcare_installation_invoice_id")
    assert metadata.get("selfcare_installation_invoice_amount") == "80000.00"

    # Second trigger is a no-op — the stored metadata dedups it.
    billing_calls.clear()
    sales_order_service.ensure_installation_invoice_for_sales_order(
        db_session, order.id
    )
    assert billing_calls == []


def test_installation_amount_falls_back_to_quote_lines(db_session, billing_calls):
    subscriber = _make_subscriber(db_session)
    quote = _make_quote(db_session, subscriber)
    sales_service.quote_line_items.create(
        db_session,
        QuoteLineItemCreate(
            quote_id=quote.id,
            description="Installation (survey inclusive)",
            quantity=Decimal("1"),
            unit_price=Decimal("60000.00"),
        ),
    )
    sales_service.quotes.update(
        db_session,
        str(quote.id),
        QuoteUpdate(status=QuoteStatus.accepted),
    )
    order = db_session.query(SalesOrder).filter_by(quote_id=quote.id).one()

    billing_calls.clear()
    sales_order_service.ensure_installation_invoice_for_sales_order(
        db_session, order.id
    )
    assert len(billing_calls) == 1
    assert billing_calls[0][1]["amount"] == Decimal("60000.00")
