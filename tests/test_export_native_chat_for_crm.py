from pathlib import Path

import pytest

from app.models.subscriber import Subscriber
from app.models.team_inbox import (
    InboxChannelType,
    InboxConversation,
    InboxMessage,
    InboxMessageDirection,
)
from scripts.one_off.export_native_chat_for_crm import (
    SCHEMA,
    build_export,
    write_private_export,
)


def test_export_contains_only_populated_native_chat(db_session, tmp_path: Path):
    subscriber = Subscriber(
        first_name="Export",
        last_name="Customer",
        email="export@example.com",
    )
    db_session.add(subscriber)
    db_session.flush()
    populated = InboxConversation(
        subscriber_id=subscriber.id,
        channel_type=InboxChannelType.chat_widget.value,
        subject="Chat with customer",
        metadata_={"surface": "customer", "customer_name": "Private Name"},
    )
    empty = InboxConversation(
        subscriber_id=subscriber.id,
        channel_type=InboxChannelType.chat_widget.value,
    )
    db_session.add_all([populated, empty])
    db_session.flush()
    db_session.add(
        InboxMessage(
            conversation_id=populated.id,
            channel_type=InboxChannelType.chat_widget.value,
            direction=InboxMessageDirection.inbound.value,
            body="Need assistance",
            metadata_={"client_message_id": "client-1"},
        )
    )
    db_session.flush()

    payload = build_export(db_session)

    assert payload["schema"] == SCHEMA
    assert payload["conversation_count"] == 1
    assert payload["message_count"] == 1
    assert payload["conversations"][0]["source_conversation_id"] == str(populated.id)
    assert payload["conversations"][0]["messages"][0]["body"] == "Need assistance"
    assert "customer_name" not in payload["conversations"][0]["metadata"]

    output = tmp_path / "history.json"
    write_private_export(output, payload)
    assert output.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        write_private_export(output, payload)


def test_export_fails_closed_without_subscriber_identity(db_session):
    conversation = InboxConversation(
        channel_type=InboxChannelType.chat_widget.value,
    )
    db_session.add(conversation)
    db_session.flush()
    db_session.add(
        InboxMessage(
            conversation_id=conversation.id,
            channel_type=InboxChannelType.chat_widget.value,
            direction=InboxMessageDirection.inbound.value,
            body="Unmapped",
        )
    )
    db_session.flush()

    with pytest.raises(RuntimeError, match="no subscriber identity"):
        build_export(db_session)
