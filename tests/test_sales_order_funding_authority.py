"""An operator cannot manufacture funding on a sales order.

``sales_order.funding_satisfied`` is the event that records the funded finance
gate. It is staged on the pending/partial -> paid edge, so
anything that can write ``payment_status``, ``amount_paid`` or ``paid_at`` can
manufacture a funded commercial state without money ever arriving.

The funding gate in ``app/services/sales_order_funding.py`` exists to stop
exactly that, and a generic sales-order edit used to route around it: the admin
form posted ``payment_status=paid``, ``SalesOrders.update`` promoted the order,
and ``stage_funding_transition`` emitted the event.

These tests pin the boundary from both sides — the forged path is refused, and
the authoritative path still works and still fires exactly once.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.event_store import EventStore
from app.models.party import Party
from app.models.sales import (
    SalesOrder,
    SalesOrderPaymentStatus,
    SalesOrderStatus,
)
from app.models.subscriber import Subscriber
from app.schemas.sales_order import SalesOrderCreate, SalesOrderUpdate
from app.services import sales_orders as sales_order_service
from app.services.sales_orders import FundingAuthority

_FUNDING_EVENT = "sales_order.funding_satisfied"


def _make_subscriber(db) -> Subscriber:
    party = Party(display_name="Funding Guard", party_type="person", status="active")
    db.add(party)
    db.flush()
    subscriber = Subscriber(
        first_name="Funding",
        last_name="Guard",
        email=f"funding-{uuid.uuid4().hex}@example.com",
        party_id=party.id,
        party_bound_at=datetime.now(UTC),
        party_binding_source="pytest",
        party_binding_reason="Funding authority fixture",
    )
    db.add(subscriber)
    db.commit()
    db.refresh(subscriber)
    return subscriber


def _unfunded_order(db, subscriber) -> SalesOrder:
    """An accepted, unpaid order — the state an operator would try to forge."""
    return sales_order_service.sales_orders.create(
        db,
        SalesOrderCreate(
            subscriber_id=subscriber.id,
            status=SalesOrderStatus.confirmed,
            subtotal=Decimal("1000.00"),
            total=Decimal("1000.00"),
        ),
    )


def _funding_events(db, sales_order_id) -> list[EventStore]:
    rows = (
        db.execute(select(EventStore).where(EventStore.event_type == _FUNDING_EVENT))
        .scalars()
        .all()
    )
    return [
        row
        for row in rows
        if (row.payload or {}).get("sales_order_id") == str(sales_order_id)
    ]


# --------------------------------------------------------------------------
# The bypass, refused
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "forged",
    [
        pytest.param(
            {
                "payment_status": SalesOrderPaymentStatus.paid,
                "paid_at": datetime.now(UTC),
            },
            id="payment_status_paid",
        ),
        pytest.param({"amount_paid": Decimal("1000.00")}, id="amount_paid"),
        pytest.param({"paid_at": datetime.now(UTC)}, id="paid_at"),
    ],
)
def test_operator_update_cannot_assert_coverage(db_session, forged):
    """Every funding field is refused on the generic edit, by name."""
    subscriber = _make_subscriber(db_session)
    order = _unfunded_order(db_session, subscriber)

    with pytest.raises(HTTPException) as exc:
        sales_order_service.sales_orders.update(
            db_session, str(order.id), SalesOrderUpdate(**forged)
        )

    assert exc.value.status_code == 422
    for field in forged:
        assert field in exc.value.detail

    # `assert_funding_authority` runs before `update` touches the order, so
    # there is nothing pending to roll back. Rolling back anyway dropped the
    # order the fixture had committed, leaving `refresh` with no row to read.
    # Expiring instead forces a fresh SELECT, which asserts on what is
    # persisted rather than on whatever the identity map still holds.
    order_id = order.id
    db_session.expire_all()
    persisted = db_session.get(SalesOrder, order_id)
    assert persisted is not None
    assert persisted.payment_status == SalesOrderPaymentStatus.pending.value
    assert _funding_events(db_session, order_id) == []


def test_forged_paid_update_creates_no_funding_event(db_session):
    """The consequence, not just the field: no funding event is ever staged.

    Proving ``funding_satisfied`` is absent proves the finance projection cannot
    accept a fabricated paid state.
    """
    subscriber = _make_subscriber(db_session)
    order = _unfunded_order(db_session, subscriber)
    before = len(_funding_events(db_session, order.id))

    with pytest.raises(HTTPException) as exc:
        sales_order_service.sales_orders.update(
            db_session,
            str(order.id),
            SalesOrderUpdate(
                payment_status=SalesOrderPaymentStatus.paid,
                paid_at=datetime.now(UTC),
                amount_paid=Decimal("1000.00"),
            ),
        )
    assert exc.value.status_code == 422

    # Same reason as above: the refusal happens before any mutation, so the
    # order is still committed and must be re-read, not rolled back.
    order_id = order.id
    db_session.expire_all()
    persisted = db_session.get(SalesOrder, order_id)
    assert persisted is not None
    assert persisted.payment_status == SalesOrderPaymentStatus.pending.value
    assert persisted.status != SalesOrderStatus.paid.value
    assert len(_funding_events(db_session, order_id)) == before


def test_operator_create_cannot_open_an_already_paid_order(db_session):
    """Creation is the same bypass one step earlier."""
    subscriber = _make_subscriber(db_session)

    with pytest.raises(HTTPException) as exc:
        sales_order_service.sales_orders.create(
            db_session,
            SalesOrderCreate(
                subscriber_id=subscriber.id,
                status=SalesOrderStatus.confirmed,
                payment_status=SalesOrderPaymentStatus.paid,
                subtotal=Decimal("500.00"),
                total=Decimal("500.00"),
                amount_paid=Decimal("500.00"),
                paid_at=datetime.now(UTC),
            ),
        )

    assert exc.value.status_code == 422
    assert "payment_status" in exc.value.detail
    db_session.rollback()


def test_operator_create_cannot_assert_paid_lifecycle_status(db_session):
    subscriber = _make_subscriber(db_session)

    with pytest.raises(HTTPException) as exc:
        sales_order_service.sales_orders.create(
            db_session,
            SalesOrderCreate(
                subscriber_id=subscriber.id,
                status=SalesOrderStatus.paid,
                subtotal=Decimal("500.00"),
                total=Decimal("500.00"),
            ),
        )

    assert exc.value.status_code == 422
    assert "paid" in exc.value.detail


def test_receipted_order_commercial_terms_are_immutable(db_session):
    subscriber = _make_subscriber(db_session)
    order = _unfunded_order(db_session, subscriber)
    order.payment_status = SalesOrderPaymentStatus.partial.value
    order.amount_paid = Decimal("250.00")
    order.balance_due = Decimal("750.00")
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        sales_order_service.sales_orders.update(
            db_session,
            str(order.id),
            SalesOrderUpdate(total=Decimal("1250.00")),
        )

    assert exc.value.status_code == 409
    assert "immutable commercial document" in exc.value.detail


def test_legacy_paid_label_cannot_be_edited_or_deleted_while_unsettled(db_session):
    subscriber = _make_subscriber(db_session)
    order = _unfunded_order(db_session, subscriber)
    order.status = SalesOrderStatus.paid.value
    db_session.commit()

    with pytest.raises(HTTPException) as update_exc:
        sales_order_service.sales_orders.update(
            db_session,
            str(order.id),
            SalesOrderUpdate(total=Decimal("1250.00")),
        )
    with pytest.raises(HTTPException) as delete_exc:
        sales_order_service.sales_orders.delete(db_session, str(order.id))

    assert update_exc.value.status_code == 409
    assert delete_exc.value.status_code == 409
    db_session.expire_all()
    persisted = db_session.get(SalesOrder, order.id)
    assert persisted.is_active is True
    assert persisted.payment_status == SalesOrderPaymentStatus.pending.value


@pytest.mark.parametrize(
    "status",
    [SalesOrderStatus.paid, SalesOrderStatus.fulfilled],
)
def test_operator_update_cannot_assert_evidence_controlled_status(db_session, status):
    subscriber = _make_subscriber(db_session)
    order = _unfunded_order(db_session, subscriber)

    with pytest.raises(HTTPException) as exc:
        sales_order_service.sales_orders.update(
            db_session,
            str(order.id),
            SalesOrderUpdate(status=status),
        )

    assert exc.value.status_code == 422
    assert status.value in exc.value.detail
    db_session.expire_all()
    persisted = db_session.get(SalesOrder, order.id)
    assert persisted.status == SalesOrderStatus.confirmed.value
    assert _funding_events(db_session, order.id) == []


def test_a_default_pending_create_is_not_treated_as_an_assertion(db_session):
    """The guard must not break ordinary order creation.

    ``SalesOrderCreate`` carries schema defaults for every funding field, so a
    guard that looked at values alone would refuse every create.
    """
    subscriber = _make_subscriber(db_session)
    order = _unfunded_order(db_session, subscriber)
    assert order.payment_status == SalesOrderPaymentStatus.pending.value


def test_web_form_path_carries_no_funding_fields(db_session):
    """``update_from_input`` is the admin form seam and rejects loudly.

    A caller left behind by this change gets a 422 naming the field, never a
    ``TypeError`` and never a silent drop — a silent drop would let an operator
    believe a payment had been recorded.
    """
    subscriber = _make_subscriber(db_session)
    order = _unfunded_order(db_session, subscriber)

    with pytest.raises(HTTPException) as exc:
        sales_order_service.sales_orders.update_from_input(
            db_session,
            str(order.id),
            status="confirmed",
            payment_status="paid",
        )

    assert exc.value.status_code == 422
    assert "payment_status" in exc.value.detail
    assert _funding_events(db_session, order.id) == []


def test_the_guard_actually_bites(db_session):
    """Sensitivity proof (ADR-0018).

    Every assertion above is a refusal, and a refusal test passes for the wrong
    reason if the guarded path stopped existing. This proves the SAME call
    succeeds once authority is supplied, so the tests above are measuring the
    guard and not a missing feature.
    """
    subscriber = _make_subscriber(db_session)
    order = _unfunded_order(db_session, subscriber)

    updated = sales_order_service.sales_orders.update(
        db_session,
        str(order.id),
        SalesOrderUpdate(
            payment_status=SalesOrderPaymentStatus.paid,
            paid_at=datetime.now(UTC),
            amount_paid=Decimal("1000.00"),
        ),
        funding_authority=FundingAuthority.settlement,
    )

    assert updated.payment_status == SalesOrderPaymentStatus.paid.value


# --------------------------------------------------------------------------
# The authoritative path, unbroken
# --------------------------------------------------------------------------


def test_accepted_settlement_still_funds_the_order_exactly_once(db_session):
    """Legitimate settlement evidence still stages the funding output — once.

    Guarding the operator surface must not weaken the real path, and must not
    make it fire twice: ``stage_funding_transition`` is edge-triggered on
    pending/partial -> paid, so a replay of the same settlement is a no-op.
    """
    subscriber = _make_subscriber(db_session)
    order = _unfunded_order(db_session, subscriber)

    sales_order_service.sales_orders.update(
        db_session,
        str(order.id),
        SalesOrderUpdate(
            payment_status=SalesOrderPaymentStatus.paid,
            paid_at=datetime.now(UTC),
            amount_paid=Decimal("1000.00"),
        ),
        funding_authority=FundingAuthority.settlement,
    )
    db_session.refresh(order)
    assert order.payment_status == SalesOrderPaymentStatus.paid.value
    assert len(_funding_events(db_session, order.id)) == 1

    # Replaying the same settlement does not fund the order a second time.
    sales_order_service.sales_orders.update(
        db_session,
        str(order.id),
        SalesOrderUpdate(
            payment_status=SalesOrderPaymentStatus.paid,
            paid_at=order.paid_at,
            amount_paid=Decimal("1000.00"),
        ),
        funding_authority=FundingAuthority.settlement,
    )
    assert len(_funding_events(db_session, order.id)) == 1


def test_derived_coverage_from_recorded_lines_is_not_an_operator_assertion(db_session):
    """Recalculation derives coverage; it never asserts new money.

    ``total`` stays operator-editable on purpose — changing what an order is
    worth is a real sales edit. What an operator cannot do is claim money
    arrived.
    """
    subscriber = _make_subscriber(db_session)
    order = _unfunded_order(db_session, subscriber)

    updated = sales_order_service.sales_orders.update(
        db_session, str(order.id), SalesOrderUpdate(total=Decimal("1500.00"))
    )

    assert updated.total == Decimal("1500.00")
    assert updated.payment_status == SalesOrderPaymentStatus.pending.value
    assert _funding_events(db_session, order.id) == []
