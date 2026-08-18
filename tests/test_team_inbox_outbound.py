from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.api import support as support_api
from app.models.notification import (
    CommunicationIntentRecord,
    Notification,
    NotificationChannel,
    NotificationStatus,
)
from app.models.service_team import ServiceTeam, ServiceTeamType
from app.models.subscriber import Subscriber, SubscriberStatus
from app.models.subscription_engine import SettingValueType
from app.models.team_inbox import (
    InboxChannelType,
    InboxConversation,
    InboxConversationStatus,
    InboxConversationTeam,
    InboxMessage,
    InboxMessageDirection,
    InboxTeamRole,
)
from app.schemas.ai_intake import GENERIC_FOLLOW_UP_QUESTION
from app.schemas.settings import DomainSettingUpdate
from app.schemas.team_inbox import InboxConversationReplyRequest
from app.services import email as email_service
from app.services import (
    team_inbox_commands,
    team_inbox_media,
    team_inbox_outbound,
    team_outbound,
)
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


def _open_whatsapp_window(
    db_session,
    conversation: InboxConversation,
    *,
    at: datetime | None = None,
) -> InboxMessage:
    inbound = InboxMessage(
        conversation_id=conversation.id,
        channel_type=InboxChannelType.whatsapp.value,
        direction=InboxMessageDirection.inbound.value,
        body="Hello",
        received_at=at or datetime.now(UTC),
    )
    db_session.add(inbound)
    db_session.flush()
    return inbound


def test_send_inbox_reply_uses_owner_team_sender(db_session, monkeypatch):
    delivery_wakeups: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        notification_tasks.deliver_notification,
        "apply_async",
        lambda *args, **kwargs: delivery_wakeups.append((args, kwargs)),
    )
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
    assert result.notification_id == notification.id
    assert notification.recipient == "customer@example.com"
    assert notification.subject == "Re: Router offline"
    assert notification.metadata_["activity"] == "support_ticket"
    assert message.direction == InboxMessageDirection.outbound.value
    assert message.from_address == "support@dotmac.io"
    assert message.to_addresses == ["customer@example.com"]
    assert message.metadata_["sender_key"] == "support"
    assert message.notification_id == notification.id
    assert message.metadata_["delivery_status"] == "queued"
    assert message.body == "We are checking."
    assert message.metadata_["body_html"] == "<p>We are checking.</p>"
    assert message.metadata_["body_text"] == "We are checking."
    assert delivery_wakeups == [((), {"args": [str(notification.id)], "retry": False})]


def test_send_inbox_reply_sends_whatsapp_text(db_session, monkeypatch):
    conversation = _whatsapp_conversation(db_session)
    _open_whatsapp_window(db_session, conversation)
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

    message = (
        db_session.query(InboxMessage)
        .filter(InboxMessage.direction == InboxMessageDirection.outbound.value)
        .one()
    )
    notification = db_session.query(Notification).one()
    intent = db_session.query(CommunicationIntentRecord).one()
    assert result.kind == "queued"
    assert result.to_email == "+2348035550114"
    assert notification.recipient == "+2348035550114"
    assert notification.body == "We are checking this."
    assert notification.metadata_["delivery_latency"] == "immediate"
    assert notification.metadata_["delivery_timing_source"] == "immediate"
    assert notification.send_at is None
    assert intent.subscriber_id is None
    assert intent.metadata_["delivery_latency"] == "immediate"
    assert message.channel_type == "whatsapp"
    assert message.direction == InboxMessageDirection.outbound.value
    assert message.body == "We are checking this."
    assert message.external_message_id is None
    assert message.to_addresses == ["+2348035550114"]
    assert message.metadata_["delivery_status"] == "queued"
    assert conversation.last_message_at == datetime(2026, 7, 10, 8, 5)


