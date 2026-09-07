from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.models.party import Party
from app.models.sales import Lead
from app.models.service_team import ServiceTeam, ServiceTeamType
from app.models.subscriber import Subscriber
from app.models.team_inbox import (
    InboxChannelType,
    InboxConversation,
    InboxConversationAssignment,
    InboxConversationLeadLink,
    InboxConversationStatus,
    InboxMessage,
    InboxMessageDirection,
)
from app.services import (
    team_inbox_channel_receive,
    team_inbox_operations,
    team_inbox_outbound,
    team_inbox_widget,
)


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


def test_widget_delivers_and_labels_ai_intake_messages(db_session):
    sub = _subscriber(db_session)
    with _chat_enabled():
        session = team_inbox_widget.broker_customer_session(db_session, str(sub.id))
        principal = team_inbox_widget.decode_widget_token(
            db_session,
            str(session["visitor_token"]),
        )
        conversation = db_session.get(InboxConversation, principal.conversation_id)
        result = team_inbox_outbound.send_ai_intake_message(
            db_session,
            conversation=conversation,
            body_text="Hello, I am the Dotmac Virtual Assistant.",
            metadata={
                "sender_type": "ai",
                "author_name": "Dotmac Virtual Assistant",
            },
            dedupe_key=f"widget-ai-test:{conversation.id}",
        )
        messages = team_inbox_widget.list_session_messages(
            db_session,
            principal=principal,
        )

    assert result.kind == "queued"
    assert messages["messages"][0]["sender_type"] == "ai"
    assert messages["messages"][0]["author_name"] == "Dotmac Virtual Assistant"


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
    stale_system_reply = InboxConversation(
        channel_type=InboxChannelType.whatsapp.value,
        status=InboxConversationStatus.open.value,
        subject="Queue notice",
        last_message_at=now - timedelta(hours=80),
    )
    db_session.add_all([stale_agent_reply, stale_customer_reply, stale_system_reply])
    db_session.flush()
    db_session.add_all(
        [
            InboxMessage(
                conversation_id=stale_agent_reply.id,
                channel_type=InboxChannelType.email.value,
                direction=InboxMessageDirection.outbound.value,
                body="We fixed this.",
                metadata_={"sent_by_person_id": str(uuid4())},
            ),
            InboxMessage(
                conversation_id=stale_customer_reply.id,
                channel_type=InboxChannelType.email.value,
                direction=InboxMessageDirection.inbound.value,
                body="Still down.",
            ),
            InboxMessage(
                conversation_id=stale_system_reply.id,
                channel_type=InboxChannelType.whatsapp.value,
                direction=InboxMessageDirection.outbound.value,
                body="You are still in the queue.",
                metadata_={
                    "sender_type": "ai",
                    "automation_kind": "queue_notification",
                },
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
    assert stale_system_reply.status == InboxConversationStatus.open.value


def test_auto_resolve_skips_stale_conversation_with_active_assignment(db_session):
    now = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
    conversation = InboxConversation(
        channel_type=InboxChannelType.email.value,
        status=InboxConversationStatus.pending.value,
        subject="Agent is still working",
        last_message_at=now - timedelta(hours=80),
    )
    team = ServiceTeam(name="Assigned Support", team_type=ServiceTeamType.support.value)
    db_session.add_all([conversation, team])
    db_session.flush()
    db_session.add_all(
        [
            InboxConversationAssignment(
                conversation_id=conversation.id,
                service_team_id=team.id,
                person_id=uuid4(),
                assigned_at=now - timedelta(hours=81),
                is_active=True,
            ),
            InboxMessage(
                conversation_id=conversation.id,
                channel_type=InboxChannelType.email.value,
                direction=InboxMessageDirection.outbound.value,
                body="I am still investigating this.",
                metadata_={"sent_by_person_id": str(uuid4())},
            ),
        ]
    )
    db_session.flush()

    count = team_inbox_operations.auto_resolve_stale_conversations(
        db_session,
        stale_hours=72,
        now=now,
    )

    assert count == 0
    assert conversation.status == InboxConversationStatus.pending.value
    assert "auto_resolved_at" not in (conversation.metadata_ or {})


def test_chat_disabled_returns_503(db_session):
    sub = _subscriber(db_session)

    with _chat_enabled(False):
        with pytest.raises(team_inbox_widget.TeamInboxWidgetError) as exc:
            team_inbox_widget.broker_customer_session(db_session, str(sub.id))

    assert exc.value.code == "communications.team_inbox_widget.disabled"


def _fiber_chat_command(
    *,
    client_session_id=None,
    email: str = "prospect@example.com",
    phone: str | None = "08031234567",
) -> team_inbox_widget.FiberWidgetSessionCommand:
    import uuid

    return team_inbox_widget.FiberWidgetSessionCommand(
        client_session_id=client_session_id or uuid.uuid4(),
        full_name="Fiber Chat Prospect",
        email=email,
        phone=phone,
        message="Can I get fiber at my address?",
        page_url="https://fiber.dotmac.ng/coverage/",
        referrer_url="https://www.google.com/",
        started_at=datetime.now(UTC) - timedelta(seconds=5),
        actor="transport:test-fiber-widget",
    )


def test_fiber_widget_unmatched_visitor_creates_party_lead_and_chat(db_session):
    command = _fiber_chat_command()

    with _chat_enabled():
        outcome = team_inbox_widget.broker_fiber_visitor_session_committed(
            db_session,
            command=command,
        )

    conversation = db_session.get(InboxConversation, outcome.conversation_id)
    message = db_session.get(InboxMessage, outcome.message_id)
    assert conversation.channel_type == InboxChannelType.chat_widget.value
    assert conversation.subscriber_id is None
    assert conversation.metadata_["surface"] == "fiber_website"
    assert conversation.metadata_["contact_resolution"]["status"] == "unmatched"
    assert message.body == "Can I get fiber at my address?"
    assert db_session.query(Party).count() == 1
    assert db_session.query(Lead).count() == 1
    assert db_session.query(InboxConversationLeadLink).count() == 1


def test_fiber_widget_first_message_enters_ai_intake_admission(db_session, monkeypatch):
    observed: list[tuple[str, str, bool]] = []

    def capture_admission(
        db,
        *,
        command: team_inbox_channel_receive.PersistedWidgetAiIntakeCommand,
    ) -> None:
        observed.append(
            (
                str(command.conversation_id),
                str(command.message_id),
                command.created_conversation,
            )
        )

    monkeypatch.setattr(
        team_inbox_channel_receive,
        "start_ai_intake_for_persisted_widget_message",
        capture_admission,
    )
    with _chat_enabled():
        outcome = team_inbox_widget.broker_fiber_visitor_session_committed(
            db_session,
            command=_fiber_chat_command(),
        )

    assert observed == [(outcome.conversation_id, outcome.message_id, True)]


def test_customer_widget_first_message_enters_ai_intake_admission(
    db_session, monkeypatch
):
    sub = _subscriber(db_session)
    observed: list[tuple[str, str, bool]] = []

    def capture_admission(
        db,
        *,
        command: team_inbox_channel_receive.PersistedWidgetAiIntakeCommand,
    ) -> None:
        observed.append(
            (
                str(command.conversation_id),
                str(command.message_id),
                command.created_conversation,
            )
        )

    monkeypatch.setattr(
        team_inbox_channel_receive,
        "start_ai_intake_for_persisted_widget_message",
        capture_admission,
    )
    with _chat_enabled():
        session = team_inbox_widget.broker_customer_session(db_session, str(sub.id))
        principal = team_inbox_widget.decode_widget_token(
            db_session,
            str(session["visitor_token"]),
        )
        team_inbox_widget.add_visitor_message(
            db_session,
            principal=principal,
            body="My internet is not browsing.",
        )

    assert len(observed) == 1
    conversation_id, message_id, created_conversation = observed[0]
    assert conversation_id == str(principal.conversation_id)
    assert message_id
    assert created_conversation is True


def test_fiber_widget_exact_subscriber_match_creates_no_prospect(db_session):
    subscriber = _subscriber(db_session)
    db_session.commit()
    command = _fiber_chat_command(
        email="ADA@example.com",
        phone="08035550114",
    )

    with _chat_enabled():
        outcome = team_inbox_widget.broker_fiber_visitor_session_committed(
            db_session,
            command=command,
        )

    conversation = db_session.get(InboxConversation, outcome.conversation_id)
    assert outcome.resolution_status == "linked_subscriber"
    assert conversation.subscriber_id == subscriber.id
    assert db_session.query(Lead).count() == 0
    assert db_session.query(Party).count() == 0


def test_fiber_widget_conflicting_matches_fail_closed(db_session):
    _subscriber(db_session)
    other = Subscriber(
        first_name="Other",
        last_name="Customer",
        display_name="Other Customer",
        email="other@example.com",
        phone="0803 000 0002",
        is_active=True,
    )
    db_session.add(other)
    db_session.commit()
    command = _fiber_chat_command(
        email="ada@example.com",
        phone="08030000002",
    )

    with _chat_enabled():
        outcome = team_inbox_widget.broker_fiber_visitor_session_committed(
            db_session,
            command=command,
        )

    conversation = db_session.get(InboxConversation, outcome.conversation_id)
    assert outcome.resolution_status == "identity_review_required"
    assert conversation.subscriber_id is None
    assert conversation.metadata_["identity_review_required"] is True
    assert db_session.query(Lead).count() == 0
    assert db_session.query(Party).count() == 0


def test_fiber_widget_session_start_replays_by_client_session_id(db_session):
    command = _fiber_chat_command()

    with _chat_enabled():
        first = team_inbox_widget.broker_fiber_visitor_session_committed(
            db_session,
            command=command,
        )
        replay = team_inbox_widget.broker_fiber_visitor_session_committed(
            db_session,
            command=command,
        )

    assert replay.replayed is True
    assert replay.conversation_id == first.conversation_id
    assert replay.message_id == first.message_id
    assert db_session.query(InboxConversation).count() == 1
    assert db_session.query(InboxMessage).count() == 1
    assert db_session.query(Lead).count() == 1


def test_agent_reply_reaches_fiber_widget_session_history(db_session):
    command = _fiber_chat_command()
    with _chat_enabled():
        session = team_inbox_widget.broker_fiber_visitor_session_committed(
            db_session,
            command=command,
        )
        conversation = db_session.get(InboxConversation, session.conversation_id)
        reply = team_inbox_outbound.send_inbox_reply(
            db_session,
            conversation=conversation,
            payload=team_inbox_outbound.InboxReplyPayload(
                body_html="",
                body_text="Yes, please send your installation address.",
                metadata={"author_name": "Fiber Support"},
            ),
        )
        principal = team_inbox_widget.decode_widget_token(
            db_session,
            session.visitor_token,
        )
        history = team_inbox_widget.list_session_messages(
            db_session,
            principal=principal,
        )

    assert reply.kind == "queued"
    assert [message["direction"] for message in history["messages"]] == [
        InboxMessageDirection.inbound.value,
        InboxMessageDirection.outbound.value,
    ]
    assert history["messages"][-1]["body"] == (
        "Yes, please send your installation address."
    )
