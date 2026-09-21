from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.models.domain_settings import SettingDomain
from app.models.service_team import ServiceTeam, ServiceTeamMember, ServiceTeamType
from app.models.team_inbox import (
    InboxAgentPresence,
    InboxAgentPresenceEvent,
    InboxAgentPresenceStatus,
    InboxConversation,
    InboxConversationAssignment,
    InboxConversationTeam,
    InboxRoutingDecisionMode,
    InboxRoutingEvent,
    InboxRoutingEventType,
    InboxTeamRole,
)
from app.services import (
    settings_spec,
    team_inbox_assignment,
    team_inbox_commands,
    web_system_settings_forms,
)
from app.services.owner_commands import CommandContext
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
            last_seen_at=datetime.now(UTC) if status == "online" else None,
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
    availability = team_inbox_assignment.agent_availability_snapshots(
        db_session,
        (full, free),
    )

    assert [candidate.person_id for candidate in candidates] == [str(free)]
    assert availability[full].active_conversation_count == 1
    assert availability[full].max_concurrent_conversations == 1
    assert availability[full].available_capacity == 0
    assert availability[full].assignment_eligible is False
    assert (
        availability[full].unavailability_reason
        is team_inbox_assignment.InboxAgentUnavailabilityReason.at_capacity
    )
    assert availability[free].available_capacity == 1
    assert availability[free].assignment_eligible is True


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
    assert event.max_concurrent_conversations == 10


def test_auto_assignment_uses_durable_round_robin_cursor(db_session):
    team = _team(db_session, "Round Robin")
    first_agent = _member(db_session, team)
    second_agent = _member(db_session, team)
    first = _conversation(db_session)
    second = _conversation(db_session)
    db_session.commit()

    first_result = team_inbox_assignment.assign_conversation_to_available_agent(
        db_session,
        conversation=first,
        service_team_id=team.id,
    )
    second_result = team_inbox_assignment.assign_conversation_to_available_agent(
        db_session,
        conversation=second,
        service_team_id=team.id,
    )
    db_session.commit()

    assert {first_result.assigned_person_id, second_result.assigned_person_id} == {
        str(first_agent),
        str(second_agent),
    }


def test_assign_conversation_to_me_requires_team_membership(db_session):
    team = _team(db_session, "Support")
    user, _person = add_bound_staff_user(db_session)
    conversation = _conversation(db_session)
    conversation.primary_service_team_id = team.id
    db_session.commit()

    with pytest.raises(
        team_inbox_commands.InboxCommandError,
        match="active member of the target team",
    ):
        team_inbox_commands.assign_conversation_to_me(
            db_session,
            conversation_id=conversation.id,
            actor_person_id=user.id,
        )
    assert db_session.query(InboxConversationAssignment).count() == 0


def test_assign_conversation_to_me_replays_existing_active_assignment(db_session):
    team = _team(db_session, "Support")
    user_id = _member(db_session, team)
    conversation = _conversation(db_session)
    conversation.primary_service_team_id = team.id
    db_session.commit()

    first = team_inbox_commands.assign_conversation_to_me(
        db_session,
        conversation_id=conversation.id,
        actor_person_id=user_id,
    )
    second = team_inbox_commands.assign_conversation_to_me(
        db_session,
        conversation_id=conversation.id,
        actor_person_id=user_id,
    )
    db_session.commit()

    assert first.message == "Assigned conversation to you."
    assert second.message == "Assigned conversation to you."
    assignments = db_session.query(InboxConversationAssignment).all()
    events = db_session.query(InboxRoutingEvent).all()
    assert len(assignments) == 1
    assert assignments[0].person_id == user_id
    assert assignments[0].service_team_id == team.id
    assert assignments[0].is_active is True
    assert len(events) == 1


def test_self_assignment_cannot_bypass_capacity(db_session):
    team = _team(db_session, "Support")
    user_id = _member(db_session, team, max_concurrent=1)
    existing = _conversation(db_session)
    target = _conversation(db_session)
    target.primary_service_team_id = team.id
    db_session.add(
        InboxConversationAssignment(
            conversation_id=existing.id,
            service_team_id=team.id,
            person_id=user_id,
            is_active=True,
        )
    )
    db_session.commit()

    with pytest.raises(
        team_inbox_commands.InboxCommandError,
        match=r"Agent is at capacity \(1 of 1 active conversations\)",
    ):
        team_inbox_commands.assign_conversation_to_me(
            db_session,
            conversation_id=target.id,
            actor_person_id=user_id,
        )
    assert db_session.query(InboxConversationAssignment).count() == 1