def test_email_notification_delivers_inbox_attachment(db_session, monkeypatch):
    _smtp_sender(db_session, "support", from_email="support@dotmac.io")
    _activity_sender(db_session, "support_ticket", "support")
    team = _team(db_session, "Support", ServiceTeamType.support.value)
    conversation = _conversation(db_session, team)
    attachment_id = uuid4()
    calls: list[dict] = []
    monkeypatch.setattr(
        notification_tasks.team_inbox_media,
        "resolve_delivery_attachments",
        lambda *_args, **_kwargs: (
            team_inbox_media.InboxDeliveryAttachment(
                asset_id=attachment_id,
                filename="router.pdf",
                content_type="application/pdf",
                content=b"pdf-bytes",
                asset_type="document",
            ),
        ),
    )
    monkeypatch.setattr(
        notification_tasks.email_service,
        "send_email",
        lambda *args, **kwargs: calls.append(kwargs) or True,
    )
    db_session.commit()

    result = team_inbox_outbound.send_inbox_reply(
        db_session,
        conversation=conversation,
        payload=team_inbox_outbound.InboxReplyPayload(
            body_html="<p>Document attached.</p>",
            body_text="Document attached.",
            metadata={"inbox_attachment_ids": [str(attachment_id)]},
        ),
    )
    notification_tasks._deliver_notification_queue_stats(db_session)

    assert result.kind == "queued"
    assert len(calls) == 1
    assert calls[0]["body_html"] == "<p>Document attached.</p>"
    assert calls[0]["body_text"] == "Document attached."
    assert len(calls[0]["attachments"]) == 1
    assert calls[0]["attachments"][0].filename == "router.pdf"
    assert calls[0]["attachments"][0].content == b"pdf-bytes"


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


def test_worker_delivers_metadata_template_instead_of_placeholder_text(
    db_session, monkeypatch
):
    conversation = _whatsapp_conversation(db_session)
    db_session.commit()
    queued = team_inbox_outbound.send_inbox_reply(
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
                    "components": [],
                }
            },
        ),
        now=datetime(2026, 7, 10, 8, 5, tzinfo=UTC),
    )
    template_calls: list[dict[str, object]] = []
    text_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        notification_tasks.whatsapp_service,
        "send_template_message",
        lambda *args, **kwargs: template_calls.append(kwargs) or {"ok": True},
    )
    monkeypatch.setattr(
        notification_tasks.whatsapp_service,
        "send_text_message",
        lambda *args, **kwargs: text_calls.append(kwargs) or {"ok": True},
    )

    notification_tasks._deliver_notification_queue_stats(db_session)

    notification = db_session.get(Notification, queued.notification_id)
    assert text_calls == []
    assert len(template_calls) == 1
    assert template_calls[0]["template_name"] == "service_update"
    assert template_calls[0]["language"] == "en"
    assert template_calls[0]["variables"] == {"1": "Ada"}
    assert template_calls[0]["components"] == []
    assert notification.status == NotificationStatus.delivered


def test_whatsapp_free_form_reply_requires_open_customer_window(db_session):
    conversation = _whatsapp_conversation(db_session)
    db_session.add(
        InboxMessage(
            conversation_id=conversation.id,
            channel_type=InboxChannelType.whatsapp.value,
            direction=InboxMessageDirection.inbound.value,
            body="Hello",
            received_at=datetime(2026, 7, 10, 8, 0, tzinfo=UTC),
        )
    )
    db_session.flush()

    result = team_inbox_outbound.send_inbox_reply(
        db_session,
        conversation=conversation,
        payload=team_inbox_outbound.InboxReplyPayload(
            body_html="<p>We are checking.</p>",
            body_text="We are checking.",
        ),
        now=datetime(2026, 7, 11, 8, 0, tzinfo=UTC),
    )

    assert result.kind == "reply_window_expired"
    assert db_session.query(Notification).count() == 0


def test_whatsapp_free_form_reply_fails_closed_when_window_is_unavailable(db_session):
    conversation = _whatsapp_conversation(db_session)
    db_session.commit()

    result = team_inbox_outbound.send_inbox_reply(
        db_session,
        conversation=conversation,
        payload=team_inbox_outbound.InboxReplyPayload(
            body_html="<p>Can we send?</p>",
            body_text="Can we send?",
        ),
        now=datetime(2026, 7, 10, 8, 0, tzinfo=UTC),
    )

    assert result.kind == "reply_window_expired"
    assert result.reason == (
        "Reply availability could not be confirmed. Free-form messaging is "
        "disabled to prevent a provider rejection."
    )
    assert db_session.query(Notification).count() == 0


def test_nonqualifying_inbound_message_does_not_reopen_whatsapp_window(db_session):
    conversation = _whatsapp_conversation(db_session)
    opened_at = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    _open_whatsapp_window(db_session, conversation, at=opened_at)
    db_session.add(
        InboxMessage(
            conversation_id=conversation.id,
            channel_type=InboxChannelType.whatsapp.value,
            direction=InboxMessageDirection.inbound.value,
            body="Read receipt placeholder",
            received_at=opened_at + timedelta(hours=25),
            metadata_={"reply_window_qualifying": False},
        )
    )
    db_session.flush()

    result = team_inbox_outbound.send_inbox_reply(
        db_session,
        conversation=conversation,
        payload=team_inbox_outbound.InboxReplyPayload(
            body_html="<p>Receipt should not reopen.</p>",
            body_text="Receipt should not reopen.",
        ),
        now=opened_at + timedelta(hours=25, minutes=5),
    )

    assert result.kind == "reply_window_expired"
    assert db_session.query(Notification).count() == 0


