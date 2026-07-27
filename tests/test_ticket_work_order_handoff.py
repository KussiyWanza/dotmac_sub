from __future__ import annotations

from uuid import uuid4

import pytest

from app.models.audit import AuditEvent
from app.models.project import Project, ProjectTask
from app.models.service_team import ServiceTeam, ServiceTeamMember, ServiceTeamType
from app.models.subscriber import Subscriber
from app.models.support import Ticket
from app.models.work_order import WorkOrder
from app.schemas.dispatch import WorkOrderHeaderCreate, WorkOrderHeaderUpdate
from app.schemas.support import TicketWorkOrderIssueRequest
from app.services import ticket_work_order_handoff
from app.services.owner_commands import CommandContext
from tests.staff_identity_fixtures import add_bound_staff_user


def _subscriber(db_session, label: str = "Handoff") -> Subscriber:
    row = Subscriber(
        first_name=label,
        last_name="Customer",
        email=f"handoff-{uuid4().hex[:8]}@example.com",
    )
    db_session.add(row)
    db_session.flush()
    return row


def _ticket_with_team(db_session):
    subscriber = _subscriber(db_session)
    actor, person = add_bound_staff_user(db_session)
    actor_id = actor.id
    team = ServiceTeam(
        name="Field Response",
        team_type=ServiceTeamType.field_service.value,
        is_active=True,
    )
    db_session.add(team)
    db_session.flush()
    db_session.add(
        ServiceTeamMember(
            team_id=team.id,
            person_id=person.id,
            is_active=True,
        )
    )
    ticket = Ticket(
        number=f"TKT-{uuid4().hex[:6]}",
        title="Loss of signal",
        description="Subscriber has no optical signal",
        subscriber_id=subscriber.id,
        customer_account_id=subscriber.id,
        service_team_id=team.id,
        status="open",
        priority="high",
    )
    db_session.add(ticket)
    db_session.commit()
    return subscriber, team, actor_id, ticket


def _payload() -> TicketWorkOrderIssueRequest:
    return TicketWorkOrderIssueRequest(
        reason="Optical levels require an onsite trace",
        work_type="repair",
        tags=["fibre"],
    )


def _issue(
    db_session,
    *,
    ticket_id,
    actor_id,
    idempotency_key: str,
    payload: TicketWorkOrderIssueRequest | None = None,
):
    # Owner commands require a transaction-free adapter session. Test setup
    # commits above, so all identifiers are captured before entering it.
    command = ticket_work_order_handoff.TicketWorkOrderIssueCommand(
        ticket_id=ticket_id,
        request=payload or _payload(),
        actor_id=actor_id,
        actor_type=ticket_work_order_handoff.HandoffActorType.SYSTEM_USER,
        permissions=frozenset({"support:ticket:update", "operations:dispatch:write"}),
        context=CommandContext.system(
            actor=str(actor_id),
            scope=ticket_work_order_handoff.WORK_ORDER_ISSUE_SCOPE,
            reason="test ticket-to-work-order issuance",
            idempotency_key=idempotency_key,
        ),
    )
    return ticket_work_order_handoff.issue_work_order(db_session, command)


