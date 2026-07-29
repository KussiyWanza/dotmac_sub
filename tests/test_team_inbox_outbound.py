from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from app.api import support as support_api
from app.models.notification import CommunicationIntentRecord, Notification
from app.models.service_team import ServiceTeam, ServiceTeamType
from app.models.subscriber import Subscriber, SubscriberStatus
from app.models.subscription_engine import SettingValueType
from app.models.team_inbox import (
    InboxConversation,
    InboxConversationStatus,
    InboxConversationTeam,
    InboxMessage,
    InboxMessageDirection,
    InboxTeamRole,
)
from app.schemas.settings import DomainSettingUpdate
from app.schemas.team_inbox import InboxConversationReplyRequest
from app.services import email as email_service
from app.services import team_inbox_outbound, team_outbound
from app.services.domain_settings import notification_settings
from app.tasks import notifications as notification_tasks


def _smtp_sender(db_session, key: str, *, from_email: str) -> None:
    email_service.upsert_smtp_sender(
        db_session,
        sender_key=key,
        host=f"smtp.{key}.local",
        port=587,
        username=f"{key}-user",
        password=f"bao://notifications/smtp_sender_{key}#password",
        from_email=from_email,
        from_name=key.title(),
        use_tls=True,
        use_ssl=False,
        is_active=True,
    )


def _activity_sender(db_session, activity: str, sender_key: str) -> None:
    notification_settings.upsert_by_key(
        db_session,
        f"smtp_activity_sender.{activity}",
        DomainSettingUpdate(
            value_type=SettingValueType.string,
            value_text=sender_key,
        ),
    )


def _team(db_session, name: str, team_type: str) -> ServiceTeam:
    team = ServiceTeam(name=name, team_type=team_type)
    db_session.add(team)
    db_session.flush()
    return team


def _conversation(db_session, team: ServiceTeam) -> InboxConversation:
    conversation = InboxConversation(
        channel_type="email",
        subject="Router offline",
        contact_address="customer@example.com",
        primary_service_team_id=team.id,
        status=InboxConversationStatus.open.value,
        first_message_at=datetime(2026, 7, 10, 8, 0, tzinfo=UTC),
        last_message_at=datetime(2026, 7, 10, 8, 0, tzinfo=UTC),
    )
    db_session.add(conversation)
    db_session.flush()
    db_session.add(
        InboxConversationTeam(
            conversation_id=conversation.id,
            service_team_id=team.id,
            role=InboxTeamRole.owner.value,
            is_active=True,
        )
    )
    db_session.flush()
    return conversation


def _whatsapp_conversation(db_session) -> InboxConversation:
    conversation = InboxConversation(
        channel_type="whatsapp",
        subject="WhatsApp support",
        contact_address="whatsapp:0803 555 0114",
        status=InboxConversationStatus.open.value,
        first_message_at=datetime(2026, 7, 10, 8, 0, tzinfo=UTC),
        last_message_at=datetime(2026, 7, 10, 8, 0, tzinfo=UTC),
    )
    db_session.add(conversation)
    db_session.flush()
    return conversation


def test_send_inbox_reply_uses_owner_team_sender(db_session, monkeypatch):
    _smtp_sender(db_session, "support", from_email="support@dotmac.io")
    _activity_sender(db_session, "support_ticket", "support")
    team = _team(db_session, "Support", ServiceTeamType.support.value)
    conversation = _conversation(db_session, team)
    db_session.commit()

    result = team_inbox_outbound.send_inbox_reply(
        db_session,
        conversation=conversation,
        payload=team_inbox_outbound.InboxReplyPayload(
            body_html="<p>We are checking.</p>",
            body_text="We are checking.",
            sent_by_person_id=uuid4(),
        ),
        now=datetime(2026, 7, 10, 8, 5, tzinfo=UTC),
    )
    db_session.commit()

    message = db_session.query(InboxMessage).one()
    notification = db_session.query(Notification).one()
    assert result.kind == "queued"
    assert result.sender_key == "support"
    assert result.activity == "support_ticket"
    assert result.from_address == "support@dotmac.io"
    assert notification.recipient == "customer@example.com"
    assert notification.subject == "Re: Router offline"
    assert notification.metadata_["activity"] == "support_ticket"
    assert message.direction == InboxMessageDirection.outbound.value
    assert message.from_address == "support@dotmac.io"
    assert message.to_addresses == ["customer@example.com"]
    assert message.metadata_["sender_key"] == "support"
    assert message.notification_id == notification.id
    assert message.metadata_["delivery_status"] == "queued"


