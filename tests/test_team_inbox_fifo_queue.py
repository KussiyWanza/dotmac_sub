from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.models.party import Party
from app.models.service_team import ServiceTeam, ServiceTeamMember
from app.models.subscriber import UserType
from app.models.system_user import SystemUser
from app.models.team_inbox import (
    InboxAgentPresence,
    InboxConversation,
    InboxConversationAssignment,
    InboxConversationQueueEntry,
)
from app.services.owner_commands import CommandContext
from app.services.team_inbox_assignment import (
    InboxQueueSweepCommand,
    estimate_queue_wait_minutes,
    queue_conversation_for_team,
    sweep_queued_conversations,
)


def _team(db_session) -> ServiceTeam:
    team = ServiceTeam(name=f"FIFO {uuid4()}", team_type="support")
    db_session.add(team)
    db_session.flush()
    return team


def _conversation(db_session) -> InboxConversation:
    row = InboxConversation(channel_type="chat_widget", status="open")
    db_session.add(row)
    db_session.flush()
    return row


def _agent(db_session, team: ServiceTeam, *, capacity: int) -> SystemUser:
    party = Party(party_type="person", display_name="Queue Agent")
    db_session.add(party)
    db_session.flush()
    user = SystemUser(
        person_party_id=party.id,
        party_bound_at=datetime.now(UTC),
        party_binding_source="test",
        party_binding_reason="FIFO queue test",
        first_name="Queue",
        last_name="Agent",
        email=f"queue-{uuid4()}@example.test",
        user_type=UserType.system_user,
    )
    db_session.add(user)
    db_session.flush()
    db_session.add(
        ServiceTeamMember(team_id=team.id, person_id=party.id, role="member")
    )
    db_session.add(
        InboxAgentPresence(
            person_id=user.id,
            status="online",
            manual_override_status="online",
            max_concurrent_conversations=capacity,
            last_seen_at=datetime.now(UTC),
        )
    )
    db_session.flush()
    return user


def test_queue_admission_preserves_fifo_order(db_session):
    team = _team(db_session)
    first = _conversation(db_session)
    second = _conversation(db_session)
    now = datetime(2026, 8, 6, 10, 0, tzinfo=UTC)

    queue_conversation_for_team(
        db_session, conversation=second, service_team_id=team.id, now=now
    )
    queue_conversation_for_team(
        db_session,
        conversation=first,
        service_team_id=team.id,
        now=now - timedelta(minutes=1),
    )

    rows = (
        db_session.query(InboxConversationQueueEntry)
        .order_by(InboxConversationQueueEntry.queue_position)
        .all()
    )
    assert [row.conversation_id for row in rows] == [second.id, first.id]
    assert [row.queue_position for row in rows] == [1, 2]
    assert rows[0].entered_at.replace(tzinfo=UTC) == now


def test_sweep_promotes_oldest_when_capacity_appears(db_session):
    team = _team(db_session)
    agent = _agent(db_session, team, capacity=1)
    occupied = _conversation(db_session)
    first = _conversation(db_session)
    second = _conversation(db_session)
    db_session.add(
        InboxConversationAssignment(
            conversation_id=occupied.id,
            service_team_id=team.id,
            person_id=agent.id,
            is_active=True,
        )
    )
    queue_conversation_for_team(db_session, conversation=first, service_team_id=team.id)
    queue_conversation_for_team(
        db_session, conversation=second, service_team_id=team.id
    )
    db_session.commit()

    blocked = sweep_queued_conversations(
        db_session,
        InboxQueueSweepCommand(
            context=CommandContext.system(
                actor="test", scope="team-inbox:routing-command", reason="test"
            )
        ),
    )
    assert blocked.promoted == 0

    assignment = (
        db_session.query(InboxConversationAssignment)
        .filter(InboxConversationAssignment.conversation_id == occupied.id)
        .one()
    )
    assignment.is_active = False
    db_session.commit()
    promoted = sweep_queued_conversations(
        db_session,
        InboxQueueSweepCommand(
            context=CommandContext.system(
                actor="test", scope="team-inbox:routing-command", reason="test"
            )
        ),
    )
    assert promoted.promoted == 1
    active = (
        db_session.query(InboxConversationAssignment)
        .filter(InboxConversationAssignment.is_active.is_(True))
        .one()
    )
    assert active.conversation_id == first.id


def test_wait_estimate_uses_fifo_position_and_capacity():
    assert (
        estimate_queue_wait_minutes(
            queue_position=1,
            active_assignments=2,
            total_capacity=2,
            average_handle_minutes=10,
        )
        == 10
    )
    assert (
        estimate_queue_wait_minutes(
            queue_position=3,
            active_assignments=2,
            total_capacity=2,
            average_handle_minutes=10,
        )
        == 20
    )
    assert (
        estimate_queue_wait_minutes(
            queue_position=1,
            active_assignments=0,
            total_capacity=2,
        )
        == 0
    )
    assert (
        estimate_queue_wait_minutes(
            queue_position=1, active_assignments=0, total_capacity=0
        )
        is None
    )