def test_assigned_team_member_issues_many_idempotent_work_orders(db_session):
    subscriber, team, actor_id, ticket = _ticket_with_team(db_session)
    ticket_id = ticket.id
    first = _issue(
        db_session,
        ticket_id=ticket_id,
        actor_id=actor_id,
        idempotency_key="scope-one",
    )
    replay = _issue(
        db_session,
        ticket_id=ticket_id,
        actor_id=actor_id,
        idempotency_key="scope-one",
    )
    second = _issue(
        db_session,
        ticket_id=ticket_id,
        actor_id=actor_id,
        payload=TicketWorkOrderIssueRequest(
            reason="A second feeder trace is required",
            title="Trace secondary feeder",
            work_type="survey",
        ),
        idempotency_key="scope-two",
    )

    assert first.replayed is False
    assert replay.replayed is True
    assert replay.work_order.id == first.work_order.id
    assert second.work_order.id != first.work_order.id
    linked = ticket_work_order_handoff.list_for_ticket(db_session, ticket.id)
    assert [row.id for row in linked] == [first.work_order.id, second.work_order.id]
    assert all(row.origin_ticket_id == ticket.id for row in linked)
    assert all(row.subscriber_id == subscriber.id for row in linked)
    assert all(row.crm_ticket_id is None for row in linked)
    assert "work_order_id" not in (ticket.metadata_ or {})
    assert "ticket_id" not in (first.work_order.metadata_ or {})
    assert (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "ticket.work_order_issued")
        .filter(AuditEvent.entity_id == str(ticket.id))
        .count()
        == 2
    )
    assert (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "work_order.created")
        .count()
        == 2
    )
    assert team.id == ticket.service_team_id


def test_handoff_rejects_non_member_and_terminal_ticket(db_session):
    _subscriber_row, _team, actor_id, ticket = _ticket_with_team(db_session)

    with pytest.raises(
        ticket_work_order_handoff.TicketWorkOrderHandoffError
    ) as forbidden:
        _issue(
            db_session,
            ticket_id=ticket.id,
            actor_id=uuid4(),
            idempotency_key="outsider",
        )
    assert forbidden.value.code == "assigned_team_membership_required"
    assert db_session.query(WorkOrder).count() == 0

    ticket.status = "resolved"
    db_session.commit()
    with pytest.raises(
        ticket_work_order_handoff.TicketWorkOrderHandoffError
    ) as terminal:
        _issue(
            db_session,
            ticket_id=ticket.id,
            actor_id=actor_id,
            idempotency_key="terminal",
        )
    assert terminal.value.code == "ticket_terminal"


def test_handoff_scopes_field_visit_to_ticket_linked_project_task(db_session):
    subscriber, _team, actor_id, ticket = _ticket_with_team(db_session)
    project = Project(name="Repair project", subscriber_id=subscriber.id)
    db_session.add(project)
    db_session.flush()
    task = ProjectTask(
        project_id=project.id,
        ticket_id=ticket.id,
        title="Trace feeder",
        status="todo",
    )
    db_session.add(task)
    db_session.commit()

    result = _issue(
        db_session,
        ticket_id=ticket.id,
        actor_id=actor_id,
        payload=TicketWorkOrderIssueRequest(
            reason="Field trace required",
            project_task_id=task.id,
        ),
        idempotency_key="project-task-scope",
    )

    assert result.work_order.project_id == project.id
    assert result.work_order.project_task_id == task.id
    assert result.work_order.origin_ticket_id == ticket.id


def test_handoff_rejects_project_task_linked_to_another_ticket(db_session):
    subscriber, _team, actor_id, ticket = _ticket_with_team(db_session)
    other_ticket = Ticket(title="Other incident", subscriber_id=subscriber.id)
    project = Project(name="Repair project", subscriber_id=subscriber.id)
    db_session.add_all([project, other_ticket])
    db_session.flush()
    task = ProjectTask(
        project_id=project.id,
        ticket_id=other_ticket.id,
        title="Other scope",
    )
    db_session.add(task)
    db_session.commit()

    with pytest.raises(
        ticket_work_order_handoff.TicketWorkOrderHandoffError
    ) as mismatch:
        _issue(
            db_session,
            ticket_id=ticket.id,
            actor_id=actor_id,
            payload=TicketWorkOrderIssueRequest(
                reason="Wrong task",
                project_task_id=task.id,
            ),
            idempotency_key="wrong-task",
        )
    assert mismatch.value.code == "project_task_ticket_mismatch"


def test_generic_work_order_writes_cannot_accept_origin_ticket_id():
    assert "origin_ticket_id" not in WorkOrderHeaderCreate.model_fields
    assert "origin_ticket_id" not in WorkOrderHeaderUpdate.model_fields
