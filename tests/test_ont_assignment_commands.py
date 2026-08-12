from __future__ import annotations

import uuid
from uuid import uuid4

import pytest

from app.models.audit import AuditEvent
from app.models.catalog import SubscriptionStatus
from app.models.network import OLTDevice, OntAssignment, OntUnit, PonPort
from app.models.subscriber import SubscriberStatus
from app.services import network as network_service
from app.services.network.ont_assignment_commands import (
    OntAssignmentCommandError,
    ReassignActiveOntCommand,
)
from app.services.owner_commands import CommandContext


def _plant(db_session):
    suffix = uuid.uuid4().hex[:10]
    olt = OLTDevice(
        name=f"Command OLT {suffix}",
        hostname=f"command-olt-{suffix}",
        is_active=True,
    )
    db_session.add(olt)
    db_session.flush()
    first_pon = PonPort(olt_id=olt.id, name="0/2/1", is_active=True)
    second_pon = PonPort(olt_id=olt.id, name="0/2/2", is_active=True)
    ont = OntUnit(serial_number=f"COMMAND-{suffix}", is_active=False)
    db_session.add_all([first_pon, second_pon, ont])
    db_session.commit()
    return olt, first_pon, second_pon, ont


def _active_subscription(subscription, db_session):
    subscription.status = SubscriptionStatus.active
    subscription.subscriber.status = SubscriberStatus.active
    db_session.commit()
    return subscription


def _ctx(key: str = "reassign-test") -> CommandContext:
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor="operator@example.com",
        scope="test",
        reason="pytest",
        idempotency_key=key,
    )


def _target(db_session, olt, pon, serial: str = "TARGET") -> OntUnit:
    ont = OntUnit(
        serial_number=f"{serial}-{uuid.uuid4().hex[:8]}",
        mac_address="AA:BB:CC:12:34:56",
        is_active=True,
        olt_device_id=olt.id,
        pon_port_id=pon.id,
    )
    db_session.add(ont)
    db_session.commit()
    return ont


def _reassign(db_session, subscription, current, target):
    subscription_id = subscription.id
    subscriber_id = subscription.subscriber_id
    current_assignment_id = current.id
    target_ont_unit_id = target.id
    db_session.commit()
    return network_service.ont_assignment_commands.reassign_active_ont(
        db_session,
        command=ReassignActiveOntCommand(
            context=_ctx(f"{current_assignment_id}:{target_ont_unit_id}"),
            subscriber_id=subscriber_id,
            subscription_id=subscription_id,
            current_assignment_id=current_assignment_id,
            target_ont_unit_id=target_ont_unit_id,
        ),
    )


def test_exact_assignment_is_idempotent_and_records_exact_result(
    db_session, subscription
):
    olt, pon, _other_pon, ont = _plant(db_session)

    created = network_service.ont_assignment_commands.assign(
        db_session,
        ont_unit_id=ont.id,
        subscription_id=subscription.id,
        pon_port_id=pon.id,
        subscriber_id=subscription.subscriber_id,
        actor_id="operator@example.com",
        source="test",
    )
    replay = network_service.ont_assignment_commands.assign(
        db_session,
        ont_unit_id=ont.id,
        subscription_id=subscription.id,
        pon_port_id=pon.id,
        subscriber_id=subscription.subscriber_id,
        actor_id="operator@example.com",
        source="test",
    )

    assert created.action == "created"
    assert replay.replayed is True
    assert replay.assignment.id == created.assignment.id
    assert db_session.query(OntAssignment).count() == 1
    db_session.refresh(ont)
    assert ont.olt_device_id == olt.id
    assert ont.pon_port_id == pon.id
    assert ont.board == "0/2"
    assert ont.port == "1"
    assert created.assignment.subscription_id == subscription.id
    assert created.assignment.subscriber_id == subscription.subscriber_id
    audit = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "network.ont_assignment.assign")
        .order_by(AuditEvent.occurred_at.desc())
        .first()
    )
    assert audit is not None
    assert audit.metadata_["exact_result"]["assignment_id"] == str(
        created.assignment.id
    )
    assert audit.metadata_["exact_result"]["subscription_id"] == str(subscription.id)


