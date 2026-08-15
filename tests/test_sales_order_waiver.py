"""An order waiver is a decision, and it is never a payment.

Closing the manufacture-funding hole refused `payment_status` on the generic
order edit, which also removed the only path to a waiver — waiver travelled on
the same field as settlement. This owner is the replacement.

The tests are organised around the two halves that must both hold: the waiver
is accountable evidence, and it never becomes funding.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.audit import AuditEvent
from app.models.event_store import EventStore
from app.models.party import Party
from app.models.sales import (
    SalesOrder,
    SalesOrderLine,
    SalesOrderPaymentStatus,
    SalesOrderStatus,
)
from app.models.sales_order_waiver import SalesOrderWaiver, WaiverState
from app.models.subscriber import Subscriber
from app.schemas.sales_order import (
    SalesOrderCreate,
    SalesOrderLineCreate,
    SalesOrderLineUpdate,
    SalesOrderUpdate,
)
from app.services import sales_orders as sales_order_service
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext
from app.services.sales_orders import (
    AUDIT_WAIVER_GRANTED,
    AUDIT_WAIVER_REVOKED,
    SalesOrderWaivers,
    active_waiver,
)

_FUNDING_EVENT = "sales_order.funding_satisfied"


def _make_subscriber(db) -> Subscriber:
    party = Party(display_name="Waiver Case", party_type="person", status="active")
    db.add(party)
    db.flush()
    subscriber = Subscriber(
        first_name="Waiver",
        last_name="Case",
        email=f"waiver-{uuid.uuid4().hex}@example.com",
        party_id=party.id,
        party_bound_at=datetime.now(UTC),
        party_binding_source="pytest",
        party_binding_reason="Waiver fixture",
    )
    db.add(subscriber)
    db.commit()
    db.refresh(subscriber)
    return subscriber


def _order(db, subscriber) -> SalesOrder:
    order = sales_order_service.sales_orders.create(
        db,
        SalesOrderCreate(
            subscriber_id=subscriber.id,
            status=SalesOrderStatus.confirmed,
            subtotal=Decimal("1000.00"),
            total=Decimal("1000.00"),
        ),
    )
    # `create` commits, which expires the instance; the first attribute access
    # then autobegins a read transaction. `execute_owner_command` requires a
    # transaction-free session at entry and fails closed, so touch the
    # attributes here and release the transaction before any command runs —
    # the same thing `db_session_adapter.release_read_transaction` does for a
    # route.
    _ = (order.id, order.currency, order.total)
    db.commit()
    return order


def _ctx(key: str, actor: str = "user:ada") -> CommandContext:
    return CommandContext.system(
        actor=actor, scope="test.waiver", reason="goodwill", idempotency_key=key
    )


def _grant(db, order, *, amount="1000.00", reason="goodwill", key="waive-key-0001"):
    db.commit()  # release any read transaction opened by prior assertions
    return SalesOrderWaivers.grant(
        db,
        sales_order_id=order.id,
        waived_amount=Decimal(amount),
        reason_code=reason,
        context=_ctx(key),
    )


def _funding_events(db, sales_order_id) -> list[EventStore]:
    rows = (
        db.execute(select(EventStore).where(EventStore.event_type == _FUNDING_EVENT))
        .scalars()
        .all()
    )
    return [
        r
        for r in rows
        if (r.payload or {}).get("sales_order_id") == str(sales_order_id)
    ]


# --------------------------------------------------------------------------
# The waiver is accountable evidence
# --------------------------------------------------------------------------


def test_a_waiver_records_who_decided_what_and_why(db_session):
    subscriber = _make_subscriber(db_session)
    order = _order(db_session, subscriber)

    waiver = _grant(db_session, order)

    assert waiver.state is WaiverState.active
    assert waiver.granted_by == "user:ada"
    assert waiver.granted_at is not None
    assert waiver.waived_amount == Decimal("1000.00")
    assert waiver.currency == order.currency
    assert waiver.reason_code == "goodwill"
    assert waiver.grant_fingerprint


def test_a_grant_writes_an_audit_action(db_session):
    subscriber = _make_subscriber(db_session)
    order = _order(db_session, subscriber)
    _grant(db_session, order)

    actions = [
        e.action
        for e in db_session.query(AuditEvent)
        .filter(AuditEvent.entity_id == str(order.id))
        .all()
    ]
    assert AUDIT_WAIVER_GRANTED in actions


def test_grounds_must_be_registered(db_session):
    subscriber = _make_subscriber(db_session)
    order = _order(db_session, subscriber)

    with pytest.raises(DomainError) as exc:
        _grant(db_session, order, reason="because_i_said_so")
    assert exc.value.code.endswith("unregistered_reason_code")


def test_a_waiver_requires_an_idempotency_identity(db_session):
    subscriber = _make_subscriber(db_session)
    order = _order(db_session, subscriber)

    with pytest.raises(DomainError) as exc:
        SalesOrderWaivers.grant(
            db_session,
            sales_order_id=order.id,
            waived_amount=Decimal("10.00"),
            reason_code="goodwill",
            context=CommandContext.system(
                actor="user:ada", scope="test", reason="goodwill"
            ),
        )
    assert exc.value.code.endswith("missing_idempotency_key")


def test_a_non_positive_amount_is_refused(db_session):
    subscriber = _make_subscriber(db_session)
    order = _order(db_session, subscriber)

    with pytest.raises(DomainError) as exc:
        _grant(db_session, order, amount="0.00")
    assert exc.value.code.endswith("non_positive_amount")


# --------------------------------------------------------------------------
# Idempotency: waive once
# --------------------------------------------------------------------------


def test_replaying_a_grant_waives_once(db_session):
    subscriber = _make_subscriber(db_session)
    order = _order(db_session, subscriber)

    first = _grant(db_session, order)
    second = _grant(db_session, order)

    assert first.id == second.id
    assert (
        db_session.query(SalesOrderWaiver).filter_by(sales_order_id=order.id).count()
        == 1
    )


def test_the_same_key_with_different_inputs_conflicts(db_session):
    """Silently returning the original would tell the caller its larger waiver
    had been applied when it had not."""
    subscriber = _make_subscriber(db_session)
    order = _order(db_session, subscriber)
    _grant(db_session, order, amount="100.00")

    with pytest.raises(DomainError) as exc:
        _grant(db_session, order, amount="900.00")
    assert exc.value.code.endswith("idempotency_conflict")


def test_a_second_active_waiver_is_refused(db_session):
    subscriber = _make_subscriber(db_session)
    order = _order(db_session, subscriber)
    _grant(db_session, order, key="waive-key-0001")

    with pytest.raises(DomainError) as exc:
        _grant(db_session, order, key="waive-key-0002")
    assert exc.value.code.endswith("waiver_already_active")


# --------------------------------------------------------------------------
# Revocation
# --------------------------------------------------------------------------


def test_revocation_reopens_the_order(db_session):
    subscriber = _make_subscriber(db_session)
    order = _order(db_session, subscriber)
    _grant(db_session, order)

    db_session.commit()
    revoked = SalesOrderWaivers.revoke(
        db_session,
        sales_order_id=order.id,
        reason_code="granted_in_error",
        context=_ctx("revoke-key-0001", actor="user:grace"),
    )

    assert revoked.state is WaiverState.revoked
    assert revoked.revoked_by == "user:grace"
    assert revoked.revoke_reason_code == "granted_in_error"
    assert active_waiver(db_session, order.id) is None

    actions = [
        e.action
        for e in db_session.query(AuditEvent)
        .filter(AuditEvent.entity_id == str(order.id))
        .all()
    ]
    assert AUDIT_WAIVER_REVOKED in actions


def test_revoking_twice_is_idempotent(db_session):
    subscriber = _make_subscriber(db_session)
    order = _order(db_session, subscriber)
    _grant(db_session, order)

    db_session.commit()
    first = SalesOrderWaivers.revoke(
        db_session,
        sales_order_id=order.id,
        reason_code="granted_in_error",
        context=_ctx("revoke-key-0001"),
    )
    db_session.commit()
    second = SalesOrderWaivers.revoke(
        db_session,
        sales_order_id=order.id,
        reason_code="granted_in_error",
        context=_ctx("revoke-key-0001"),
    )
    assert first.id == second.id


def test_revoking_without_a_waiver_is_refused(db_session):
    subscriber = _make_subscriber(db_session)
    order = _order(db_session, subscriber)

    order_id = order.id
    db_session.commit()

    with pytest.raises(DomainError) as exc:
        SalesOrderWaivers.revoke(
            db_session,
            sales_order_id=order_id,
            reason_code="granted_in_error",
            context=_ctx("revoke-key-0001"),
        )
    assert exc.value.code.endswith("no_active_waiver")


def test_a_new_waiver_is_allowed_after_revocation(db_session):
    subscriber = _make_subscriber(db_session)
    order = _order(db_session, subscriber)
    _grant(db_session, order, key="waive-key-0001")
    db_session.commit()
    SalesOrderWaivers.revoke(
        db_session,
        sales_order_id=order.id,
        reason_code="superseded",
        context=_ctx("revoke-key-0001"),
    )

    again = _grant(db_session, order, key="waive-key-0002")
    assert again.state is WaiverState.active


# --------------------------------------------------------------------------
# It is never a payment
# --------------------------------------------------------------------------


def test_a_waiver_touches_no_payment_field_and_stages_no_funding(db_session):
    """The core guarantee. A waived order was not paid.

    `funding_satisfied` is what the lifecycle projection consumes to create the
    subscription and provisioning order, so its absence is what proves no
    service can follow from a waiver.
    """
    subscriber = _make_subscriber(db_session)
    order = _order(db_session, subscriber)
    before = (order.payment_status, order.amount_paid, order.paid_at)

    _grant(db_session, order)

    db_session.refresh(order)
    assert (order.payment_status, order.amount_paid, order.paid_at) == before
    assert order.payment_status == SalesOrderPaymentStatus.pending.value
    assert _funding_events(db_session, order.id) == []


def test_the_generic_edit_still_refuses_waived(db_session):
    """`payment_status = waived` is not a way back in.

    Historical rows carrying `waived` stay readable, but nothing writes the
    value any more — the waiver table is the record.
    """
    subscriber = _make_subscriber(db_session)
    order = _order(db_session, subscriber)

    with pytest.raises(HTTPException) as exc:
        sales_order_service.sales_orders.update(
            db_session,
            str(order.id),
            SalesOrderUpdate(payment_status=SalesOrderPaymentStatus.waived),
        )
    assert exc.value.status_code == 422

    # No rollback: the refusal precedes any mutation, so nothing is pending and
    # a rollback would discard the committed order instead.
    db_session.expire_all()
    persisted = db_session.get(SalesOrder, order.id)
    assert persisted.payment_status == SalesOrderPaymentStatus.pending.value


def test_a_historical_waived_value_stays_readable(db_session):
    """Rows written by the old path are evidence of what happened and are not
    rewritten or migrated away."""
    subscriber = _make_subscriber(db_session)
    legacy = SalesOrder(
        subscriber_id=subscriber.id,
        order_number=f"SO-LEGACY-{uuid.uuid4().hex[:6]}",
        status=SalesOrderStatus.confirmed.value,
        payment_status=SalesOrderPaymentStatus.waived.value,
        total=Decimal("500.00"),
    )
    db_session.add(legacy)
    db_session.commit()

    fetched = db_session.get(SalesOrder, legacy.id)
    assert fetched.payment_status == SalesOrderPaymentStatus.waived.value


# --------------------------------------------------------------------------
# Commercial terms freeze while a waiver is active
# --------------------------------------------------------------------------


def test_an_active_waiver_freezes_the_order_total(db_session):
    """Re-pricing underneath a waiver would change what was forgiven with no
    new decision taken."""
    subscriber = _make_subscriber(db_session)
    order = _order(db_session, subscriber)
    _grant(db_session, order)

    with pytest.raises(HTTPException) as exc:
        sales_order_service.sales_orders.update(
            db_session, str(order.id), SalesOrderUpdate(total=Decimal("5000.00"))
        )
    assert exc.value.status_code == 409
    assert "waiver" in exc.value.detail.lower()
    db_session.expire_all()
    assert db_session.get(SalesOrder, order.id).total == Decimal("1000.00")


def test_an_active_waiver_freezes_line_edits(db_session):
    subscriber = _make_subscriber(db_session)
    order = _order(db_session, subscriber)
    line = sales_order_service.sales_order_lines.create(
        db_session,
        SalesOrderLineCreate(
            sales_order_id=order.id,
            description="Install",
            quantity=Decimal("1"),
            unit_price=Decimal("1000.00"),
        ),
    )
    _grant(db_session, order)

    with pytest.raises(HTTPException) as exc:
        sales_order_service.sales_order_lines.update(
            db_session, str(line.id), SalesOrderLineUpdate(unit_price=Decimal("1.00"))
        )
    assert exc.value.status_code == 409

    # No rollback between the two refusals. Both guards run before any
    # mutation, so nothing is pending — and a rollback here would discard the
    # committed order, so the second call would fail on a deleted instance
    # rather than on the guard it is meant to prove.
    with pytest.raises(HTTPException) as exc:
        sales_order_service.sales_order_lines.create(
            db_session,
            SalesOrderLineCreate(
                sales_order_id=order.id,
                description="Extra",
                quantity=Decimal("1"),
                unit_price=Decimal("50.00"),
            ),
        )
    assert exc.value.status_code == 409

    db_session.expire_all()
    assert db_session.get(SalesOrderLine, line.id).unit_price == Decimal("1000.00")


def test_non_commercial_edits_are_still_allowed_under_a_waiver(db_session):
    """Sensitivity proof for the freeze.

    Every freeze assertion above is a refusal, and a refusal test passes for
    the wrong reason if the update path stopped working. This proves an
    unrelated edit still succeeds while the waiver is active.
    """
    subscriber = _make_subscriber(db_session)
    order = _order(db_session, subscriber)
    _grant(db_session, order)

    updated = sales_order_service.sales_orders.update(
        db_session, str(order.id), SalesOrderUpdate(notes="Waived after outage")
    )
    assert updated.notes == "Waived after outage"


def test_the_freeze_lifts_after_revocation(db_session):
    subscriber = _make_subscriber(db_session)
    order = _order(db_session, subscriber)
    _grant(db_session, order)
    db_session.commit()
    SalesOrderWaivers.revoke(
        db_session,
        sales_order_id=order.id,
        reason_code="policy_change",
        context=_ctx("revoke-key-0001"),
    )

    updated = sales_order_service.sales_orders.update(
        db_session, str(order.id), SalesOrderUpdate(total=Decimal("1200.00"))
    )
    assert updated.total == Decimal("1200.00")