def test_direct_agent_assignment_still_requires_team_membership(db_session):
    team = _team(db_session, "Support")
    user, _person = add_bound_staff_user(db_session)
    conversation = _conversation(db_session)
    db_session.commit()

    result = team_inbox_assignment.assign_conversation_to_agent(
        db_session,
        conversation=conversation,
        service_team_id=team.id,
        person_id=user.id,
    )

    assert result.kind == "invalid_agent"
    assert result.reason == "person_id must be an active member of the target team"


def test_manual_assignment_rejects_offline_team_member(db_session):
    team = _team(db_session, "Support")
    offline_agent = _member(
        db_session, team, status=InboxAgentPresenceStatus.offline.value
    )
    conversation = _conversation(db_session)
    db_session.commit()

    result = team_inbox_assignment.assign_conversation_to_agent(
        db_session,
        conversation=conversation,
        service_team_id=team.id,
        person_id=offline_agent,
    )

    assert result.kind == "agent_unavailable"
    assert result.reason == (
        "Agent is not currently available for assignment (status: offline)."
    )
    assert db_session.query(InboxConversationAssignment).count() == 0


def test_manual_assignment_reports_exact_agent_capacity(db_session):
    team = _team(db_session, "Support")
    full_agent = _member(db_session, team, max_concurrent=1)
    existing = _conversation(db_session)
    target = _conversation(db_session)
    db_session.add(
        InboxConversationAssignment(
            conversation_id=existing.id,
            service_team_id=team.id,
            person_id=full_agent,
            is_active=True,
        )
    )
    db_session.commit()

    result = team_inbox_assignment.assign_conversation_to_agent(
        db_session,
        conversation=target,
        service_team_id=team.id,
        person_id=full_agent,
    )

    assert result.kind == "agent_unavailable"
    assert result.reason == "Agent is at capacity (1 of 1 active conversations)."
    assert db_session.query(InboxConversationAssignment).count() == 1


def test_agent_at_nine_of_ten_receives_exactly_one_more(db_session):
    team = _team(db_session, "Support")
    agent = _member(db_session, team, max_concurrent=10)
    for _index in range(9):
        existing = _conversation(db_session)
        db_session.add(
            InboxConversationAssignment(
                conversation_id=existing.id,
                service_team_id=team.id,
                person_id=agent,
                is_active=True,
            )
        )
    first_target = _conversation(db_session)
    second_target = _conversation(db_session)
    db_session.flush()

    accepted = team_inbox_assignment.assign_conversation_to_agent(
        db_session,
        conversation=first_target,
        service_team_id=team.id,
        person_id=agent,
    )
    rejected = team_inbox_assignment.assign_conversation_to_agent(
        db_session,
        conversation=second_target,
        service_team_id=team.id,
        person_id=agent,
    )

    assert accepted.kind == "assigned"
    assert rejected.kind == "agent_unavailable"
    assert rejected.reason == "Agent is at capacity (10 of 10 active conversations)."
    assert (
        db_session.query(InboxConversationAssignment)
        .filter(InboxConversationAssignment.person_id == agent)
        .filter(InboxConversationAssignment.is_active.is_(True))
        .count()
        == 10
    )


def test_manual_assignment_cannot_jump_a_team_queue(db_session):
    team = _team(db_session, "Support")
    agent = _member(db_session, team)
    first = _conversation(db_session)
    second = _conversation(db_session)
    now = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)
    team_inbox_assignment.queue_conversation_for_team(
        db_session,
        conversation=first,
        service_team_id=team.id,
        now=now,
    )
    team_inbox_assignment.queue_conversation_for_team(
        db_session,
        conversation=second,
        service_team_id=team.id,
        now=now + timedelta(seconds=1),
    )

    rejected = team_inbox_assignment.assign_conversation_to_agent(
        db_session,
        conversation=second,
        service_team_id=team.id,
        person_id=agent,
        now=now + timedelta(minutes=1),
    )
    accepted = team_inbox_assignment.assign_conversation_to_agent(
        db_session,
        conversation=first,
        service_team_id=team.id,
        person_id=agent,
        now=now + timedelta(minutes=1),
    )

    assert rejected.kind == "queue_order_conflict"
    assert accepted.kind == "assigned"
    assignments = db_session.query(InboxConversationAssignment).all()
    assert [row.conversation_id for row in assignments] == [first.id]


