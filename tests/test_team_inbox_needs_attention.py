from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.models.support import Ticket
from app.models.team_inbox import (
    InboxConversation,
    InboxConversationStatus,
    InboxMessage,
    InboxMessageDirection,
)
from app.services import team_inbox_projection, team_inbox_read

START = datetime(2026, 7, 27, 9, 0, tzinfo=UTC)


def _message(
    conversation_id: uuid.UUID,
    *,
    direction: InboxMessageDirection,
    minute: int,
    metadata: dict | None = None,
    channel_type: str = "email",
) -> InboxMessage:
    occurred_at = START + timedelta(minutes=minute)
    return InboxMessage(
        conversation_id=conversation_id,
        channel_type=channel_type,
        direction=direction.value,
        body=f"{direction.value} at {minute}",
        sent_at=occurred_at if direction == InboxMessageDirection.outbound else None,
        received_at=occurred_at if direction == InboxMessageDirection.inbound else None,
        created_at=occurred_at,
        metadata_=metadata,
    )


def _conversation(
    db_session,
    *,
    channel_type: str = "email",
    metadata: dict | None = None,
) -> InboxConversation:
    conversation = InboxConversation(
        channel_type=channel_type,
        subject="Internet is down",
        contact_address="customer@example.test",
        status=InboxConversationStatus.open.value,
        metadata_=metadata,
    )
    db_session.add(conversation)
    db_session.flush()
    return conversation


def _add_completed_exchange_with_follow_up(
    db_session,
    conversation: InboxConversation,
) -> None:
    db_session.add_all(
        [
            _message(
                conversation.id,
                direction=InboxMessageDirection.inbound,
                minute=0,
                channel_type=conversation.channel_type,
            ),
            _message(
                conversation.id,
                direction=InboxMessageDirection.outbound,
                minute=1,
                channel_type=conversation.channel_type,
                metadata={
                    "sent_by_person_id": str(uuid.uuid4()),
                    "delivery_status": "queued",
                },
            ),
            _message(
                conversation.id,
                direction=InboxMessageDirection.inbound,
                minute=2,
                channel_type=conversation.channel_type,
            ),
        ]
    )
    conversation.first_message_at = START
    conversation.last_message_at = START + timedelta(minutes=2)


def _filtered_ids(db_session, **filters) -> set[str]:
    return {
        item.id
        for item in team_inbox_read.list_conversations(
            db_session,
            limit=100,
            **filters,
        ).items
    }


def test_follow_up_enters_needs_attention_and_agent_reply_clears_it(db_session):
    conversation = _conversation(db_session)
    _add_completed_exchange_with_follow_up(db_session, conversation)
    db_session.commit()
    conversation_id = str(conversation.id)

    result = team_inbox_read.list_conversations(
        db_session,
        needs_attention=True,
    )

    assert result.count == 1
    assert result.items[0].id == conversation_id
    assert result.items[0].needs_attention is True
    assert result.items[0].needs_response is False

    db_session.add(
        _message(
            conversation.id,
            direction=InboxMessageDirection.outbound,
            minute=3,
            metadata={
                "sent_by_person_id": str(uuid.uuid4()),
                "delivery_status": "delivered",
            },
        )
    )
    conversation.last_message_at = START + timedelta(minutes=3)
    db_session.commit()

    assert (
        team_inbox_read.list_conversations(
            db_session,
            needs_attention=True,
        ).count
        == 0
    )

    db_session.add(
        _message(
            conversation.id,
            direction=InboxMessageDirection.inbound,
            minute=4,
        )
    )
    conversation.last_message_at = START + timedelta(minutes=4)
    db_session.commit()

    assert (
        team_inbox_read.list_conversations(
            db_session,
            needs_attention=True,
        ).count
        == 1
    )


def test_unreplied_is_distinct_from_needs_attention(db_session):
    conversation = _conversation(db_session)
    db_session.add(
        _message(
            conversation.id,
            direction=InboxMessageDirection.inbound,
            minute=0,
        )
    )
    conversation.first_message_at = START
    conversation.last_message_at = START
    db_session.commit()
    conversation_id = str(conversation.id)

    assert _filtered_ids(db_session, needs_response=True) == {conversation_id}
    assert _filtered_ids(db_session, needs_attention=True) == set()


