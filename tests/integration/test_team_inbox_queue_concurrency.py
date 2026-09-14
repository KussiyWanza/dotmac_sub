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
    InboxMessage,
    InboxMessageDirection,
    InboxQueueEntryStatus,
    InboxRoutingEvent,
)
from app.services import (
    team_inbox_assignment,
    team_inbox_maintenance,
    team_inbox_reply_window,
)
from app.services.owner_commands import CommandContext
from tests.staff_identity_fixtures import add_bound_staff_user


def _team(name: str) -> ServiceTeam:
    return ServiceTeam(
        name=f"{name} {uuid4().hex[:8]}",
        team_type=ServiceTeamType.support.value,
    )


def _conversation() -> InboxConversation:
    return InboxConversation(channel_type="email", status="open", is_active=True)


def _expired_whatsapp(now: datetime) -> tuple[InboxConversation, InboxMessage]:
    conversation = InboxConversation(
        channel_type="whatsapp",
        status="open",
        is_active=True,
        contact_address=f"+23480{uuid4().int % 10**8:08d}",
        external_thread_id=f"whatsapp:{uuid4()}",
        first_message_at=now - timedelta(hours=25),
        last_message_at=now - timedelta(hours=25),
    )
    message = InboxMessage(
        conversation=conversation,
        channel_type="whatsapp",
        direction=InboxMessageDirection.inbound.value,
        body="Old inbound",
        received_at=now - timedelta(hours=25),
        metadata_={"reply_window_qualifying": True},
    )
    return conversation, message


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


def test_customer_inbound_and_expiry_release_serialize_to_a_valid_window_state(engine):
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    with factory() as setup:
        team = _team("Expiry inbound race")
        setup.add(team)
        agent, person = add_bound_staff_user(setup)
        setup.add(ServiceTeamMember(team_id=team.id, person_id=person.id))
        conversation, old_message = _expired_whatsapp(now)
        conversation.primary_service_team_id = team.id
        setup.add_all([conversation, old_message])
        setup.flush()
        assignment = InboxConversationAssignment(
            conversation_id=conversation.id,
            service_team_id=team.id,
            person_id=agent.id,
            assigned_at=now - timedelta(hours=25),
            is_active=True,
        )
        setup.add(assignment)
        setup.commit()
        conversation_id = conversation.id
        assignment_id = assignment.id
    barrier = Barrier(2)

    def expire() -> int:
        with factory() as worker:
            barrier.wait(timeout=10)
            result = team_inbox_maintenance.sweep_expired_whatsapp_windows(
                worker,
                team_inbox_maintenance.WhatsAppWindowExpirySweepCommand(
                    context=CommandContext.system(
                        actor="test:expiry-race",
                        scope="team-inbox:maintenance",
                        reason="race expiry against inbound",
                    ),
                    now=now,
                ),
            )
            return result.assignments_released

    def inbound() -> None:
        with factory.begin() as worker:
            barrier.wait(timeout=10)
            conversation = (
                worker.query(InboxConversation)
                .filter(InboxConversation.id == conversation_id)
                .with_for_update()
                .one()
            )
            worker.add(
                InboxMessage(
                    conversation_id=conversation.id,
                    channel_type="whatsapp",
                    direction=InboxMessageDirection.inbound.value,
                    body="Concurrent inbound",
                    received_at=now,
                    metadata_={"reply_window_qualifying": True},
                )
            )
            conversation.last_message_at = now

    with ThreadPoolExecutor(max_workers=2) as pool:
        expiry_future = pool.submit(expire)
        inbound_future = pool.submit(inbound)
        released = expiry_future.result(timeout=20)
        inbound_future.result(timeout=20)

    with factory() as check:
        conversation = check.get(InboxConversation, conversation_id)
        assignment = check.get(InboxConversationAssignment, assignment_id)
        assert conversation is not None and assignment is not None
        assert (
            team_inbox_reply_window.decide_reply_window(
                check, conversation=conversation, now=now
            ).status
            is team_inbox_reply_window.ReplyWindowStatus.open
        )
        assert released in {0, 1}
        assert assignment.is_active is (released == 0)
        assert (
            check.query(InboxRoutingEvent)
            .filter(InboxRoutingEvent.conversation_id == conversation_id)
            .filter(InboxRoutingEvent.reason_code == "whatsapp_window_expired")
            .count()
            == released
        )


def test_agent_assignment_and_expiry_release_cannot_leave_expired_assignment(engine):
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    with factory() as setup:
        team = _team("Expiry assignment race")
        setup.add(team)
        agent, person = add_bound_staff_user(setup)
        setup.add_all(
            [
                ServiceTeamMember(team_id=team.id, person_id=person.id),
                InboxAgentPresence(
                    person_id=agent.id,
                    status="online",
                    manual_override_status="online",
                    max_concurrent_conversations=10,
                    last_seen_at=now,
                ),
            ]
        )
        conversation, message = _expired_whatsapp(now)
        conversation.primary_service_team_id = team.id
        setup.add_all([conversation, message])
        setup.flush()
        setup.add(
            InboxConversationAssignment(
                conversation_id=conversation.id,
                service_team_id=team.id,
                person_id=agent.id,
                assigned_at=now - timedelta(hours=25),
                is_active=True,
            )
        )
        setup.commit()
        conversation_id = conversation.id
        team_id = team.id
        agent_id = agent.id
    barrier = Barrier(2)

    def expire() -> int:
        with factory() as worker:
            barrier.wait(timeout=10)
            return team_inbox_maintenance.sweep_expired_whatsapp_windows(
                worker,
                team_inbox_maintenance.WhatsAppWindowExpirySweepCommand(
                    context=CommandContext.system(
                        actor="test:expiry-assignment-race",
                        scope="team-inbox:maintenance",
                        reason="race expiry against assignment",
                    ),
                    now=now,
                ),
            ).assignments_released

    def assign() -> str:
        with factory.begin() as worker:
            conversation = worker.get(InboxConversation, conversation_id)
            assert conversation is not None
            barrier.wait(timeout=10)
            return team_inbox_assignment.assign_conversation_to_agent(
                worker,
                conversation=conversation,
                service_team_id=team_id,
                person_id=agent_id,
                now=now,
            ).kind

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [pool.submit(expire), pool.submit(assign)]
        released = outcomes[0].result(timeout=20)
        assigned = outcomes[1].result(timeout=20)

    assert released == 1
    assert assigned == "reply_window_expired"
    with factory() as check:
        assert (
            check.query(InboxConversationAssignment)
            .filter(InboxConversationAssignment.conversation_id == conversation_id)
            .filter(InboxConversationAssignment.is_active.is_(True))
            .count()
            == 0
        )