def test_conflicting_ont_identity_fails_closed(db_session, subscription):
    olt, pon, other_pon, ont = _plant(db_session)
    ont.olt_device_id = olt.id
    ont.pon_port_id = other_pon.id
    db_session.commit()

    with pytest.raises(OntAssignmentCommandError, match="reviewed identity repair"):
        network_service.ont_assignment_commands.assign(
            db_session,
            ont_unit_id=ont.id,
            subscription_id=subscription.id,
            pon_port_id=pon.id,
        )

    assert db_session.query(OntAssignment).count() == 0


def test_move_to_pon_refuses_a_non_canonical_target(db_session, subscription):
    """The guard covered ``assign`` but not the physical move.

    ``move_to_pon`` loads its target PON directly instead of through
    ``_load_target``, so a prefixed row reached the point where board/port are
    written from the row's name -- the exact path that turns a name nobody
    chose into corrupt ONT inventory.
    """
    olt, first_pon, second_pon, ont = _plant(db_session)
    network_service.ont_assignment_commands.assign(
        db_session,
        ont_unit_id=ont.id,
        subscription_id=subscription.id,
        pon_port_id=first_pon.id,
    )
    second_pon.name = "pon-0/2/2"
    db_session.commit()

    with pytest.raises(OntAssignmentCommandError, match="pon_port_identity"):
        network_service.ont_assignment_commands.move_to_pon(
            db_session,
            ont_unit_id=ont.id,
            target_pon_port_id=second_pon.id,
            actor_id="operator@example.com",
        )

    db_session.refresh(ont)
    assert ont.pon_port_id == first_pon.id
    assert ont.board == "0/2"


def test_release_and_verified_move_delegate_to_same_owner(db_session, subscription):
    _olt, first_pon, second_pon, ont = _plant(db_session)
    created = network_service.ont_assignment_commands.assign(
        db_session,
        ont_unit_id=ont.id,
        subscription_id=subscription.id,
        pon_port_id=first_pon.id,
    )

    moved = network_service.ont_assignment_commands.move_to_pon(
        db_session,
        ont_unit_id=ont.id,
        target_pon_port_id=second_pon.id,
        actor_id="operator@example.com",
    )
    assert moved.assignment.id == created.assignment.id
    assert moved.assignment.pon_port_id == second_pon.id
    assert db_session.query(OntAssignment).count() == 1

    released = network_service.ont_assignment_commands.release(
        db_session,
        assignment_id=created.assignment.id,
        reason="normal_deprovision",
        actor_id="operator@example.com",
    )
    replay = network_service.ont_assignment_commands.release(
        db_session,
        assignment_id=created.assignment.id,
        reason="normal_deprovision",
        actor_id="operator@example.com",
    )
    assert released.assignment.active is False
    assert released.assignment.release_reason == "normal_deprovision"
    assert replay.replayed is True


def test_reassign_active_ont_releases_old_and_assigns_target(db_session, subscription):
    subscription = _active_subscription(subscription, db_session)
    olt, first_pon, second_pon, current_ont = _plant(db_session)
    current = network_service.ont_assignment_commands.assign(
        db_session,
        ont_unit_id=current_ont.id,
        subscription_id=subscription.id,
        pon_port_id=first_pon.id,
        subscriber_id=subscription.subscriber_id,
    ).assignment
    current.wifi_ssid = "Preserved"
    db_session.commit()
    target = _target(db_session, olt, second_pon)

    outcome = _reassign(db_session, subscription, current, target)

    db_session.refresh(current)
    db_session.refresh(target)
    assert current.active is False
    assert outcome.new_ont_unit_id == target.id
    assert outcome.olt_id == olt.id
    active = db_session.query(OntAssignment).filter_by(active=True).one()
    assert active.ont_unit_id == target.id
    assert active.subscription_id == subscription.id
    assert active.subscriber_id == subscription.subscriber_id
    assert active.wifi_ssid == "Preserved"
    assert target.olt_device_id == olt.id


