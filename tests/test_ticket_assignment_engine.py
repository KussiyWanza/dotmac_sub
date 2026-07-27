from __future__ import annotations

from uuid import uuid4

from app.models.service_team import ServiceTeam, ServiceTeamMember, ServiceTeamType
from app.models.support import Ticket, TicketAssignee, TicketStatus
from app.models.system_user import SystemUser
from app.models.ticket_workflow import TicketAssignmentRule, TicketAssignmentStrategy
from app.services.ticket_assignment.engine import auto_assign_ticket
from app.services.ticket_assignment.selectors import list_team_candidate_person_ids
from tests.staff_identity_fixtures import add_bound_staff_user


def _team(db_session, name: str = "Dispatch") -> ServiceTeam:
    team = ServiceTeam(name=name, team_type=ServiceTeamType.support.value)
    db_session.add(team)
    db_session.flush()
    return team


def _member(db_session, team: ServiceTeam):
    user, person = add_bound_staff_user(db_session)
    db_session.add(
        ServiceTeamMember(team_id=team.id, person_id=person.id, is_active=True)
    )
    db_session.flush()
    return user.id


def _ticket(
    db_session, *, title: str, service_team_id=None, region: str | None = None
) -> Ticket:
    ticket = Ticket(title=title, service_team_id=service_team_id, region=region)
    db_session.add(ticket)
    db_session.flush()
    return ticket


def test_ticket_auto_assign_round_robins_across_team_members(db_session):
    team = _team(db_session)
    person_a = _member(db_session, team)
    person_b = _member(db_session, team)
    rule = TicketAssignmentRule(
        name="Ops default",
        priority=100,
        is_active=True,
        strategy=TicketAssignmentStrategy.round_robin.value,
        team_id=team.id,
    )
    db_session.add(rule)
    t1 = _ticket(db_session, title="Ticket 1", service_team_id=team.id)
    t2 = _ticket(db_session, title="Ticket 2", service_team_id=team.id)
    db_session.commit()

    r1 = auto_assign_ticket(db_session, str(t1.id))
    r2 = auto_assign_ticket(db_session, str(t2.id))

    assert r1.assigned is True
    assert r2.assigned is True
    assert {r1.assignee_person_id, r2.assignee_person_id} == {
        str(person_a),
        str(person_b),
    }


def test_ticket_auto_assign_least_loaded_uses_open_ticket_counts(db_session):
    team = _team(db_session, "Support")
    loaded_person = _member(db_session, team)
    free_person = _member(db_session, team)
    db_session.add(
        Ticket(
            title="Existing load",
            service_team_id=team.id,
            assigned_to_person_id=loaded_person,
            status=TicketStatus.open.value,
        )
    )
    db_session.add(
        TicketAssignmentRule(
            name="Support least-loaded",
            priority=100,
            is_active=True,
            strategy=TicketAssignmentStrategy.least_loaded.value,
            team_id=team.id,
        )
    )
    candidate = _ticket(db_session, title="Needs assignment", service_team_id=team.id)
    db_session.commit()

    result = auto_assign_ticket(db_session, str(candidate.id))

    assert result.assigned is True
    assert result.assignee_person_id == str(free_person)


def test_ticket_auto_assign_respects_match_config_and_fallback_team(db_session):
    team = _team(db_session, "Regional")
    rule = TicketAssignmentRule(
        name="North queue",
        priority=100,
        is_active=True,
        strategy=TicketAssignmentStrategy.round_robin.value,
        team_id=team.id,
        match_config={"regions": ["north"]},
    )
    db_session.add(rule)
    north = _ticket(db_session, title="North issue", region="north")
    south = _ticket(db_session, title="South issue", region="south")
    db_session.commit()

    north_result = auto_assign_ticket(db_session, str(north.id))
    south_result = auto_assign_ticket(db_session, str(south.id))

    assert north_result.assigned is True
    assert north_result.fallback_service_team_id == str(team.id)
    assert south_result.assigned is False
    assert south_result.reason == "no_matching_rule"


def test_ticket_auto_assign_direct_technician_is_a_proposal_only(db_session):
    technician_id = uuid4()
    rule = TicketAssignmentRule(
        name="Direct technician",
        priority=100,
        is_active=True,
        strategy=TicketAssignmentStrategy.round_robin.value,
        match_config={"regions": ["west"], "assignee_person_id": str(technician_id)},
    )
    db_session.add(rule)
    ticket = _ticket(db_session, title="Direct issue", region="west")
    db_session.commit()

    result = auto_assign_ticket(db_session, str(ticket.id))
    db_session.refresh(ticket)

    assert result.assigned is True
    assert result.assignee_person_id == str(technician_id)
    assert ticket.assigned_to_person_id is None
    assert (
        db_session.query(TicketAssignee)
        .filter(
            TicketAssignee.ticket_id == ticket.id,
            TicketAssignee.person_id == technician_id,
        )
        .count()
        == 0
    )


def test_ticket_assignment_candidates_ignore_inactive_team(db_session):
    team = _team(db_session, "Retired support")
    _member(db_session, team)
    team.is_active = False
    db_session.commit()

    assert list_team_candidate_person_ids(db_session, str(team.id)) == []


def test_ticket_assignment_candidates_ignore_inactive_members(db_session):
    team = _team(db_session, "Partial support")
    inactive_person_id = _member(db_session, team)
    active_person_id = _member(db_session, team)
    inactive_party_id = db_session.get(SystemUser, inactive_person_id).person_party_id
    for member in team.members:
        if member.person_id == inactive_party_id:
            member.is_active = False
    db_session.commit()

    assert list_team_candidate_person_ids(db_session, str(team.id)) == [
        str(active_person_id)
    ]