def test_staff_and_internal_messages_do_not_extend_meta_reply_window(db_session):
    conversation = _whatsapp_conversation(db_session)
    opened_at = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    db_session.add_all(
        [
            InboxMessage(
                conversation_id=conversation.id,
                channel_type=InboxChannelType.whatsapp.value,
                direction=InboxMessageDirection.inbound.value,
                body="Hello",
                received_at=opened_at,
            ),
            InboxMessage(
                conversation_id=conversation.id,
                channel_type=InboxChannelType.whatsapp.value,
                direction=InboxMessageDirection.outbound.value,
                body="Queued later",
                sent_at=opened_at + timedelta(hours=23),
            ),
            InboxMessage(
                conversation_id=conversation.id,
                channel_type=InboxChannelType.whatsapp.value,
                direction=InboxMessageDirection.internal.value,
                body="Internal note",
                created_at=opened_at + timedelta(hours=23, minutes=30),
            ),
        ]
    )
    db_session.flush()

    result = team_inbox_outbound.send_inbox_reply(
        db_session,
        conversation=conversation,
        payload=team_inbox_outbound.InboxReplyPayload(
            body_html="<p>Still checking.</p>",
            body_text="Still checking.",
        ),
        now=opened_at + timedelta(hours=24, seconds=1),
    )

    assert result.kind == "reply_window_expired"


def test_new_qualifying_inbound_reopens_whatsapp_window(db_session):
    conversation = _whatsapp_conversation(db_session)
    opened_at = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    _open_whatsapp_window(db_session, conversation, at=opened_at)
    _open_whatsapp_window(
        db_session,
        conversation,
        at=opened_at + timedelta(hours=25),
    )

    result = team_inbox_outbound.send_inbox_reply(
        db_session,
        conversation=conversation,
        payload=team_inbox_outbound.InboxReplyPayload(
            body_html="<p>Window reopened.</p>",
            body_text="Window reopened.",
        ),
        now=opened_at + timedelta(hours=25, minutes=5),
    )

    assert result.kind == "queued"
    assert db_session.query(Notification).count() == 1


def test_whatsapp_template_send_does_not_reopen_free_form_window(
    db_session,
):
    conversation = _whatsapp_conversation(db_session)
    opened_at = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    _open_whatsapp_window(db_session, conversation, at=opened_at)

    template_result = team_inbox_outbound.send_inbox_reply(
        db_session,
        conversation=conversation,
        payload=team_inbox_outbound.InboxReplyPayload(
            body_html="<p>Template fallback.</p>",
            body_text="Template fallback.",
            metadata={
                "whatsapp_template": {
                    "name": "service_update",
                    "language": "en",
                    "components": [],
                }
            },
        ),
        now=opened_at + timedelta(hours=25),
    )
    free_form_result = team_inbox_outbound.send_inbox_reply(
        db_session,
        conversation=conversation,
        payload=team_inbox_outbound.InboxReplyPayload(
            body_html="<p>Can I send now?</p>",
            body_text="Can I send now?",
        ),
        now=opened_at + timedelta(hours=25, minutes=1),
    )

    assert template_result.kind == "queued"
    assert free_form_result.kind == "reply_window_expired"


def test_whatsapp_template_retry_preserves_template_payload(db_session):
    conversation = _whatsapp_conversation(db_session)
    failed = InboxMessage(
        conversation_id=conversation.id,
        channel_type=InboxChannelType.whatsapp.value,
        direction=InboxMessageDirection.outbound.value,
        body="[WhatsApp template: service_update]",
        sent_at=datetime(2026, 7, 11, 9, 0, tzinfo=UTC),
        metadata_={
            "delivery_status": "failed",
            "retry_count": 0,
            "whatsapp_template": {
                "name": "service_update",
                "language": "en",
                "components": [],
            },
        },
    )
    db_session.add(failed)
    db_session.flush()

    result = team_inbox_outbound.retry_outbound_message(db_session, message=failed)

    notification = db_session.query(Notification).one()
    assert result.kind == "queued"
    assert notification.metadata_["message_kind"] == "template"
    assert notification.metadata_["whatsapp_template"]["name"] == "service_update"
    assert failed.metadata_["delivery_status"] == "retried"