def test_send_inbox_reply_sends_whatsapp_text(db_session, monkeypatch):
    conversation = _whatsapp_conversation(db_session)
    db_session.commit()

    result = team_inbox_outbound.send_inbox_reply(
        db_session,
        conversation=conversation,
        payload=team_inbox_outbound.InboxReplyPayload(
            body_html="<p>We are checking this.</p>",
            sent_by_person_id=uuid4(),
        ),
        now=datetime(2026, 7, 10, 8, 5, tzinfo=UTC),
    )
    db_session.commit()

    message = db_session.query(InboxMessage).one()
    notification = db_session.query(Notification).one()
    intent = db_session.query(CommunicationIntentRecord).one()
    assert result.kind == "queued"
    assert result.to_email == "+2348035550114"
    assert notification.recipient == "+2348035550114"
    assert notification.body == "We are checking this."
    assert intent.subscriber_id is None
    assert message.channel_type == "whatsapp"
    assert message.direction == InboxMessageDirection.outbound.value
    assert message.body == "We are checking this."
    assert message.external_message_id is None
    assert message.to_addresses == ["+2348035550114"]
    assert message.metadata_["delivery_status"] == "queued"
    assert conversation.last_message_at == datetime(2026, 7, 10, 8, 5)


def test_send_inbox_reply_sends_whatsapp_template(db_session, monkeypatch):
    conversation = _whatsapp_conversation(db_session)
    db_session.commit()

    result = team_inbox_outbound.send_inbox_reply(
        db_session,
        conversation=conversation,
        payload=team_inbox_outbound.InboxReplyPayload(
            body_html="<p>Template fallback.</p>",
            body_text="Template fallback.",
            metadata={
                "whatsapp_template": {
                    "name": "service_update",
                    "language": "en",
                    "variables": {"1": "Ada"},
                }
            },
        ),
        now=datetime(2026, 7, 10, 8, 5, tzinfo=UTC),
    )
    db_session.commit()

    message = db_session.query(InboxMessage).one()
    notification = db_session.query(Notification).one()
    assert result.kind == "queued"
    assert notification.metadata_["whatsapp_template"]["name"] == "service_update"
    assert notification.metadata_["whatsapp_template"]["language"] == "en"
    assert notification.metadata_["whatsapp_template"]["variables"] == {"1": "Ada"}
    assert message.body == "[WhatsApp template: service_update]"
    assert message.metadata_["message_kind"] == "template"
    assert message.external_message_id is None


def test_send_inbox_reply_does_not_call_whatsapp_provider_inline(
    db_session, monkeypatch
):
    conversation = _whatsapp_conversation(db_session)
    calls: list[object] = []
    monkeypatch.setattr(
        notification_tasks.whatsapp_service,
        "send_text_message",
        lambda *args, **kwargs: calls.append(kwargs) or {"ok": True},
    )
    db_session.commit()

    result = team_inbox_outbound.send_inbox_reply(
        db_session,
        conversation=conversation,
        payload=team_inbox_outbound.InboxReplyPayload(
            body_html="<p>We are checking this.</p>",
        ),
    )

    assert result.kind == "queued"
    assert calls == []
    assert db_session.query(InboxMessage).count() == 1


