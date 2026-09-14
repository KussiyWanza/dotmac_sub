"""PostgreSQL locking proof for WhatsApp inbound-versus-expiry races."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

from sqlalchemy.orm import sessionmaker

from app.models.service_team import ServiceTeam, ServiceTeamType
from app.models.team_inbox import (
    InboxConversation,
    InboxConversationAssignment,
    InboxMessage,
    InboxMessageDirection,
)
from app.services import (
    team_inbox_assignment,
    team_inbox_channel_receive,
    team_inbox_reply_window,
)


def test_customer_inbound_and_expiry_converge_on_open_unassigned_window(engine) -> None:
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    thread_id = f"whatsapp:expiry-race:{uuid4()}"
    endpoint = f"+23480{uuid4().int % 10**8:08d}"
    with factory() as setup:
        team = ServiceTeam(
            name=f"Expiry Race {uuid4().hex[:8]}",
            team_type=ServiceTeamType.support.value,
        )
        setup.add(team)
        setup.flush()
        conversation = InboxConversation(
            channel_type="whatsapp",
            status="open",
            is_active=True,
            contact_address=endpoint,
            external_thread_id=thread_id,
            primary_service_team_id=team.id,
            first_message_at=now - timedelta(hours=25),
            last_message_at=now - timedelta(hours=25),
        )
        setup.add(conversation)
        setup.flush()
        old_message = InboxMessage(
            conversation_id=conversation.id,
            channel_type="whatsapp",
            direction=InboxMessageDirection.inbound.value,
            body="Initial enquiry",
            received_at=now - timedelta(hours=25),
            metadata_={"reply_window_qualifying": True},
        )
        assignment = InboxConversationAssignment(
            conversation_id=conversation.id,
            service_team_id=team.id,
            person_id=uuid4(),
            assigned_at=now - timedelta(hours=25),
            is_active=True,
        )
        setup.add_all([old_message, assignment])
        setup.commit()
        conversation_id = conversation.id
        assignment_id = assignment.id

    ready = Barrier(2)

    def expire() -> str:
        with factory() as session:
            ready.wait(timeout=10)
            result = team_inbox_assignment.release_expired_whatsapp_conversation(
                session,
                team_inbox_assignment.ReleaseExpiredWhatsAppConversationCommand(
                    conversation_id=conversation_id,
                    occurred_at=now,
                ),
            )
            session.commit()
            return "released" if result.assignment_released else "current_window"

    def receive() -> str:
        with factory() as session:
            ready.wait(timeout=10)
            result = team_inbox_channel_receive.receive_inbound_channel(
                session,
                team_inbox_channel_receive.InboundChannelPayload(
                    channel_type="whatsapp",
                    contact_address=endpoint,
                    body="I am back",
                    external_message_id=f"wamid.expiry-race.{uuid4()}",
                    external_thread_id=thread_id,
                    received_at=now + timedelta(seconds=1),
                    metadata={"reply_window_qualifying": True},
                ),
            )
            session.commit()
            return result.conversation_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        expiry_future = pool.submit(expire)
        inbound_future = pool.submit(receive)
        expiry_outcome = expiry_future.result(timeout=30)
        inbound_conversation_id = inbound_future.result(timeout=30)

    assert expiry_outcome in {"released", "current_window"}
    assert inbound_conversation_id == str(conversation_id)
    with factory() as check:
        conversation = check.get(InboxConversation, conversation_id)
        assignment = check.get(InboxConversationAssignment, assignment_id)
        assert conversation is not None
        assert assignment is not None
        assert assignment.is_active is False
        assert conversation.status == "open"
        assert (
            check.query(InboxMessage).filter_by(conversation_id=conversation_id).count()
            == 2
        )
        assert (
            team_inbox_reply_window.decide_reply_window(
                check, conversation=conversation, now=now + timedelta(seconds=1)
            ).status
            is team_inbox_reply_window.ReplyWindowStatus.open
        )