def test_worker_preflight_blocks_expired_whatsapp_free_form_before_provider(
    db_session, monkeypatch
):
    conversation = _whatsapp_conversation(db_session)
    _open_whatsapp_window(
        db_session,
        conversation,
        at=datetime(2026, 7, 10, 8, 0, tzinfo=UTC),
    )
    queued = team_inbox_outbound.send_inbox_reply(
        db_session,
        conversation=conversation,
        payload=team_inbox_outbound.InboxReplyPayload(
            body_html="<p>Queued before expiry.</p>",
            body_text="Queued before expiry.",
        ),
        now=datetime(2026, 7, 10, 8, 5, tzinfo=UTC),
    )
    calls: list[object] = []
    monkeypatch.setattr(
        notification_tasks.whatsapp_service,
        "send_text_message",
        lambda *args, **kwargs: calls.append(kwargs) or {"ok": True},
    )

    notification_tasks._deliver_notification_queue_stats(db_session)

    notification = db_session.get(
        Notification, db_session.get(InboxMessage, queued.message_id).notification_id
    )
    message = db_session.get(InboxMessage, queued.message_id)
    assert calls == []
    assert notification.status == NotificationStatus.failed
    assert notification.last_error == "reply_window_expired"
    assert message.metadata_["delivery_status"] == "failed"
    assert message.metadata_["send_error"] == "reply_window_expired"


def test_direct_whatsapp_template_reply_uses_approved_template_validation(
    db_session,
    monkeypatch,
):
    conversation = _whatsapp_conversation(db_session)

    from app.services.integrations import whatsapp_capability

    monkeypatch.setattr(
        whatsapp_capability,
        "list_approved_templates",
        lambda _db: (
            {
                "name": "service_update",
                "language": "en",
                "status": "APPROVED",
                "components": [],
            },
        ),
    )

    outcome = team_inbox_commands.reply(
        db_session,
        command=team_inbox_commands.ReplyCommand(
            conversation_id=conversation.id,
            body_text="",
            actor_person_id=uuid4(),
            whatsapp_template_name="service_update",
            whatsapp_template_language="en",
            whatsapp_template_components=(),
        ),
    )

    notification = db_session.query(Notification).one()
    assert outcome.kind == "queued"
    assert notification.metadata_["message_kind"] == "template"
    assert notification.metadata_["whatsapp_template"]["name"] == "service_update"


def test_send_inbox_reply_does_not_call_whatsapp_provider_inline(
    db_session, monkeypatch
):
    conversation = _whatsapp_conversation(db_session)
    _open_whatsapp_window(db_session, conversation)
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
    assert (
        db_session.query(InboxMessage)
        .filter(InboxMessage.direction == InboxMessageDirection.outbound.value)
        .count()
        == 1
    )


def test_whatsapp_notification_delivers_inbox_attachment_as_media(
    db_session, monkeypatch
):
    conversation = _whatsapp_conversation(db_session)
    _open_whatsapp_window(db_session, conversation)
    attachment_id = uuid4()
    media_calls: list[dict] = []
    text_calls: list[dict] = []

    monkeypatch.setattr(
        notification_tasks.whatsapp_service,
        "send_text_message",
        lambda *args, **kwargs: (
            text_calls.append(kwargs)
            or {"ok": True, "provider_message_id": "wamid.text"}
        ),
    )
    monkeypatch.setattr(
        notification_tasks.team_inbox_media,
        "resolve_delivery_attachments",
        lambda *_args, **_kwargs: (
            team_inbox_media.InboxDeliveryAttachment(
                asset_id=attachment_id,
                filename="drop.jpg",
                content_type="image/jpeg",
                content=b"jpeg-bytes",
                asset_type="image",
            ),
        ),
    )

    def fake_send_media_message(*args, **kwargs):
        media_calls.append(kwargs)
        return {
            "ok": True,
            "provider": "meta_cloud_api",
            "provider_message_id": "wamid.media",
            "status_code": 200,
        }

    monkeypatch.setattr(
        notification_tasks.whatsapp_service,
        "send_media_message",
        fake_send_media_message,
    )
    db_session.commit()

    result = team_inbox_outbound.send_inbox_reply(
        db_session,
        conversation=conversation,
        payload=team_inbox_outbound.InboxReplyPayload(
            body_html="<p>Photo attached.</p>",
            body_text="Photo attached.",
            metadata={"inbox_attachment_ids": [str(attachment_id)]},
        ),
    )
    notification_tasks._deliver_notification_queue_stats(db_session)

    message = db_session.get(InboxMessage, result.message_id)
    assert text_calls == []
    assert len(media_calls) == 1
    assert media_calls[0]["media_type"] == "image"
    assert media_calls[0]["content"] == b"jpeg-bytes"
    assert media_calls[0]["caption"] == "Photo attached."
    assert message is not None
    assert message.external_message_id == "wamid.media"
    assert message.metadata_["provider_message_ids"] == ["wamid.media"]