def test_failed_outbox_message_can_be_manually_requeued(db_session, monkeypatch):
    conversation = _whatsapp_conversation(db_session)
    attempts: list[dict[str, object]] = []

    def _fake_send(*args, **kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            return {
                "ok": False,
                "provider": "meta_cloud_api",
                "sent": True,
                "status_code": 400,
                "response": "bad recipient",
            }
        return {
            "ok": True,
            "provider": "meta_cloud_api",
            "sent": True,
            "status_code": 200,
            "response": '{"messages":[{"id":"wamid.retry"}]}',
        }

    monkeypatch.setattr(
        notification_tasks.whatsapp_service, "send_text_message", _fake_send
    )
    db_session.commit()

    queued = team_inbox_outbound.send_inbox_reply(
        db_session,
        conversation=conversation,
        payload=team_inbox_outbound.InboxReplyPayload(
            body_html="<p>We are checking this.</p>",
        ),
        record_failure=True,
    )
    notification_tasks._deliver_notification_queue_stats(db_session)
    failed_message = db_session.get(InboxMessage, queued.message_id)
    retried = team_inbox_outbound.retry_outbound_message(
        db_session,
        message=failed_message,
    )

    assert queued.kind == "queued"
    assert failed_message.metadata_["delivery_status"] == "retried"
    assert failed_message.metadata_["retry_count"] == 1
    assert retried.kind == "queued"
    assert db_session.query(InboxMessage).count() == 2


def test_send_inbox_reply_requires_whatsapp_recipient(db_session):
    conversation = _whatsapp_conversation(db_session)
    conversation.contact_address = None
    db_session.commit()

    result = team_inbox_outbound.send_inbox_reply(
        db_session,
        conversation=conversation,
        payload=team_inbox_outbound.InboxReplyPayload(body_html="<p>Hello.</p>"),
    )

    assert result.kind == "missing_recipient"
    assert result.reason == "Conversation has no WhatsApp reply recipient"


def test_linked_disabled_subscriber_reply_is_suppressed(db_session):
    subscriber = Subscriber(
        first_name="Disabled",
        last_name="Customer",
        email="disabled-inbox@example.com",
        status=SubscriberStatus.disabled,
        is_active=False,
    )
    db_session.add(subscriber)
    db_session.flush()
    conversation = InboxConversation(
        subscriber_id=subscriber.id,
        channel_type="email",
        contact_address=subscriber.email,
        status=InboxConversationStatus.open.value,
    )
    db_session.add(conversation)
    db_session.flush()

    result = team_inbox_outbound.send_inbox_reply(
        db_session,
        conversation=conversation,
        payload=team_inbox_outbound.InboxReplyPayload(body_html="<p>Hello.</p>"),
    )

    assert result.kind == "suppressed"
    assert db_session.query(Notification).count() == 0
    assert db_session.query(InboxMessage).count() == 0


def test_send_inbox_reply_uses_team_metadata_activity_sender(db_session, monkeypatch):
    # Team identity no longer derives delivery behavior. A team needing a
    # distinct sender declares the outbound activity via operator metadata,
    # which overrides the inbox caller's declared support_ticket activity.
    _smtp_sender(db_session, "field", from_email="field@dotmac.io")
    _activity_sender(db_session, "field_service", "field")
    team = _team(db_session, "Field Service", ServiceTeamType.field_service.value)
    team.metadata_ = {
        team_outbound.OUTBOUND_EMAIL_ACTIVITY_METADATA_KEY: "field_service"
    }
    conversation = _conversation(db_session, team)
    db_session.commit()

    result = team_inbox_outbound.send_inbox_reply(
        db_session,
        conversation=conversation,
        payload=team_inbox_outbound.InboxReplyPayload(
            body_html="<p>Technician is on route.</p>",
            to_email="site-contact@example.com",
        ),
    )

    notification = db_session.query(Notification).one()
    assert result.kind == "queued"
    assert result.activity == "field_service"
    assert result.from_address == "field@dotmac.io"
    assert notification.recipient == "site-contact@example.com"
    assert notification.metadata_["activity"] == "field_service"


def test_reply_api_queues_before_provider_delivery(db_session, monkeypatch):
    team = _team(db_session, "Support", ServiceTeamType.support.value)
    conversation = _conversation(db_session, team)
    db_session.commit()

    result = support_api.reply_to_inbox_conversation(
        conversation.id,
        InboxConversationReplyRequest(body_html="<p>No route.</p>"),
        auth={"principal_id": str(uuid4())},
        db=db_session,
    )

    assert result.kind == "queued"
    assert db_session.query(InboxMessage).count() == 1


def test_team_metadata_sender_key_overrides_reply_activity(db_session, monkeypatch):
    _smtp_sender(db_session, "vip_support", from_email="vip@dotmac.io")
    team = ServiceTeam(
        name="VIP Support",
        team_type=ServiceTeamType.support.value,
        metadata_={
            team_outbound.OUTBOUND_EMAIL_SENDER_METADATA_KEY: "vip_support",
        },
    )
    db_session.add(team)
    db_session.flush()
    conversation = _conversation(db_session, team)
    db_session.commit()

    result = team_inbox_outbound.send_inbox_reply(
        db_session,
        conversation=conversation,
        payload=team_inbox_outbound.InboxReplyPayload(body_html="<p>VIP reply.</p>"),
    )

    notification = db_session.query(Notification).one()
    assert result.kind == "queued"
    assert result.sender_key == "vip_support"
    assert result.from_address == "vip@dotmac.io"
    assert notification.metadata_["sender_key"] == "vip_support"


def test_owner_route_sender_metadata_overrides_team_sender(db_session, monkeypatch):
    _smtp_sender(db_session, "team_support", from_email="support@dotmac.io")
    _smtp_sender(db_session, "route_support", from_email="help@dotmac.io")
    team = ServiceTeam(
        name="Support",
        team_type=ServiceTeamType.support.value,
        metadata_={
            team_outbound.OUTBOUND_EMAIL_SENDER_METADATA_KEY: "team_support",
        },
    )
    db_session.add(team)
    db_session.flush()
    conversation = InboxConversation(
        channel_type="email",
        subject="Need help",
        contact_address="customer@example.com",
        primary_service_team_id=team.id,
        status=InboxConversationStatus.open.value,
    )
    db_session.add(conversation)
    db_session.flush()
    db_session.add(
        InboxConversationTeam(
            conversation_id=conversation.id,
            service_team_id=team.id,
            role=InboxTeamRole.owner.value,
            is_active=True,
            metadata_={
                team_outbound.OUTBOUND_EMAIL_SENDER_METADATA_KEY: "route_support",
                team_outbound.OUTBOUND_EMAIL_ACTIVITY_METADATA_KEY: "support_ticket",
                "route_email_address": "help@dotmac.io",
            },
        )
    )
    db_session.commit()

    result = team_inbox_outbound.send_inbox_reply(
        db_session,
        conversation=conversation,
        payload=team_inbox_outbound.InboxReplyPayload(body_html="<p>Reply.</p>"),
    )

    notification = db_session.query(Notification).one()
    assert result.kind == "queued"
    assert result.sender_key == "route_support"
    assert result.activity == "support_ticket"
    assert result.from_address == "help@dotmac.io"
    assert notification.metadata_["sender_key"] == "route_support"