def test_admin_capacity_setting_is_consumed_by_assignment_runtime(db_session):
    spec = settings_spec.get_spec(
        SettingDomain.comms,
        "inbox_agent_default_max_concurrent_conversations",
    )
    assert spec is not None
    assert spec.label == "Default active Inbox conversations per agent"
    assert spec.default == 10
    assert (spec.min_value, spec.max_value) == (1, 100)
    service = settings_spec.DOMAIN_SETTINGS_SERVICE[SettingDomain.comms]

    errors = web_system_settings_forms.upsert_settings_from_specs(
        db=db_session,
        form={spec.key: "25"},
        specs=[spec],
        service=service,
    )

    assert errors == []
    assert (
        team_inbox_assignment.resolve_default_max_concurrent_conversations(db_session)
        == 25
    )


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


def test_stale_online_presence_is_not_available_for_auto_assignment(db_session):
    team = _team(db_session)
    agent = _member(db_session, team)
    presence = (
        db_session.query(InboxAgentPresence)
        .filter(InboxAgentPresence.person_id == agent)
        .one()
    )
    presence.last_seen_at = datetime.now(UTC) - timedelta(seconds=30 * 60 + 1)
    conversation = _conversation(db_session)
    db_session.commit()

    result = team_inbox_assignment.assign_conversation_to_available_agent(
        db_session,
        conversation=conversation,
        service_team_id=team.id,
    )

    assert result.kind == "queued"
    assert result.reason == "no_available_agent"


def test_effective_online_presence_stays_online_until_freshness_expires(db_session):
    observed_at = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
    presence = InboxAgentPresence(
        person_id=uuid4(),
        status=InboxAgentPresenceStatus.online.value,
        manual_override_status=InboxAgentPresenceStatus.online.value,
        last_seen_at=observed_at,
    )

    assert (
        team_inbox_assignment.effective_presence_status(
            presence, now=observed_at + timedelta(minutes=30)
        )
        == InboxAgentPresenceStatus.online.value
    )
    assert (
        team_inbox_assignment.effective_presence_status(
            presence, now=observed_at + timedelta(minutes=30, seconds=1)
        )
        == InboxAgentPresenceStatus.offline.value
    )


def test_repeated_online_presence_update_refreshes_freshness(db_session):
    team = _team(db_session)
    agent = _member(db_session, team)
    presence = (
        db_session.query(InboxAgentPresence)
        .filter(InboxAgentPresence.person_id == agent)
        .one()
    )
    observed_at = datetime.now(UTC)

    updated = team_inbox_assignment.set_agent_presence(
        db_session,
        person_id=agent,
        status=InboxAgentPresenceStatus.online.value,
        now=observed_at,
    )

    assert updated.last_seen_at == observed_at
    assert presence.last_seen_at is not None
    assert presence.last_seen_at.replace(tzinfo=UTC) == observed_at


def test_set_agent_presence_records_effective_transition_when_online_is_stale(
    db_session,
):
    person_id = uuid4()
    observed_at = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
    presence = InboxAgentPresence(
        person_id=person_id,
        status=InboxAgentPresenceStatus.online.value,
        manual_override_status=InboxAgentPresenceStatus.online.value,
        last_seen_at=observed_at - timedelta(minutes=31),
    )
    db_session.add(presence)
    db_session.flush()

    updated = team_inbox_assignment.set_agent_presence(
        db_session,
        person_id=person_id,
        status=InboxAgentPresenceStatus.online.value,
        now=observed_at,
    )

    assert updated.status == InboxAgentPresenceStatus.online.value
    assert updated.manual_override_status == InboxAgentPresenceStatus.online.value
    assert updated.last_seen_at == observed_at
    event = db_session.query(InboxAgentPresenceEvent).one()
    assert event.previous_status == InboxAgentPresenceStatus.offline.value
    assert event.status == InboxAgentPresenceStatus.online.value


