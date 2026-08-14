from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import OperationalError

from app.models.team_inbox import (
    InboxConversation,
    InboxConversationStatus,
)
from app.services import team_inbox_commands, team_inbox_outbound


def _conversation(db_session, *, contact_address: str | None = "ada@example.com"):
    conversation = InboxConversation(
        channel_type="email",
        subject="Need help",
        status=InboxConversationStatus.open.value,
        contact_address=contact_address,
    )
    db_session.add(conversation)
    db_session.flush()
    return conversation


def test_status_command_owns_history_and_no_op_behavior(db_session):
    actor_id = uuid.uuid4()
    conversation = _conversation(db_session)
    conversation_id = conversation.id
    db_session.commit()

    changed = team_inbox_commands.update_status(
        db_session,
        conversation_id=conversation_id,
        status_value=InboxConversationStatus.pending.value,
        actor_person_id=actor_id,
    )
    unchanged = team_inbox_commands.update_status(
        db_session,
        conversation_id=conversation_id,
        status_value=InboxConversationStatus.pending.value,
        actor_person_id=actor_id,
    )

    db_session.refresh(conversation)
    assert changed.already_set is False
    assert unchanged.already_set is True
    assert conversation.status == InboxConversationStatus.pending.value
    assert conversation.metadata_["status_history"] == [
        {
            "from": InboxConversationStatus.open.value,
            "to": InboxConversationStatus.pending.value,
            "at": conversation.metadata_["status_history"][0]["at"],
            "actor_id": str(actor_id),
            "source": "admin_inbox_status_action",
        }
    ]


def test_rejected_reply_rolls_back_the_command_transaction(monkeypatch, db_session):
    conversation = InboxConversation(
        channel_type="email",
        subject="Need help",
        status=InboxConversationStatus.open.value,
        contact_address="ada@example.com",
        is_active=True,
    )
    conversation.id = uuid.uuid4()
    conversation_id = conversation.id
    db_session.add(conversation)
    db_session.commit()
    monkeypatch.setattr(
        team_inbox_commands.team_inbox_outbound,
        "send_inbox_reply",
        lambda *args, **kwargs: team_inbox_outbound.InboxReplyResult(
            kind="failed",
            conversation_id=str(conversation_id),
            reason="Provider rejected reply.",
        ),
    )

    with pytest.raises(team_inbox_commands.InboxCommandRejected):
        team_inbox_commands.reply(
            db_session,
            command=team_inbox_commands.ReplyCommand(
                conversation_id=conversation_id,
                body_text="We are checking this.",
                actor_person_id=uuid.uuid4(),
            ),
        )

    assert db_session.get(InboxConversation, conversation_id) is not None
    assert db_session.query(InboxConversation).count() == 1


class _PostgresLockUnavailable(RuntimeError):
    sqlstate = "55P03"


def test_reply_maps_postgres_nowait_contention_to_retryable_domain_error(
    monkeypatch, db_session
):
    conversation = _conversation(db_session)
    conversation_id = conversation.id
    db_session.commit()

    def lock_unavailable(*_args, **_kwargs):
        raise OperationalError("SELECT", {}, _PostgresLockUnavailable())

    monkeypatch.setattr(team_inbox_commands, "_active_conversation", lock_unavailable)

    with pytest.raises(team_inbox_commands.ConversationBusyError) as exc:
        team_inbox_commands.reply(
            db_session,
            command=team_inbox_commands.ReplyCommand(
                conversation_id=conversation_id,
                body_text="We are checking this.",
                actor_person_id=uuid.uuid4(),
                idempotency_key="busy-reply-1",
            ),
        )

    assert exc.value.code == "communications.team_inbox_commands.conversation_busy"
    assert not db_session.in_transaction()


def test_reply_does_not_hide_unrelated_database_errors(monkeypatch, db_session):
    conversation = _conversation(db_session)
    conversation_id = conversation.id
    db_session.commit()

    def database_failure(*_args, **_kwargs):
        raise OperationalError("SELECT", {}, RuntimeError("connection lost"))

    monkeypatch.setattr(team_inbox_commands, "_active_conversation", database_failure)

    with pytest.raises(OperationalError):
        team_inbox_commands.reply(
            db_session,
            command=team_inbox_commands.ReplyCommand(
                conversation_id=conversation_id,
                body_text="We are checking this.",
                actor_person_id=uuid.uuid4(),
                idempotency_key="database-failure-1",
            ),
        )
