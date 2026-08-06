from __future__ import annotations

import uuid

import pytest

from app.models.audit import AuditEvent
from app.models.network import OLTDevice, OntAssignment, OntUnit, PonPort
from app.services import network as network_service
from app.services.network.ont_assignment_commands import OntAssignmentCommandError


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