def test_reply_activity_refreshes_only_manually_online_presence(db_session):
    online_person_id = uuid4()
    away_person_id = uuid4()
    original_seen_at = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
    reply_seen_at = original_seen_at + timedelta(minutes=20)
    online_presence = InboxAgentPresence(
        person_id=online_person_id,
        status=InboxAgentPresenceStatus.online.value,
        manual_override_status=InboxAgentPresenceStatus.online.value,
        last_seen_at=original_seen_at,
    )
    away_presence = InboxAgentPresence(
        person_id=away_person_id,
        status=InboxAgentPresenceStatus.away.value,
        manual_override_status=InboxAgentPresenceStatus.away.value,
        last_seen_at=original_seen_at,
    )
    db_session.add_all([online_presence, away_presence])
    db_session.flush()

    team_inbox_assignment.record_agent_reply_activity(
        db_session,
        person_id=online_person_id,
        now=reply_seen_at,
    )
    team_inbox_assignment.record_agent_reply_activity(
        db_session,
        person_id=away_person_id,
        now=reply_seen_at,
    )

    assert online_presence.last_seen_at == reply_seen_at
    assert away_presence.last_seen_at == original_seen_at


def _presence_heartbeat_command(
    system_user_id,
    *,
    observed_at: datetime,
) -> team_inbox_assignment.AgentPresenceHeartbeatCommand:
    return team_inbox_assignment.AgentPresenceHeartbeatCommand(
        context=CommandContext.system(
            actor=f"system-user:{system_user_id}",
            scope="test:team-inbox-presence-heartbeat",
            reason="test authenticated visible Inbox activity",
        ),
        system_user_id=system_user_id,
        observed_at=observed_at,
    )


def test_presence_heartbeat_creates_online_presence_for_active_staff(db_session):
    system_user, _person = add_bound_staff_user(db_session)
    system_user_id = system_user.id
    observed_at = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    db_session.commit()

    outcome = team_inbox_assignment.refresh_agent_presence(
        db_session,
        command=_presence_heartbeat_command(
            system_user_id,
            observed_at=observed_at,
        ),
    )

    presence = db_session.query(InboxAgentPresence).one()
    event = db_session.query(InboxAgentPresenceEvent).one()
    assert outcome.status is InboxAgentPresenceStatus.online
    assert (
        outcome.disposition
        is team_inbox_assignment.AgentPresenceHeartbeatDisposition.refreshed
    )
    assert presence.status == InboxAgentPresenceStatus.online.value
    assert presence.manual_override_status is None
    assert presence.last_seen_at is not None
    assert presence.last_seen_at.replace(tzinfo=UTC) == observed_at
    assert (
        event.reason_code
        == team_inbox_assignment.InboxPresenceReason.authenticated_inbox_activity
    )


def test_presence_heartbeat_revives_stale_selected_online_presence(db_session):
    system_user, _person = add_bound_staff_user(db_session)
    system_user_id = system_user.id
    observed_at = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    presence = InboxAgentPresence(
        person_id=system_user_id,
        status=InboxAgentPresenceStatus.online.value,
        manual_override_status=InboxAgentPresenceStatus.online.value,
        last_seen_at=observed_at - timedelta(minutes=31),
    )
    db_session.add(presence)
    db_session.commit()

    outcome = team_inbox_assignment.refresh_agent_presence(
        db_session,
        command=_presence_heartbeat_command(
            system_user_id,
            observed_at=observed_at,
        ),
    )

    assert outcome.status is InboxAgentPresenceStatus.online
    assert presence.manual_override_status == InboxAgentPresenceStatus.online.value
    assert presence.last_seen_at is not None
    assert presence.last_seen_at.replace(tzinfo=UTC) == observed_at
    event = db_session.query(InboxAgentPresenceEvent).one()
    assert event.previous_status == InboxAgentPresenceStatus.offline.value
    assert event.status == InboxAgentPresenceStatus.online.value


@pytest.mark.parametrize(
    "selected_status",
    [
        InboxAgentPresenceStatus.away,
        InboxAgentPresenceStatus.on_break,
        InboxAgentPresenceStatus.offline,
    ],
)
def test_presence_heartbeat_preserves_explicit_unavailable_state(
    db_session,
    selected_status,
):
    system_user, _person = add_bound_staff_user(db_session)
    system_user_id = system_user.id
    original_seen_at = datetime(2026, 9, 10, 11, 0, tzinfo=UTC)
    observed_at = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    presence = InboxAgentPresence(
        person_id=system_user_id,
        status=selected_status.value,
        manual_override_status=selected_status.value,
        last_seen_at=original_seen_at,
    )
    db_session.add(presence)
    db_session.commit()

    outcome = team_inbox_assignment.refresh_agent_presence(
        db_session,
        command=_presence_heartbeat_command(
            system_user_id,
            observed_at=observed_at,
        ),
    )

    assert outcome.status is selected_status
    assert (
        outcome.disposition
        is team_inbox_assignment.AgentPresenceHeartbeatDisposition.explicit_unavailable
    )
    assert presence.last_seen_at is not None
    assert presence.last_seen_at.replace(tzinfo=UTC) == original_seen_at
    assert db_session.query(InboxAgentPresenceEvent).count() == 0