def _social_comment_conversation(
    db_session, *, channel: str, account_key: str, account_id: str
) -> InboxConversation:
    conversation = InboxConversation(
        channel_type=channel,
        subject="Social comment",
        status=InboxConversationStatus.open.value,
        metadata_={account_key: account_id, "permalink": "https://example.com/post"},
    )
    db_session.add(conversation)
    db_session.flush()
    db_session.add(
        InboxMessage(
            conversation_id=conversation.id,
            channel_type=channel,
            direction=InboxMessageDirection.inbound.value,
            body="Is this available?",
            external_message_id="comment-123",
            from_address="Ada",
            received_at=datetime(2026, 7, 10, 8, 0, tzinfo=UTC),
        )
    )
    db_session.flush()
    return conversation


def test_facebook_comment_reply_records_provider_id_only_after_meta_accepts(
    db_session, monkeypatch
):
    from app.services import meta_pages, team_inbox_realtime

    conversation = _social_comment_conversation(
        db_session,
        channel="facebook_comment",
        account_key="page_id",
        account_id="page-123",
    )
    calls: list[dict[str, str]] = []
    realtime_events: list[tuple[object, dict[str, object]]] = []
    monkeypatch.setattr(
        team_inbox_realtime,
        "publish_conversation_event",
        lambda _db, _conversation_id, *, event_type, payload: realtime_events.append(
            (event_type, payload)
        ),
    )
    monkeypatch.setattr(
        meta_pages,
        "reply_to_comment_sync",
        lambda _db, **kwargs: calls.append(kwargs) or {"id": "reply-456"},
    )

    result = team_inbox_outbound.send_inbox_reply(
        db_session,
        conversation=conversation,
        payload=team_inbox_outbound.InboxReplyPayload(
            body_html="<p>Yes, it is.</p>",
            body_text="Yes, it is.",
            sent_by_person_id=uuid4(),
        ),
    )
    assert result.kind == "queued"
    assert calls == []

    notification_tasks._deliver_notification_queue_stats(db_session)

    outbound = (
        db_session.query(InboxMessage)
        .filter(InboxMessage.direction == InboxMessageDirection.outbound.value)
        .one()
    )
    assert calls == [
        {
            "page_id": "page-123",
            "comment_id": "comment-123",
            "message": "Yes, it is.",
        }
    ]
    assert outbound.external_message_id == "reply-456"
    assert outbound.metadata_["delivery_status"] == "delivered"
    assert outbound.metadata_["parent_provider_comment_id"] == "comment-123"
    assert [
        payload["delivery_status"]
        for event_type, payload in realtime_events
        if event_type == team_inbox_realtime.EventType.MESSAGE_STATUS_CHANGED
    ] == ["sending", "delivered"]


def test_social_comment_reply_targets_quoted_comment_not_latest_inbound(
    db_session, monkeypatch
):
    from app.services import meta_pages

    conversation = _social_comment_conversation(
        db_session,
        channel="facebook_comment",
        account_key="page_id",
        account_id="page-123",
    )
    first = (
        db_session.query(InboxMessage)
        .filter(InboxMessage.direction == InboxMessageDirection.inbound.value)
        .one()
    )
    db_session.add(
        InboxMessage(
            conversation_id=conversation.id,
            channel_type=conversation.channel_type,
            direction=InboxMessageDirection.inbound.value,
            body="Second comment",
            external_message_id="comment-999",
            from_address="Bayo",
            received_at=datetime(2026, 7, 10, 8, 5, tzinfo=UTC),
        )
    )
    db_session.flush()
    calls: list[dict[str, str]] = []
    monkeypatch.setattr(
        meta_pages,
        "reply_to_comment_sync",
        lambda _db, **kwargs: calls.append(kwargs) or {"id": "reply-456"},
    )

    result = team_inbox_outbound.send_inbox_reply(
        db_session,
        conversation=conversation,
        payload=team_inbox_outbound.InboxReplyPayload(
            body_html="<p>Replying to the first.</p>",
            body_text="Replying to the first.",
            sent_by_person_id=uuid4(),
            metadata={"reply_to": {"message_id": str(first.id)}},
        ),
    )
    notification_tasks._deliver_notification_queue_stats(db_session)

    outbound = (
        db_session.query(InboxMessage)
        .filter(InboxMessage.direction == InboxMessageDirection.outbound.value)
        .one()
    )
    assert result.kind == "queued"
    assert calls[0]["comment_id"] == "comment-123"
    assert outbound.metadata_["parent_provider_comment_id"] == "comment-123"