@pytest.mark.parametrize(
    "metadata",
    [
        {
            "sent_by_person_id": "agent",
            "delivery_status": "failed",
        },
        {
            "sent_by_person_id": "agent",
            "delivery_status": "scheduled",
        },
        {
            "sent_by_person_id": "agent",
            "delivery_status": "delivered",
            "ai_intake": True,
        },
        {
            "sent_by_person_id": "agent",
            "delivery_status": "delivered",
            "response_required": False,
        },
    ],
)
def test_invalid_agent_replies_do_not_create_needs_attention(
    db_session,
    metadata,
):
    conversation = _conversation(db_session)
    db_session.add_all(
        [
            _message(
                conversation.id,
                direction=InboxMessageDirection.inbound,
                minute=0,
            ),
            _message(
                conversation.id,
                direction=InboxMessageDirection.outbound,
                minute=1,
                metadata=metadata,
            ),
            _message(
                conversation.id,
                direction=InboxMessageDirection.inbound,
                minute=2,
            ),
        ]
    )
    conversation.first_message_at = START
    conversation.last_message_at = START + timedelta(minutes=2)
    db_session.commit()
    conversation_id = str(conversation.id)

    assert _filtered_ids(db_session, needs_attention=True) == set()
    assert _filtered_ids(db_session, needs_response=True) == {conversation_id}


@pytest.mark.parametrize(
    ("status", "snoozed_until", "is_active"),
    [
        (InboxConversationStatus.resolved.value, None, True),
        (
            InboxConversationStatus.snoozed.value,
            START + timedelta(hours=1),
            True,
        ),
        (InboxConversationStatus.open.value, None, False),
    ],
)
def test_inactive_lifecycle_states_leave_needs_attention(
    db_session,
    status,
    snoozed_until,
    is_active,
):
    conversation = _conversation(db_session)
    _add_completed_exchange_with_follow_up(db_session, conversation)
    conversation.status = status
    conversation.snoozed_until = snoozed_until
    conversation.is_active = is_active
    db_session.commit()

    assert _filtered_ids(db_session, needs_attention=True) == set()


def test_ticket_handoff_leaves_needs_attention(db_session):
    conversation = _conversation(db_session)
    _add_completed_exchange_with_follow_up(db_session, conversation)
    db_session.add(
        Ticket(
            title="Internet is down",
            origin_conversation_id=conversation.id,
            is_active=False,
        )
    )
    db_session.commit()

    assert _filtered_ids(db_session, needs_attention=True) == set()


def test_social_comments_are_excluded_but_direct_messages_are_included(db_session):
    facebook_comment = _conversation(
        db_session,
        channel_type="facebook_messenger",
        metadata={"interaction_type": "comment"},
    )
    instagram_comment = _conversation(
        db_session,
        channel_type="instagram_dm",
        metadata={"surface": "instagram_comment"},
    )
    direct_message = _conversation(
        db_session,
        channel_type="instagram_dm",
    )
    _add_completed_exchange_with_follow_up(db_session, facebook_comment)
    _add_completed_exchange_with_follow_up(db_session, instagram_comment)
    _add_completed_exchange_with_follow_up(db_session, direct_message)
    db_session.commit()

    assert _filtered_ids(db_session, needs_attention=True) == {str(direct_message.id)}


def test_projection_count_matches_needs_attention_filter(db_session):
    conversation = _conversation(db_session)
    _add_completed_exchange_with_follow_up(db_session, conversation)
    db_session.commit()

    projection = team_inbox_projection.build_queue_projection(
        db_session,
        team_inbox_projection.InboxQueueRequest(needs_attention=True),
    )

    assert projection.count == 1
    assert projection.assignment_counts.needs_attention == 1
    assert projection.needs_attention is True
    assert projection.list_query.filter_value("needs_attention") == "true"


def test_attention_classification_hydrates_candidates_in_bounded_batches(
    db_session,
    monkeypatch,
):
    conversations = [_conversation(db_session) for _ in range(5)]
    for conversation in conversations:
        _add_completed_exchange_with_follow_up(db_session, conversation)
    db_session.commit()

    hydrated_batch_sizes: list[int] = []
    original = team_inbox_read._messages_by_conversation

    def record_batch(db, conversation_ids):
        hydrated_batch_sizes.append(len(conversation_ids))
        return original(db, conversation_ids)

    monkeypatch.setattr(
        team_inbox_read,
        "_messages_by_conversation",
        record_batch,
    )

    result = team_inbox_read.needs_attention_conversation_ids(
        db_session,
        batch_size=2,
    )

    assert set(result) == {conversation.id for conversation in conversations}
    assert hydrated_batch_sizes == [2, 2, 1]
