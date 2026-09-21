"""PostgreSQL serialization contracts for Team Inbox FIFO and capacity."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

from sqlalchemy.orm import sessionmaker

from app.models.service_team import ServiceTeam, ServiceTeamMember, ServiceTeamType
from app.models.team_inbox import (
    InboxAgentPresence,
    InboxConversation,
    InboxConversationAssignment,
    InboxConversationQueueEntry,
    InboxQueueEntryStatus,
)
from app.services import team_inbox_assignment
from app.services.owner_commands import CommandContext
from tests.staff_identity_fixtures import add_bound_staff_user


def _team(name: str) -> ServiceTeam:
    return ServiceTeam(
        name=f"{name} {uuid4().hex[:8]}",
        team_type=ServiceTeamType.support.value,
    )


def _conversation() -> InboxConversation:
    return InboxConversation(channel_type="email", status="open", is_active=True)


def test_shared_agent_cannot_consume_one_capacity_slot_from_two_teams(engine):
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as setup:
        first_team = _team("Capacity A")
        second_team = _team("Capacity B")
        setup.add_all([first_team, second_team])
        agent, person = add_bound_staff_user(setup)
        setup.add_all(
            [
                ServiceTeamMember(team_id=first_team.id, person_id=person.id),
                ServiceTeamMember(team_id=second_team.id, person_id=person.id),
                InboxAgentPresence(
                    person_id=agent.id,
                    status="online",
                    manual_override_status="online",
                    max_concurrent_conversations=1,
                    last_seen_at=datetime.now(UTC),
                ),
            ]
        )
        first = _conversation()
        second = _conversation()
        setup.add_all([first, second])
        setup.commit()
        team_ids = (first_team.id, second_team.id)
        conversation_ids = (first.id, second.id)
        agent_id = agent.id

    barrier = Barrier(2)

    def assign(index: int) -> str:
        with factory() as worker:
            conversation = worker.get(InboxConversation, conversation_ids[index])
            assert conversation is not None
            barrier.wait(timeout=10)
            result = team_inbox_assignment.assign_conversation_to_agent(
                worker,
                conversation=conversation,
                service_team_id=team_ids[index],
                person_id=agent_id,
                now=datetime.now(UTC),
            )
            worker.commit()
            return result.kind

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(assign, range(2)))

    assert sorted(outcomes) == ["agent_unavailable", "assigned"]
    with factory() as check:
        assert (
            check.query(InboxConversationAssignment)
            .filter(InboxConversationAssignment.person_id == agent_id)
            .filter(InboxConversationAssignment.is_active.is_(True))
            .count()
            == 1
        )


def test_simultaneous_same_team_admissions_get_distinct_sequences(engine):
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as setup:
        team = _team("Admission")
        first = _conversation()
        second = _conversation()
        setup.add_all([team, first, second])
        setup.commit()
        team_id = team.id
        conversation_ids = (first.id, second.id)
    entered_at = datetime(2026, 9, 8, 9, 0, tzinfo=UTC)
    barrier = Barrier(2)

    def admit(conversation_id):
        with factory() as worker:
            conversation = worker.get(InboxConversation, conversation_id)
            assert conversation is not None
            barrier.wait(timeout=10)
            result = team_inbox_assignment.queue_conversation_for_team(
                worker,
                conversation=conversation,
                service_team_id=team_id,
                now=entered_at,
            )
            worker.commit()
            return result.kind

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(admit, conversation_ids))

    assert outcomes == ["queued", "queued"]
    with factory() as check:
        rows = (
            check.query(InboxConversationQueueEntry)
            .filter(InboxConversationQueueEntry.service_team_id == team_id)
            .order_by(InboxConversationQueueEntry.queue_position)
            .all()
        )
        assert [row.queue_position for row in rows] == [1, 2]


def test_simultaneous_promotion_workers_promote_only_the_true_head(engine):
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    now = datetime(2026, 9, 8, 10, 0, tzinfo=UTC)
    with factory() as setup:
        team = _team("Promotion")
        setup.add(team)
        agent, person = add_bound_staff_user(setup)
        setup.add_all(
            [
                ServiceTeamMember(team_id=team.id, person_id=person.id),
                InboxAgentPresence(
                    person_id=agent.id,
                    status="online",
                    manual_override_status="online",
                    max_concurrent_conversations=1,
                    last_seen_at=now,
                ),
            ]
        )
        first = _conversation()
        second = _conversation()
        setup.add_all([first, second])
        setup.flush()
        team_inbox_assignment.queue_conversation_for_team(
            setup, conversation=first, service_team_id=team.id, now=now
        )
        team_inbox_assignment.queue_conversation_for_team(
            setup,
            conversation=second,
            service_team_id=team.id,
            now=now + timedelta(seconds=1),
        )
        setup.commit()
        first_id = first.id
        team_id = team.id
    barrier = Barrier(2)

    def promote(index: int) -> int:
        with factory() as worker:
            barrier.wait(timeout=10)
            result = team_inbox_assignment.sweep_queued_conversations(
                worker,
                team_inbox_assignment.InboxQueueSweepCommand(
                    context=CommandContext.system(
                        actor=f"test:promotion-worker:{index}",
                        scope="team-inbox:routing-command",
                        reason="concurrent strict FIFO proof",
                    ),
                    now=now + timedelta(minutes=1),
                ),
            )
            return result.promoted

    with ThreadPoolExecutor(max_workers=2) as pool:
        promoted_counts = list(pool.map(promote, range(2)))

    assert sum(promoted_counts) == 1
    with factory() as check:
        active = (
            check.query(InboxConversationAssignment)
            .filter(InboxConversationAssignment.service_team_id == team_id)
            .filter(InboxConversationAssignment.is_active.is_(True))
            .one()
        )
        assert active.conversation_id == first_id


def test_locked_team_head_cannot_be_skipped_by_another_promotion_worker(engine):
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    now = datetime(2026, 9, 8, 10, 0, tzinfo=UTC)
    with factory() as setup:
        team = _team("FIFO lock")
        setup.add(team)
        agent, person = add_bound_staff_user(setup)
        setup.add(ServiceTeamMember(team_id=team.id, person_id=person.id))
        setup.add(
            InboxAgentPresence(
                person_id=agent.id,
                status="online",
                manual_override_status="online",
                max_concurrent_conversations=1,
                last_seen_at=now,
            )
        )
        first = _conversation()
        second = _conversation()
        setup.add_all([first, second])
        setup.flush()
        team_inbox_assignment.queue_conversation_for_team(
            setup,
            conversation=first,
            service_team_id=team.id,
            now=now,
        )
        team_inbox_assignment.queue_conversation_for_team(
            setup,
            conversation=second,
            service_team_id=team.id,
            now=now + timedelta(seconds=1),
        )
        setup.commit()
        team_id = team.id
        first_id = first.id
        second_id = second.id

    with factory() as holder:
        holder.query(ServiceTeam).filter(
            ServiceTeam.id == team_id
        ).with_for_update().one()
        with factory() as contender:
            blocked = team_inbox_assignment.sweep_queued_conversations(
                contender,
                team_inbox_assignment.InboxQueueSweepCommand(
                    context=CommandContext.system(
                        actor="test:team-inbox-concurrency",
                        scope="team-inbox:routing-command",
                        reason="prove a locked team head is never skipped",
                    ),
                    now=now + timedelta(minutes=1),
                ),
            )
        assert blocked.promoted == 0
        holder.rollback()

    with factory() as worker:
        promoted = team_inbox_assignment.sweep_queued_conversations(
            worker,
            team_inbox_assignment.InboxQueueSweepCommand(
                context=CommandContext.system(
                    actor="test:team-inbox-concurrency",
                    scope="team-inbox:routing-command",
                    reason="promote the true head after lock release",
                ),
                now=now + timedelta(minutes=2),
            ),
        )
    assert promoted.promoted == 1

    with factory() as check:
        assignment = (
            check.query(InboxConversationAssignment)
            .filter(InboxConversationAssignment.service_team_id == team_id)
            .filter(InboxConversationAssignment.is_active.is_(True))
            .one()
        )
        assert assignment.conversation_id == first_id
        second_entry = (
            check.query(InboxConversationQueueEntry)
            .filter(InboxConversationQueueEntry.conversation_id == second_id)
            .one()
        )
        assert second_entry.status == InboxQueueEntryStatus.queued.value