def test_facebook_targeted_comment_reply_dispatches_exact_page_and_comment(
    db_session, monkeypatch
):
    from app.services import meta_pages

    conversation = _social_comment_conversation(
        db_session,
        channel="facebook_comment",
        account_key="page_id",
        account_id="page-123",
    )
    target = (
        db_session.query(InboxMessage)
        .filter(InboxMessage.direction == InboxMessageDirection.inbound.value)
        .one()
    )
    target.metadata_ = {
        "page_id": "page-123",
        "post_id": "page-123_987",
        "provider_comment_id": "comment-123",
    }
    db_session.add(
        InboxMessage(
            conversation_id=conversation.id,
            channel_type=conversation.channel_type,
            direction=InboxMessageDirection.inbound.value,
            body="Do not reply here",
            external_message_id="comment-latest",
            metadata_={
                "page_id": "page-123",
                "post_id": "page-123_987",
                "provider_comment_id": "comment-latest",
            },
            received_at=datetime(2026, 7, 10, 8, 5, tzinfo=UTC),
        )
    )
    db_session.flush()
    calls: list[dict[str, str]] = []
    monkeypatch.setattr(
        meta_pages,
        "reply_to_comment_sync",
        lambda _db, **kwargs: calls.append(kwargs) or {"id": "fb-reply-1"},
    )

    result = team_inbox_commands.reply(
        db_session,
        command=team_inbox_commands.ReplyCommand(
            conversation_id=conversation.id,
            body_text="Replying publicly.",
            actor_person_id=uuid4(),
            reply_to_message_id=target.id,
            idempotency_key="facebook-public-comment-reply",
        ),
    )
    notification_tasks._deliver_notification_queue_stats(db_session)

    outbound = (
        db_session.query(InboxMessage)
        .filter(InboxMessage.direction == InboxMessageDirection.outbound.value)
        .one()
    )
    assert result.kind == "queued"
    assert calls == [
        {
            "page_id": "page-123",
            "comment_id": "comment-123",
            "message": "Replying publicly.",
        }
    ]
    assert outbound.external_message_id == "fb-reply-1"
    assert outbound.metadata_["target_inbox_message_id"] == str(target.id)
    assert outbound.metadata_["provider_post_id"] == "page-123_987"


def test_instagram_targeted_comment_reply_dispatches_exact_account_and_comment(
    db_session, monkeypatch
):
    from app.services import meta_pages

    conversation = _social_comment_conversation(
        db_session,
        channel="instagram_comment",
        account_key="instagram_account_id",
        account_id="ig-123",
    )
    target = (
        db_session.query(InboxMessage)
        .filter(InboxMessage.direction == InboxMessageDirection.inbound.value)
        .one()
    )
    target.external_message_id = "ig-comment-123"
    target.metadata_ = {
        "instagram_account_id": "ig-123",
        "media_id": "ig-media-987",
        "provider_comment_id": "ig-comment-123",
        "parent_provider_comment_id": "ig-root-1",
    }
    db_session.add(
        InboxMessage(
            conversation_id=conversation.id,
            channel_type=conversation.channel_type,
            direction=InboxMessageDirection.inbound.value,
            body="Wrong target",
            external_message_id="ig-comment-latest",
            metadata_={
                "instagram_account_id": "ig-123",
                "media_id": "ig-media-987",
                "provider_comment_id": "ig-comment-latest",
            },
            received_at=datetime(2026, 7, 10, 8, 5, tzinfo=UTC),
        )
    )
    db_session.flush()
    calls: list[dict[str, str]] = []
    monkeypatch.setattr(
        meta_pages,
        "reply_to_instagram_comment_sync",
        lambda _db, **kwargs: calls.append(kwargs) or {"id": "ig-reply-1"},
    )

    result = team_inbox_commands.reply(
        db_session,
        command=team_inbox_commands.ReplyCommand(
            conversation_id=conversation.id,
            body_text="Instagram public reply.",
            actor_person_id=uuid4(),
            reply_to_message_id=target.id,
            idempotency_key="instagram-public-comment-reply",
        ),
    )
    notification_tasks._deliver_notification_queue_stats(db_session)

    outbound = (
        db_session.query(InboxMessage)
        .filter(InboxMessage.direction == InboxMessageDirection.outbound.value)
        .one()
    )
    assert result.kind == "queued"
    assert calls == [
        {
            "ig_account_id": "ig-123",
            "comment_id": "ig-comment-123",
            "message": "Instagram public reply.",
        }
    ]
    assert outbound.external_message_id == "ig-reply-1"
    assert outbound.metadata_["target_inbox_message_id"] == str(target.id)
    assert outbound.metadata_["provider_media_id"] == "ig-media-987"
    assert outbound.metadata_["root_provider_comment_id"] == "ig-root-1"