def test_presence_heartbeat_rejects_inactive_staff_without_writing(db_session):
    system_user, _person = add_bound_staff_user(db_session)
    system_user_id = system_user.id
    system_user.is_active = False
    observed_at = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    db_session.commit()

    outcome = team_inbox_assignment.refresh_agent_presence(
        db_session,
        command=_presence_heartbeat_command(
            system_user_id,
            observed_at=observed_at,
        ),
    )

    assert outcome.status is InboxAgentPresenceStatus.offline
    assert (
        outcome.disposition
        is team_inbox_assignment.AgentPresenceHeartbeatDisposition.inactive_principal
    )
    assert db_session.query(InboxAgentPresence).count() == 0
    assert db_session.query(InboxAgentPresenceEvent).count() == 0


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


def test_staff_sign_in_defaults_presence_online_and_clears_prior_override(db_session):
    system_user, _person = add_bound_staff_user(db_session)
    signed_in_at = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    auth_session_id = uuid4()
    db_session.add(
        InboxAgentPresence(
            person_id=system_user.id,
            status=InboxAgentPresenceStatus.away.value,
            manual_override_status=InboxAgentPresenceStatus.away.value,
            last_seen_at=signed_in_at - timedelta(minutes=5),
        )
    )
    db_session.flush()

    outcome = team_inbox_assignment.record_agent_signed_in_presence(
        db_session,
        command=team_inbox_assignment.AgentSignedInPresenceCommand(
            system_user_id=system_user.id,
            auth_session_id=auth_session_id,
            signed_in_at=signed_in_at,
        ),
    )

    presence = db_session.query(InboxAgentPresence).one()
    event = db_session.query(InboxAgentPresenceEvent).one()
    assert outcome.status is InboxAgentPresenceStatus.online
    assert outcome.transition_recorded is True
    assert presence.status == InboxAgentPresenceStatus.online.value
    assert presence.manual_override_status is None
    assert presence.last_seen_at == signed_in_at
    assert event.previous_status == InboxAgentPresenceStatus.away.value
    assert event.status == InboxAgentPresenceStatus.online.value
    assert event.reason_code == team_inbox_assignment.InboxPresenceReason.staff_sign_in
    assert event.source_id == f"auth-session:{auth_session_id}"
    assert (presence.metadata_ or {}).get("manual_status_history") is None


def test_staff_sign_in_locks_only_team_inbox_presence():
    sign_in_source = inspect.getsource(
        team_inbox_assignment.record_agent_signed_in_presence
    )
    presence_source = inspect.getsource(team_inbox_assignment.set_agent_presence)

    assert ".with_for_update()" not in sign_in_source
    assert ".with_for_update()" in presence_source


def test_staff_sign_in_refreshes_online_presence_without_false_transition(db_session):
    system_user, _person = add_bound_staff_user(db_session)
    signed_in_at = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    db_session.add(
        InboxAgentPresence(
            person_id=system_user.id,
            status=InboxAgentPresenceStatus.online.value,
            manual_override_status=InboxAgentPresenceStatus.online.value,
            last_seen_at=signed_in_at - timedelta(minutes=5),
        )
    )
    db_session.flush()

    outcome = team_inbox_assignment.record_agent_signed_in_presence(
        db_session,
        command=team_inbox_assignment.AgentSignedInPresenceCommand(
            system_user_id=system_user.id,
            auth_session_id=uuid4(),
            signed_in_at=signed_in_at,
        ),
    )

    presence = db_session.query(InboxAgentPresence).one()
    assert outcome.transition_recorded is False
    assert presence.status == InboxAgentPresenceStatus.online.value
    assert presence.manual_override_status is None
    assert presence.last_seen_at == signed_in_at
    assert db_session.query(InboxAgentPresenceEvent).count() == 0
