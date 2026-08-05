from __future__ import annotations

from uuid import uuid4

from app.models.service_team import ServiceTeam, ServiceTeamMember, ServiceTeamType
from app.models.team_inbox import (
    InboxAgentPresence,
    InboxAgentPresenceStatus,
    InboxConversation,
    InboxConversationAssignment,
    InboxConversationTeam,
    InboxRoutingDecisionMode,
    InboxRoutingEvent,
    InboxRoutingEventType,
    InboxTeamRole,
)
from app.services import team_inbox_assignment
from tests.staff_identity_fixtures import add_bound_staff_user


def _team(db_session, name: str = "Support") -> ServiceTeam:
    team = ServiceTeam(name=name, team_type=ServiceTeamType.support.value)
    db_session.add(team)
    db_session.flush()
    return team


def _member(
    db_session,
    team: ServiceTeam,
    *,
    status: str = InboxAgentPresenceStatus.online.value,
    max_concurrent: int | None = None,
):
    user, person = add_bound_staff_user(db_session)
    person_id = user.id
    db_session.add(
        ServiceTeamMember(team_id=team.id, person_id=person.id, is_active=True)
    )
    db_session.add(
        InboxAgentPresence(
            person_id=person_id,
            status=status,
            max_concurrent_conversations=max_concurrent,
        )
    )
    db_session.flush()
    return person_id


def _conversation(db_session) -> InboxConversation:
    conversation = InboxConversation(channel_type="email", subject="Need help")
    db_session.add(conversation)
    db_session.flush()
    return conversation


def test_available_team_agents_ignore_offline_members(db_session):
    team = _team(db_session)
    online = _member(db_session, team, status=InboxAgentPresenceStatus.online.value)
    _member(db_session, team, status=InboxAgentPresenceStatus.offline.value)
    db_session.commit()

    candidates = team_inbox_assignment.list_available_team_agents(db_session, team.id)

    assert [candidate.person_id for candidate in candidates] == [str(online)]


def test_available_team_agents_respect_manual_presence_override(db_session):
    team = _team(db_session)
    agent = _member(db_session, team, status=InboxAgentPresenceStatus.online.value)
    presence = (
        db_session.query(InboxAgentPresence)
        .filter(InboxAgentPresence.person_id == agent)
        .one()
    )
    presence.manual_override_status = InboxAgentPresenceStatus.away.value
    db_session.commit()

    candidates = team_inbox_assignment.list_available_team_agents(db_session, team.id)

    assert candidates == []


def test_available_team_agents_ignore_full_members(db_session):
    team = _team(db_session)
    full = _member(db_session, team, max_concurrent=1)
    free = _member(db_session, team, max_concurrent=1)
    conversation = _conversation(db_session)
    db_session.add(
        InboxConversationAssignment(
            conversation_id=conversation.id,
            service_team_id=team.id,
            person_id=full,
            is_active=True,
        )
    )
    db_session.commit()

    candidates = team_inbox_assignment.list_available_team_agents(db_session, team.id)

    assert [candidate.person_id for candidate in candidates] == [str(free)]


def test_assign_conversation_escalates_to_team_and_online_agent(db_session):
    team = _team(db_session, "Field Service")
    agent = _member(db_session, team)
    conversation = _conversation(db_session)
    db_session.commit()

    result = team_inbox_assignment.assign_conversation_to_available_agent(
        db_session,
        conversation=conversation,
        service_team_id=team.id,
    )
    db_session.commit()

    link = db_session.query(InboxConversationTeam).one()
    assignment = db_session.query(InboxConversationAssignment).one()
    event = db_session.query(InboxRoutingEvent).one()
    assert result.kind == "assigned"
    assert result.assigned_person_id == str(agent)
    assert conversation.primary_service_team_id == team.id
    assert link.role == InboxTeamRole.owner.value
    assert assignment.person_id == agent
    assert assignment.is_active is True
    assert event.decision_mode is InboxRoutingDecisionMode.automatic
    assert event.presence_status == InboxAgentPresenceStatus.online.value
    assert event.active_conversation_count == 0
    assert event.max_concurrent_conversations == 3


def test_assign_conversation_queues_when_no_agent_available(db_session):
    team = _team(db_session)
    _member(db_session, team, status=InboxAgentPresenceStatus.away.value)
    conversation = _conversation(db_session)
    db_session.commit()

    result = team_inbox_assignment.assign_conversation_to_available_agent(
        db_session,
        conversation=conversation,
        service_team_id=team.id,
    )
    db_session.commit()

    assert result.kind == "queued"
    assert result.reason == "no_available_agent"
    assert conversation.primary_service_team_id == team.id
    assert (
        db_session.query(InboxConversationTeam).one().role == InboxTeamRole.owner.value
    )
    assert db_session.query(InboxConversationAssignment).count() == 0
    event = db_session.query(InboxRoutingEvent).one()
    assert event.event_type is InboxRoutingEventType.auto_assignment_declined
    assert event.decision_mode is InboxRoutingDecisionMode.automatic


def test_set_agent_presence_creates_manual_override(db_session):
    person_id = uuid4()

    presence = team_inbox_assignment.set_agent_presence(
        db_session,
        person_id=person_id,
        status=InboxAgentPresenceStatus.away.value,
    )
    db_session.commit()

    assert presence.person_id == person_id
    assert presence.status == InboxAgentPresenceStatus.away.value
    assert presence.manual_override_status == InboxAgentPresenceStatus.away.value
    assert presence.last_seen_at is not None
