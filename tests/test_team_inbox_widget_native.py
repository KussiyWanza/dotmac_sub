from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.subscriber import Subscriber
from app.models.team_inbox import (
    InboxChannelType,
    InboxConversation,
    InboxConversationStatus,
    InboxMessage,
    InboxMessageDirection,
)
from app.services import team_inbox_operations, team_inbox_widget


@contextmanager
def _chat_enabled(enabled: bool = True):
    from app.config import settings

    saved = settings.chat_live_enabled
    object.__setattr__(settings, "chat_live_enabled", enabled)
    try:
        yield
    finally:
        object.__setattr__(settings, "chat_live_enabled", saved)


def _subscriber(db_session) -> Subscriber:
    sub = Subscriber(
        first_name="Ada",
        last_name="Nwosu",
        display_name="Ada Nwosu",
        email="ada@example.com",
        phone="0803 555 0114",
        is_active=True,
    )
    db_session.add(sub)
    db_session.flush()
    return sub


def test_native_customer_chat_session_creates_team_inbox_conversation(db_session):
    sub = _subscriber(db_session)

    with _chat_enabled():
        result = team_inbox_widget.broker_customer_session(
            db_session,
            str(sub.id),
            ticket_id="ticket-123",
        )

    conversation = db_session.query(InboxConversation).one()
    assert result["api_base"] == "/widget"
    assert result["ws_url"] == "/ws/inbox"
    assert result["conversation_id"] == str(conversation.id)
    assert conversation.channel_type == InboxChannelType.chat_widget.value
    assert conversation.subscriber_id == sub.id
    assert conversation.metadata_["ticket_id"] == "ticket-123"
    assert conversation.metadata_["source"] == "native_chat_widget"


def test_widget_token_lists_and_sends_messages(db_session):
    sub = _subscriber(db_session)
    with _chat_enabled():
        session = team_inbox_widget.broker_customer_session(db_session, str(sub.id))
        principal = team_inbox_widget.decode_widget_token(
            db_session,
            str(session["visitor_token"]),
        )

        sent = team_inbox_widget.add_visitor_message(
            db_session,
            principal=principal,
            body="My router is down",
            client_message_id="client-1",
        )
        messages = team_inbox_widget.list_session_messages(
            db_session,
            principal=principal,
        )

    assert sent["client_message_id"] == "client-1"
    assert sent["direction"] == InboxMessageDirection.inbound.value
    assert messages["messages"][0]["body"] == "My router is down"
    assert messages["messages"][0]["sender_type"] == "visitor"


def test_existing_native_token_cannot_write_after_crm_authority_cutover(db_session):
    from app.services.settings_cache import SettingsCache

    sub = _subscriber(db_session)
    with _chat_enabled():
        session = team_inbox_widget.broker_customer_session(db_session, str(sub.id))
        principal = team_inbox_widget.decode_widget_token(
            db_session,
            str(session["visitor_token"]),
        )
        db_session.add(
            DomainSetting(
                domain=SettingDomain.comms,
                key="chat_session_authority",
                value_text="crm",
                is_active=True,
            )
        )
        db_session.commit()
        SettingsCache.invalidate(SettingDomain.comms.value, "chat_session_authority")

        with pytest.raises(team_inbox_widget.TeamInboxWidgetError) as exc:
            team_inbox_widget.add_visitor_message(
                db_session,
                principal=principal,
                body="Must not be stored locally",
            )

    assert exc.value.code == "communications.team_inbox_widget.authority_external"
    assert db_session.query(InboxMessage).count() == 0


def test_widget_satisfaction_requires_resolved_conversation(db_session):
    sub = _subscriber(db_session)
    with _chat_enabled():
        session = team_inbox_widget.broker_customer_session(db_session, str(sub.id))
    principal = team_inbox_widget.decode_widget_token(
        db_session,
        str(session["visitor_token"]),
    )
    conversation = db_session.get(InboxConversation, principal.conversation_id)

    with pytest.raises(team_inbox_operations.InboxOperationError):
        team_inbox_operations.set_satisfaction(
            db_session,
            conversation=conversation,
            rating=5,
        )

    conversation.status = InboxConversationStatus.resolved.value
    team_inbox_operations.set_satisfaction(
        db_session,
        conversation=conversation,
        rating=5,
        comment="Great help",
        actor=principal.subscriber_id,
    )

    assert conversation.metadata_["csat"]["rating"] == 5
    assert conversation.metadata_["csat"]["comment"] == "Great help"


def test_auto_resolve_skips_conversations_needing_response(db_session):
    now = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
    stale_agent_reply = InboxConversation(
        channel_type=InboxChannelType.email.value,
        status=InboxConversationStatus.pending.value,
        subject="Waiting",
        last_message_at=now - timedelta(hours=80),
    )
    stale_customer_reply = InboxConversation(
        channel_type=InboxChannelType.email.value,
        status=InboxConversationStatus.open.value,
        subject="Needs response",
        last_message_at=now - timedelta(hours=80),
    )
    db_session.add_all([stale_agent_reply, stale_customer_reply])
    db_session.flush()
    db_session.add_all(
        [
            InboxMessage(
                conversation_id=stale_agent_reply.id,
                channel_type=InboxChannelType.email.value,
                direction=InboxMessageDirection.outbound.value,
                body="We fixed this.",
            ),
            InboxMessage(
                conversation_id=stale_customer_reply.id,
                channel_type=InboxChannelType.email.value,
                direction=InboxMessageDirection.inbound.value,
                body="Still down.",
            ),
        ]
    )
    db_session.flush()

    count = team_inbox_operations.auto_resolve_stale_conversations(
        db_session,
        stale_hours=72,
        now=now,
    )

    assert count == 1
    assert stale_agent_reply.status == InboxConversationStatus.resolved.value
    assert stale_customer_reply.status == InboxConversationStatus.open.value


def test_chat_disabled_returns_503(db_session):
    sub = _subscriber(db_session)

    with _chat_enabled(False):
        with pytest.raises(team_inbox_widget.TeamInboxWidgetError) as exc:
            team_inbox_widget.broker_customer_session(db_session, str(sub.id))

    assert exc.value.code == "communications.team_inbox_widget.disabled"