def test_targeted_social_reply_cannot_fall_back_from_outbound_message(
    db_session,
):
    conversation = _social_comment_conversation(
        db_session,
        channel="facebook_comment",
        account_key="page_id",
        account_id="page-123",
    )
    outbound_target = InboxMessage(
        conversation_id=conversation.id,
        channel_type=conversation.channel_type,
        direction=InboxMessageDirection.outbound.value,
        body="Previous public reply",
        external_message_id="reply-previous",
        sent_at=datetime(2026, 7, 10, 8, 5, tzinfo=UTC),
    )
    db_session.add(outbound_target)
    db_session.flush()

    result = team_inbox_outbound.send_inbox_reply(
        db_session,
        conversation=conversation,
        payload=team_inbox_outbound.InboxReplyPayload(
            body_html="<p>Must not fall through.</p>",
            body_text="Must not fall through.",
            sent_by_person_id=uuid4(),
            metadata={"reply_to": {"message_id": str(outbound_target.id)}},
        ),
    )

    assert result.kind == "invalid_reply_target"
    assert db_session.query(Notification).count() == 0
    assert (
        db_session.query(InboxMessage)
        .filter(InboxMessage.direction == InboxMessageDirection.outbound.value)
        .count()
        == 1
    )


def test_worker_rejects_social_reply_when_target_account_context_does_not_match(
    db_session, monkeypatch
):
    from app.services import meta_pages

    conversation = _social_comment_conversation(
        db_session,
        channel="facebook_comment",
        account_key="page_id",
        account_id="page-123",
    )
    target = (
        db_session.query(InboxMessage)
        .filter(InboxMessage.direction == InboxMessageDirection.inbound.value)
        .one()
    )
    target.metadata_ = {
        "page_id": "page-123",
        "post_id": "page-123_987",
        "provider_comment_id": "comment-123",
    }
    result = team_inbox_outbound.send_inbox_reply(
        db_session,
        conversation=conversation,
        payload=team_inbox_outbound.InboxReplyPayload(
            body_html="<p>Account mismatch.</p>",
            body_text="Account mismatch.",
            sent_by_person_id=uuid4(),
            metadata={"reply_to": {"message_id": str(target.id)}},
        ),
    )
    notification = db_session.query(Notification).one()
    notification.metadata_["provider_account_id"] = "page-999"
    calls: list[dict[str, str]] = []
    monkeypatch.setattr(
        meta_pages,
        "reply_to_comment_sync",
        lambda _db, **kwargs: calls.append(kwargs) or {"id": "unexpected"},
    )

    notification_tasks._deliver_notification_queue_stats(db_session)

    outbound = db_session.get(InboxMessage, result.message_id)
    assert calls == []
    assert notification.status == NotificationStatus.failed
    assert notification.last_error == "meta_comment_target_account_mismatch"
    assert outbound.metadata_["delivery_status"] == "failed"
    assert outbound.metadata_["send_error"] == "meta_comment_target_account_mismatch"


def test_social_comment_provider_failure_does_not_create_a_false_reply(
    db_session, monkeypatch
):
    from app.services import meta_pages

    conversation = _social_comment_conversation(
        db_session,
        channel="instagram_comment",
        account_key="instagram_account_id",
        account_id="ig-123",
    )

    def _fail(*_args, **_kwargs):
        raise RuntimeError("provider token must not reach the browser")

    monkeypatch.setattr(meta_pages, "reply_to_instagram_comment_sync", _fail)
    result = team_inbox_outbound.send_inbox_reply(
        db_session,
        conversation=conversation,
        payload=team_inbox_outbound.InboxReplyPayload(
            body_html="<p>Please send us a DM.</p>",
            body_text="Please send us a DM.",
        ),
    )
    notification_tasks._deliver_notification_queue_stats(db_session)
    outbound = (
        db_session.query(InboxMessage)
        .filter(InboxMessage.direction == InboxMessageDirection.outbound.value)
        .one()
    )

    assert result.kind == "queued"
    assert outbound.external_message_id is None
    assert outbound.metadata_["delivery_status"] == "failed"
    assert outbound.metadata_["send_error"] == "meta_comment_provider_failed"


