"""Owner-output chain behavior for the network outage lifecycle.

Every incident transition stages its typed outage event atomically with the
status write; the registered ``OutageLifecycleProjectionHandler`` applies the
cross-owner consequences after commit with durable retry. These tests assert
the durable staging, replay idempotency, failed-delivery visibility, and the
hard boundary that outage resolution never closes support Tickets or
WorkOrders.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.models.event_store import EventStatus, EventStore
from app.models.network_monitoring import NetworkDevice
from app.models.operational_escalation import (
    OperationalEntityType,
    OperationalEscalationEvent,
    OperationalNotificationChannel,
    OperationalOwner,
    OperationalWatcher,
)
from app.models.service_team import ServiceTeam, ServiceTeamType
from app.models.support import Ticket
from app.models.work_order import WorkOrder
from app.services import operational_escalation
from app.services.events.handlers.outage_lifecycle_projection import (
    OutageLifecycleProjectionHandler,
)
from app.services.events.types import Event, EventType
from app.services.topology.outage import (
    confirm_incident,
    declare_outage,
    open_classifier_incident,
    resolve_classifier_incident,
)

NOW = datetime(2026, 7, 27, 9, 0, tzinfo=UTC)


def _node(db) -> NetworkDevice:
    node = NetworkDevice(name="Chain OLT", is_active=True)
    db.add(node)
    db.flush()
    return node


def _seed_teams(db) -> None:
    for name, team_type in (
        ("NOC", ServiceTeamType.operations.value),
        ("Support", ServiceTeamType.support.value),
        ("Field", ServiceTeamType.field_service.value),
    ):
        db.add(ServiceTeam(name=name, team_type=team_type))
    db.flush()


def _policy(db) -> None:
    # Threshold on affected count: classifier incidents carry no severity.
    operational_escalation.create_policy(
        db,
        name="Outage internal channels",
        entity_type=OperationalEntityType.outage,
        channels=[OperationalNotificationChannel.email],
        min_affected_customers=10,
    )


def _events(db, event_type: str) -> list[EventStore]:
    return (
        db.execute(select(EventStore).where(EventStore.event_type == event_type))
        .scalars()
        .all()
    )


def _confirmed_event_for(incident) -> Event:
    return Event(
        event_type=EventType.outage_confirmed,
        payload={"incident_id": str(incident.id)},
    )


def test_transition_outputs_commit_atomically_and_apply_consequences(db_session):
    _seed_teams(db_session)
    _policy(db_session)
    node = _node(db_session)

    incident = declare_outage(
        db_session,
        node=node,
        severity="high",
        impact={"count": 20},
    )
    # Staged in the declare transaction, visible before commit.
    staged = _events(db_session, "outage.created")
    assert len(staged) == 1
    assert staged[0].status == EventStatus.pending
    assert staged[0].payload["incident_id"] == str(incident.id)
    # The legacy webhook fan-out stages alongside the typed output.
    assert len(_events(db_session, "network.alert")) == 1
    # Consequences have not run inside the owner's transaction.
    assert db_session.query(OperationalOwner).count() == 0

    db_session.commit()

    delivered = _events(db_session, "outage.created")[0]
    assert delivered.status == EventStatus.completed
    assert db_session.query(OperationalOwner).count() == 1
    assert db_session.query(OperationalEscalationEvent).count() == 1
    # The consumer effect committed atomically with its unique receipt.
    from app.models.owner_output import OwnerOutputReceipt

    receipt = (
        db_session.query(OwnerOutputReceipt)
        .filter(
            OwnerOutputReceipt.consumer == "network.outage_lifecycle",
            OwnerOutputReceipt.event_id == delivered.event_id,
        )
        .one()
    )
    assert receipt.outcome.value == "succeeded"


def test_replaying_the_consequence_is_idempotent(db_session):
    _seed_teams(db_session)
    _policy(db_session)
    node = _node(db_session)
    incident = open_classifier_incident(
        db_session, root_node=node, affected_count=20, now=NOW
    )
    confirm_incident(db_session, incident, now=NOW)
    db_session.commit()

    owners = db_session.query(OperationalOwner).count()
    watchers = db_session.query(OperationalWatcher).count()
    escalations = db_session.query(OperationalEscalationEvent).count()
    assert owners == 1 and escalations == 1

    OutageLifecycleProjectionHandler().handle(
        db_session, _confirmed_event_for(incident)
    )
    db_session.flush()

    assert db_session.query(OperationalOwner).count() == owners
    assert db_session.query(OperationalWatcher).count() == watchers
    assert db_session.query(OperationalEscalationEvent).count() == escalations


def test_failed_consequence_stays_failed_and_visible(db_session, monkeypatch):
    _seed_teams(db_session)
    _policy(db_session)
    node = _node(db_session)
    from app.services.topology import outage_operations

    def _boom(*args, **kwargs):
        raise RuntimeError("escalation owner unavailable")

    monkeypatch.setattr(outage_operations, "plan_outage_escalations", _boom)

    declare_outage(db_session, node=node, severity="high", impact={"count": 20})
    db_session.commit()

    event = _events(db_session, "outage.created")[0]
    assert event.status == EventStatus.failed
    failed = [item.get("handler") for item in (event.failed_handlers or [])]
    assert "OutageLifecycleProjectionHandler" in failed
    assert db_session.query(OperationalEscalationEvent).count() == 0


def test_stale_replay_after_termination_plans_nothing(db_session):
    _seed_teams(db_session)
    _policy(db_session)
    node = _node(db_session)
    incident = open_classifier_incident(
        db_session, root_node=node, affected_count=20, now=NOW
    )
    confirm_incident(db_session, incident, now=NOW)
    db_session.commit()
    resolve_classifier_incident(db_session, incident, now=NOW)
    db_session.commit()

    OutageLifecycleProjectionHandler().handle(
        db_session, _confirmed_event_for(incident)
    )
    db_session.flush()

    # No fresh escalation appears for a terminated incident: everything
    # remains in its canceled state.
    from app.models.operational_escalation import OperationalEscalationStatus

    events = db_session.query(OperationalEscalationEvent).all()
    assert len(events) == 1
    assert events[0].status == OperationalEscalationStatus.canceled


def test_resolution_never_closes_tickets_or_work_orders(db_session, subscriber):
    _seed_teams(db_session)
    node = _node(db_session)
    incident = open_classifier_incident(
        db_session, root_node=node, affected_count=20, now=NOW
    )
    confirm_incident(db_session, incident, now=NOW)
    ticket = Ticket(
        subscriber_id=subscriber.id,
        title="No internet during outage",
    )
    work_order = WorkOrder(
        subscriber_id=subscriber.id,
        title="Check drop cable",
        requires_as_built_evidence=False,
    )
    db_session.add_all([ticket, work_order])
    db_session.commit()
    ticket_status = ticket.status
    work_order_status = work_order.status

    resolve_classifier_incident(db_session, incident, now=NOW)
    db_session.commit()

    # Resolution emits recovery evidence only; Support and Field owners
    # transition their own cases.
    db_session.refresh(ticket)
    db_session.refresh(work_order)
    assert ticket.status == ticket_status
    assert work_order.status == work_order_status
    resolved_events = _events(db_session, "outage.resolved")
    assert len(resolved_events) == 1
    assert resolved_events[0].status == EventStatus.completed


@pytest.mark.parametrize(
    "kind",
    ["outage.created", "outage.confirmed", "outage.discarded", "outage.resolved"],
)
def test_typed_lifecycle_event_types_exist(kind):
    assert EventType(kind).value == kind
