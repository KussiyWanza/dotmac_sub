"""PostgreSQL lock and retry contract for Team Inbox operator replies."""

from __future__ import annotations

from time import monotonic
from uuid import uuid4

import pytest
from sqlalchemy.orm import sessionmaker

from app.models.team_inbox import (
    InboxConversation,
    InboxConversationStatus,
    InboxMessage,
    InboxMessageDirection,
)
from app.services import team_inbox_commands, team_inbox_outbound


def test_locked_conversation_fails_fast_and_same_key_retries_once(engine, monkeypatch):
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with session_factory() as setup:
        conversation = InboxConversation(
            channel_type="email",
            subject="Concurrency contract",
            status=InboxConversationStatus.open.value,
            contact_address=f"reply-concurrency-{uuid4().hex[:12]}@example.test",
            is_active=True,
        )
        setup.add(conversation)
        setup.commit()
        conversation_id = conversation.id

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
        actor_person_id=uuid4(),
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