def test_meta_direct_intake_followups_use_normal_outbound_dispatcher(
    db_session, monkeypatch
):
    from app.services.integrations import meta_social_capability
    from app.services.integrations.meta_social_contracts import (
        MetaDirectMessageOutcome,
    )

    calls = []

    def send_direct_message(_db, command):
        calls.append(command)
        return MetaDirectMessageOutcome(
            accepted=True,
            operation_status="succeeded",
            provider_message_id=f"mid-{command.channel.value}",
        )

    monkeypatch.setattr(
        meta_social_capability,
        "send_direct_message",
        send_direct_message,
    )

    expected_channels = (
        (
            "facebook_messenger",
            NotificationChannel.facebook_messenger,
            "page-123",
            "customer-fb",
        ),
        (
            "instagram_dm",
            NotificationChannel.instagram_dm,
            "ig-123",
            "customer-ig",
        ),
    )
    for channel_type, _notification_channel, account_id, recipient in expected_channels:
        conversation = InboxConversation(
            channel_type=channel_type,
            contact_address=recipient,
            external_thread_id=f"{channel_type}:{recipient}",
            status=InboxConversationStatus.open.value,
        )
        db_session.add(conversation)
        db_session.flush()
        inbound = InboxMessage(
            conversation_id=conversation.id,
            channel_type=channel_type,
            direction=InboxMessageDirection.inbound.value,
            body="Please help",
            external_message_id=f"inbound-{recipient}",
            metadata_={"provider_account_scope": account_id},
        )
        db_session.add(inbound)
        db_session.flush()

        queued = team_inbox_outbound.send_ai_intake_follow_up(
            db_session,
            conversation=conversation,
            payload=team_inbox_outbound.AiIntakeFollowUpPayload(
                question=GENERIC_FOLLOW_UP_QUESTION,
                inbound_message_id=inbound.id,
                config_id=uuid4(),
                follow_up_count=1,
            ),
        )
        assert queued.kind == "queued"
        outbound_message = db_session.get(InboxMessage, queued.message_id)
        assert outbound_message is not None
        notification = db_session.get(Notification, outbound_message.notification_id)
        assert notification is not None
        assert notification.channel == _notification_channel

    notification_tasks._deliver_notification_queue_stats(db_session)

    assert [command.channel.value for command in calls] == [
        "facebook_messenger",
        "instagram_dm",
    ]
    assert [command.provider_account_id for command in calls] == [
        "page-123",
        "ig-123",
    ]
    assert [command.recipient_id for command in calls] == [
        "customer-fb",
        "customer-ig",
    ]
    outbound = (
        db_session.query(InboxMessage)
        .filter(InboxMessage.direction == InboxMessageDirection.outbound.value)
        .order_by(InboxMessage.created_at.asc())
        .all()
    )
    assert [message.external_message_id for message in outbound] == [
        "mid-facebook_messenger",
        "mid-instagram_dm",
    ]
    assert all(
        message.metadata_["delivery_status"] == "delivered" for message in outbound
    )


def test_instagram_comment_limit_is_checked_before_meta(db_session, monkeypatch):
    from app.services import meta_pages

    conversation = _social_comment_conversation(
        db_session,
        channel="instagram_comment",
        account_key="instagram_account_id",
        account_id="ig-123",
    )
    calls: list[object] = []
    monkeypatch.setattr(
        meta_pages,
        "reply_to_instagram_comment_sync",
        lambda *_args, **_kwargs: calls.append(object()) or {"id": "unexpected"},
    )
    result = team_inbox_outbound.send_inbox_reply(
        db_session,
        conversation=conversation,
        payload=team_inbox_outbound.InboxReplyPayload(
            body_html="<p>too long</p>",
            body_text="x" * 2_201,
        ),
    )

    assert result.kind == "invalid_body"
    assert calls == []


def test_failed_outbox_message_can_be_manually_requeued(db_session, monkeypatch):
    conversation = _whatsapp_conversation(db_session)
    _open_whatsapp_window(db_session, conversation)
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
    assert (
        db_session.query(InboxMessage)
        .filter(InboxMessage.direction == InboxMessageDirection.outbound.value)
        .count()
        == 2
    )


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


def test_fiber_website_reply_is_explicitly_unsupported(db_session):
    conversation = InboxConversation(
        channel_type=InboxChannelType.website_fiber.value,
        contact_address="prospect@example.com",
        status=InboxConversationStatus.open.value,
    )
    db_session.add(conversation)
    db_session.commit()

    result = team_inbox_outbound.send_inbox_reply(
        db_session,
        conversation=conversation,
        payload=team_inbox_outbound.InboxReplyPayload(body_html="<p>Hello.</p>"),
    )

    assert result.kind == "unsupported_channel"
    assert db_session.query(Notification).count() == 0
    assert db_session.query(InboxMessage).count() == 0


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
