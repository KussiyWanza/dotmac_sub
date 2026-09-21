"""PostgreSQL lock and retry contract for Team Inbox operator replies."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier
from time import monotonic
from uuid import uuid4

import pytest
from sqlalchemy.orm import sessionmaker

from app.models.service_team import ServiceTeam, ServiceTeamMember, ServiceTeamType
from app.models.team_inbox import (
    InboxAgentPresence,
    InboxConversation,
    InboxConversationAssignment,
    InboxConversationStatus,
    InboxMessage,
    InboxMessageDirection,
)
from app.services import team_inbox_commands, team_inbox_outbound
from tests.staff_identity_fixtures import add_bound_staff_user


def _session_factory(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def test_locked_conversation_fails_fast_and_same_key_retries_once(engine, monkeypatch):
    session_factory = _session_factory(engine)
    with session_factory() as setup:
        team = ServiceTeam(
            name=f"Reply Concurrency {uuid4().hex[:8]}",
            team_type=ServiceTeamType.support.value,
        )
        setup.add(team)
        agent, person = add_bound_staff_user(setup)
        conversation = InboxConversation(
            channel_type="email",
            subject="Concurrency contract",
            status=InboxConversationStatus.open.value,
            contact_address=f"reply-concurrency-{uuid4().hex[:12]}@example.test",
            is_active=True,
            primary_service_team_id=team.id,
        )
        setup.add_all(
            [
                ServiceTeamMember(team_id=team.id, person_id=person.id),
                InboxAgentPresence(
                    person_id=agent.id,
                    status="online",
                    manual_override_status="online",
                    last_seen_at=datetime.now(UTC),
                ),
                conversation,
            ]
        )
        setup.commit()
        conversation_id = conversation.id
        agent_id = agent.id

    calls = 0

    def fake_send(db, *, conversation, payload, record_failure):
        nonlocal calls
        calls += 1
        message = InboxMessage(
            conversation_id=conversation.id,
            channel_type="email",
            direction=InboxMessageDirection.outbound.value,
            body=payload.body_text,
            from_address="support@example.test",
            to_addresses=[conversation.contact_address],
            cc_addresses=[],
            metadata_={
                **dict(payload.metadata or {}),
                "body_text": payload.body_text,
                "delivery_status": "queued",
                "sent_by_person_id": str(payload.sent_by_person_id),
            },
        )
        db.add(message)
        db.flush()
        return team_inbox_outbound.InboxReplyResult(
            kind="queued",
            conversation_id=str(conversation.id),
            message_id=str(message.id),
            from_address=message.from_address,
        )

    monkeypatch.setattr(
        team_inbox_commands.team_inbox_outbound,
        "send_inbox_reply",
        fake_send,
    )
    command = team_inbox_commands.ReplyCommand(
        conversation_id=conversation_id,
        body_text="One durable reply.",
        actor_person_id=agent_id,
        idempotency_key=f"reply-concurrency:{uuid4()}",
    )

    with session_factory() as holder:
        holder.query(InboxConversation).filter(
            InboxConversation.id == conversation_id
        ).with_for_update().one()
        started = monotonic()
        with session_factory() as contender:
            with pytest.raises(team_inbox_commands.ConversationBusyError):
                team_inbox_commands.reply(contender, command=command)
            assert monotonic() - started < 2
            assert not contender.in_transaction()
        holder.rollback()

    with session_factory() as retry:
        first = team_inbox_commands.reply(retry, command=command)
        replay = team_inbox_commands.reply(retry, command=command)

    assert first.replayed is False
    assert replay.replayed is True
    assert replay.message_id == first.message_id
    assert calls == 1
    with session_factory() as check:
        assert (
            check.query(InboxMessage)
            .filter(InboxMessage.conversation_id == conversation_id)
            .filter(InboxMessage.direction == InboxMessageDirection.outbound.value)
            .count()
            == 1
        )
        assignment = (
            check.query(InboxConversationAssignment)
            .filter(InboxConversationAssignment.conversation_id == conversation_id)
            .one()
        )
        assert assignment.conversation_id == conversation_id
        assert assignment.person_id == agent_id
        assert assignment.is_active is True


def test_two_agents_replying_simultaneously_only_one_claims_and_sends(
    engine, monkeypatch
):
    session_factory = _session_factory(engine)
    with session_factory() as setup:
        team = ServiceTeam(
            name=f"Reply Race {uuid4().hex[:8]}",
            team_type=ServiceTeamType.support.value,
        )
        setup.add(team)
        setup.flush()
        agents = []
        rows = []
        for index in range(2):
            agent, person = add_bound_staff_user(setup)
            agent.display_name = f"Race Agent {index + 1}"
            agents.append(agent.id)
            rows.extend(
                [
                    ServiceTeamMember(team_id=team.id, person_id=person.id),
                    InboxAgentPresence(
                        person_id=agent.id,
                        status="online",
                        manual_override_status="online",
                        last_seen_at=datetime.now(UTC),
                    ),
                ]
            )
        conversation = InboxConversation(
            channel_type="email",
            subject="Simultaneous reply contract",
            status=InboxConversationStatus.open.value,
            contact_address=f"reply-race-{uuid4().hex[:12]}@example.test",
            primary_service_team_id=team.id,
            is_active=True,
        )
        setup.add_all([*rows, conversation])
        setup.commit()
        conversation_id = conversation.id
        agent_ids = tuple(agents)

    def fake_send(db, *, conversation, payload, record_failure):
        message = InboxMessage(
            conversation_id=conversation.id,
            channel_type="email",
            direction=InboxMessageDirection.outbound.value,
            body=payload.body_text,
            from_address="support@example.test",
            to_addresses=[conversation.contact_address],
            cc_addresses=[],
            metadata_={
                **dict(payload.metadata or {}),
                "body_text": payload.body_text,
                "delivery_status": "queued",
                "sent_by_person_id": str(payload.sent_by_person_id),
            },
        )
        db.add(message)
        db.flush()
        return team_inbox_outbound.InboxReplyResult(
            kind="queued",
            conversation_id=str(conversation.id),
            message_id=str(message.id),
            from_address=message.from_address,
        )

    monkeypatch.setattr(team_inbox_outbound, "send_inbox_reply", fake_send)
    barrier = Barrier(2)

    def attempt(agent_id):
        with session_factory() as worker:
            barrier.wait(timeout=10)
            try:
                team_inbox_commands.reply(
                    worker,
                    command=team_inbox_commands.ReplyCommand(
                        conversation_id=conversation_id,
                        body_text=f"Reply from {agent_id}",
                        actor_person_id=agent_id,
                    ),
                )
            except team_inbox_commands.ConversationAssignedToAnotherAgentError:
                return "conflict", agent_id
            return "sent", agent_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, agent_ids))

    assert sorted(kind for kind, _agent_id in outcomes) == ["conflict", "sent"]
    winner_id = next(agent_id for kind, agent_id in outcomes if kind == "sent")
    with session_factory() as check:
        assignment = (
            check.query(InboxConversationAssignment)
            .filter(InboxConversationAssignment.conversation_id == conversation_id)
            .one()
        )
        assert assignment.person_id == winner_id
        assert (
            check.query(InboxMessage)
            .filter(InboxMessage.conversation_id == conversation_id)
            .count()
            == 1
        )