def test_reassign_rejects_target_ont_already_assigned(db_session, subscription):
    subscription = _active_subscription(subscription, db_session)
    olt, first_pon, second_pon, current_ont = _plant(db_session)
    current = network_service.ont_assignment_commands.assign(
        db_session,
        ont_unit_id=current_ont.id,
        subscription_id=subscription.id,
        pon_port_id=first_pon.id,
        subscriber_id=subscription.subscriber_id,
    ).assignment
    target = _target(db_session, olt, second_pon)
    other_subscription = subscription
    target_assignment = OntAssignment(
        ont_unit_id=target.id,
        pon_port_id=second_pon.id,
        subscriber_id=other_subscription.subscriber_id,
        subscription_id=other_subscription.id,
        active=True,
    )
    db_session.add(target_assignment)
    db_session.commit()

    with pytest.raises(OntAssignmentCommandError, match="already actively assigned"):
        _reassign(db_session, subscription, current, target)


def test_reassign_rejects_stale_current_assignment(db_session, subscription):
    subscription = _active_subscription(subscription, db_session)
    olt, first_pon, second_pon, current_ont = _plant(db_session)
    current = network_service.ont_assignment_commands.assign(
        db_session,
        ont_unit_id=current_ont.id,
        subscription_id=subscription.id,
        pon_port_id=first_pon.id,
        subscriber_id=subscription.subscriber_id,
    ).assignment
    target = _target(db_session, olt, second_pon)
    network_service.ont_assignment_commands.release(
        db_session,
        assignment_id=current.id,
        reason="stale",
    )

    with pytest.raises(OntAssignmentCommandError, match="stale"):
        _reassign(db_session, subscription, current, target)


def test_reassign_rejects_inactive_subscription(db_session, subscription):
    subscription.subscriber.status = SubscriberStatus.active
    subscription.status = SubscriptionStatus.disabled
    db_session.commit()
    olt, first_pon, second_pon, current_ont = _plant(db_session)
    current = network_service.ont_assignment_commands.assign(
        db_session,
        ont_unit_id=current_ont.id,
        subscription_id=subscription.id,
        pon_port_id=first_pon.id,
        subscriber_id=subscription.subscriber_id,
    ).assignment
    target = _target(db_session, olt, second_pon)

    with pytest.raises(OntAssignmentCommandError, match="Subscription must be active"):
        _reassign(db_session, subscription, current, target)


def test_reassign_replay_is_idempotent(db_session, subscription):
    subscription = _active_subscription(subscription, db_session)
    olt, first_pon, second_pon, current_ont = _plant(db_session)
    current = network_service.ont_assignment_commands.assign(
        db_session,
        ont_unit_id=current_ont.id,
        subscription_id=subscription.id,
        pon_port_id=first_pon.id,
        subscriber_id=subscription.subscriber_id,
    ).assignment
    target = _target(db_session, olt, second_pon)

    first = _reassign(db_session, subscription, current, target)
    replay = _reassign(db_session, subscription, current, target)

    assert replay.replayed is True
    assert replay.new_assignment_id == first.new_assignment_id
    assert db_session.query(OntAssignment).filter_by(active=True).count() == 1


def test_reassign_rolls_back_old_release_when_new_assignment_fails(
    db_session, subscription, monkeypatch
):
    subscription = _active_subscription(subscription, db_session)
    olt, first_pon, second_pon, current_ont = _plant(db_session)
    current = network_service.ont_assignment_commands.assign(
        db_session,
        ont_unit_id=current_ont.id,
        subscription_id=subscription.id,
        pon_port_id=first_pon.id,
        subscriber_id=subscription.subscriber_id,
    ).assignment
    target = _target(db_session, olt, second_pon)
    original_assign = network_service.ont_assignment_commands.assign

    def fail_assign(*args, **kwargs):
        if kwargs.get("source") == "admin_subscription_change_ont":
            raise OntAssignmentCommandError("forced create failure")
        return original_assign(*args, **kwargs)

    monkeypatch.setattr(network_service.ont_assignment_commands, "assign", fail_assign)

    with pytest.raises(OntAssignmentCommandError, match="forced create failure"):
        _reassign(db_session, subscription, current, target)

    db_session.refresh(current)
    assert current.active is True
    assert db_session.query(OntAssignment).filter_by(active=True).count() == 1
