from __future__ import annotations

import html
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, TypeVar
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.models.notification import (
    DeliveryStatus,
    Notification,
    NotificationChannel,
    NotificationDelivery,
    NotificationStatus,
)
from app.models.team_inbox import (
    InboxChannelType,
    InboxConversation,
    InboxConversationStatus,
    InboxConversationTeam,
    InboxMessage,
    InboxMessageDirection,
    InboxTeamRole,
)
from app.schemas.ai_intake import APPROVED_FOLLOW_UP_QUESTIONS
from app.schemas.notification import NotificationDeliveryLatency
from app.services import (
    ai_conversation_ownership,
    inbox_sla,
    team_inbox_realtime,
    team_inbox_reply_window,
    team_inbox_routing,
    team_outbound,
)
from app.services.communication_intents import (
    CommunicationClass,
    CommunicationIntent,
    submit,
)
from app.services.customer_identity_normalization import normalize_phone_identifier
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

if TYPE_CHECKING:
    from app.services.team_inbox_receive import EmailThreadHeaders

_HTML_TAG_RE = re.compile(r"<[^>]+>")
T = TypeVar("T")
OWNER = "communications.team_inbox_outbound_intents"
_OUTBOUND_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="transactional outbound communication intent",
    name="execute_team_inbox_outbound_intent",
)
_META_DELIVERY_LEG_RECORD_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="outbound Inbox message attempt projection",
    name="record_meta_delivery_leg_acceptance",
)


def _commit(db: Session, action: Callable[[], T]) -> T:
    return execute_owner_command(
        db,
        definition=_OUTBOUND_COMMAND,
        context=CommandContext.system(
            actor="system:team-inbox-outbound-adapter",
            scope="team-inbox:outbound-intent",
            reason="create transactional Team Inbox communication intent",
        ),
        operation=action,
    )


@dataclass(frozen=True)
class InboxReplyPayload:
    body_html: str
    body_text: str | None = None
    subject: str | None = None
    to_email: str | None = None
    cc_addresses: tuple[str, ...] = ()
    bcc_addresses: tuple[str, ...] = ()
    sent_by_person_id: str | UUID | None = None
    metadata: Mapping[str, object] | None = None
    dedupe_key: str | None = None
    email_thread_headers: EmailThreadHeaders | None = None


@dataclass(frozen=True)
class AiIntakeFollowUpPayload:
    question: str
    inbound_message_id: UUID
    config_id: UUID | None
    follow_up_count: int
    session_id: UUID | None = None
    policy_id: UUID | None = None
    policy_version_id: UUID | None = None
    display_name: str = "Dotmac Support"


@dataclass(frozen=True)
class InboxReplyResult:
    kind: str
    conversation_id: str
    message_id: str | None = None
    notification_id: UUID | None = None
    service_team_id: str | None = None
    sender_key: str | None = None
    activity: str | None = None
    from_address: str | None = None
    to_email: str | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class MetaDeliveryLegRecordCommand:
    """Accepted Meta provider leg that must survive a later delivery retry."""

    notification_id: UUID
    provider_message_id: str
    response_code: str
    response_body: str
    attachment_asset_id: UUID | None = None
    provider_attachment_id: str | None = None


@dataclass(frozen=True, slots=True)
class MetaDeliveryLegRecordOutcome:
    notification_delivery_id: UUID
    replayed: bool


def record_meta_delivery_leg_acceptance(
    db: Session,
    *,
    command: MetaDeliveryLegRecordCommand,
    context: CommandContext,
) -> MetaDeliveryLegRecordOutcome:
    """Durably record one accepted Meta send under the registered owner."""

    def operation() -> MetaDeliveryLegRecordOutcome:
        provider_message_id = command.provider_message_id.strip()
        response_code = command.response_code.strip()
        response_body = command.response_body.strip()
        if not provider_message_id or not response_code or not response_body:
            from app.services.domain_errors import DomainError

            raise DomainError(
                code=f"{OWNER}.invalid_command",
                message="A Meta delivery record requires complete provider evidence.",
            )
        if (command.attachment_asset_id is None) != (
            command.provider_attachment_id is None
        ):
            from app.services.domain_errors import DomainError

            raise DomainError(
                code=f"{OWNER}.invalid_command",
                message=(
                    "Meta attachment identity and provider attachment evidence "
                    "must be supplied together."
                ),
            )

        notification = (
            db.query(Notification)
            .filter(Notification.id == command.notification_id)
            .with_for_update()
            .one_or_none()
        )
        if notification is None:
            from app.services.domain_errors import DomainError

            raise DomainError(
                code=f"{OWNER}.not_found",
                message="The notification for this Meta delivery leg does not exist.",
                details={"notification_id": str(command.notification_id)},
            )

        existing = (
            db.query(NotificationDelivery)
            .filter(NotificationDelivery.notification_id == command.notification_id)
            .filter(NotificationDelivery.is_active.is_(True))
            .filter(NotificationDelivery.provider == "meta")
            .filter(NotificationDelivery.status == DeliveryStatus.delivered)
            .filter(NotificationDelivery.response_code == response_code)
            .with_for_update()
            .one_or_none()
        )
        if existing is not None:
            if existing.provider_message_id != provider_message_id:
                from app.services.domain_errors import DomainError

                raise DomainError(
                    code=f"{OWNER}.identity_collision",
                    message=(
                        "The Meta delivery leg was already recorded with "
                        "different provider evidence."
                    ),
                    details={
                        "notification_id": str(command.notification_id),
                        "response_code": response_code,
                    },
                )
            return MetaDeliveryLegRecordOutcome(
                notification_delivery_id=existing.id,
                replayed=True,
            )

        if command.attachment_asset_id is not None:
            attachment_ids = dict(
                (notification.metadata_ or {}).get("meta_provider_attachment_ids") or {}
            )
            attachment_ids[str(command.attachment_asset_id)] = str(
                command.provider_attachment_id
            )
            metadata = dict(notification.metadata_ or {})
            metadata["meta_provider_attachment_ids"] = attachment_ids
            notification.metadata_ = metadata

        delivery = NotificationDelivery(
            notification_id=notification.id,
            provider="meta",
            provider_message_id=provider_message_id,
            status=DeliveryStatus.delivered,
            response_code=response_code,
            response_body=response_body,
        )
        db.add(delivery)
        db.flush()
        return MetaDeliveryLegRecordOutcome(
            notification_delivery_id=delivery.id,
            replayed=False,
        )

    return execute_owner_command(
        db,
        definition=_META_DELIVERY_LEG_RECORD_COMMAND,
        context=context,
        operation=operation,
    )


def _coerce_uuid(value: str | UUID | None) -> UUID | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def _owner_team_link(conversation: InboxConversation) -> InboxConversationTeam | None:
    for link in conversation.team_links:
        if link.is_active and link.role == InboxTeamRole.owner.value:
            return link
    return None


def _owner_team_id(conversation: InboxConversation) -> UUID | None:
    if conversation.primary_service_team_id is not None:
        return conversation.primary_service_team_id
    link = _owner_team_link(conversation)
    return link.service_team_id if link is not None else None


def _reply_subject(conversation: InboxConversation, explicit: str | None) -> str:
    raw = (explicit or conversation.subject or "Message").strip() or "Message"
    if raw.lower().startswith("re:"):
        return raw[:200]
    return f"Re: {raw}"[:200]


def _reply_to_address(
    conversation: InboxConversation, explicit: str | None
) -> str | None:
    return team_inbox_routing.normalize_email_address(
        explicit or conversation.contact_address
    )


def _plain_text_reply(payload: InboxReplyPayload) -> str:
    body_text = (payload.body_text or "").strip()
    if body_text:
        return body_text
    body_html = (payload.body_html or "").strip()
    if not body_html:
        return ""
    text = _HTML_TAG_RE.sub(" ", body_html)
    return html.unescape(" ".join(text.split())).strip()


def _whatsapp_inbox_reply_bypasses_customer_policy(
    *,
    channel: NotificationChannel,
    payload: InboxReplyPayload,
) -> bool:
    metadata = dict(payload.metadata or {})
    sender_type = str(metadata.get("sender_type") or "").strip().lower()
    author_type = str(metadata.get("author_type") or "").strip().lower()
    automation_kind = str(metadata.get("automation_kind") or "").strip().lower()
    return channel == NotificationChannel.whatsapp and (
        payload.sent_by_person_id is not None
        or sender_type == "ai"
        or author_type == "ai"
        or automation_kind == "ai_intake"
    )


def _queue_outbox_reply(
    db: Session,
    *,
    conversation: InboxConversation,
    payload: InboxReplyPayload,
    channel: NotificationChannel,
    recipient: str,
    subject: str | None,
    body: str,
    now: datetime | None = None,
    from_address: str | None = None,
    metadata: dict[str, Any] | None = None,
    existing_message: InboxMessage | None = None,
) -> InboxReplyResult:
    intent_metadata = dict(payload.metadata or {})
    intent_metadata.update(metadata or {})
    intent_metadata.update(
        {
            "source": "team_inbox_reply",
            "conversation_id": str(conversation.id),
            "body_html": payload.body_html,
            "body_text": payload.body_text,
            "sent_by_person_id": str(payload.sent_by_person_id)
            if payload.sent_by_person_id
            else None,
            "cc": list(payload.cc_addresses),
            "bcc": list(payload.bcc_addresses),
        }
    )
    message = existing_message or InboxMessage(
        id=uuid4(), conversation_id=conversation.id
    )
    email_thread_headers: EmailThreadHeaders | None = None
    if channel == NotificationChannel.email:
        from app.services import team_inbox_receive

        email_thread_headers = payload.email_thread_headers
        if email_thread_headers is None:
            email_thread_headers = (
                team_inbox_receive.build_outbound_email_thread_headers(
                    db,
                    conversation_id=conversation.id,
                    outbound_message_id=message.id,
                )
            )
        intent_metadata["email_thread"] = email_thread_headers.as_metadata()
    linked_subscriber_id = conversation.subscriber_id
    if linked_subscriber_id is not None:
        intent_metadata["linked_subscriber_id"] = str(linked_subscriber_id)
    use_operational_audience = _whatsapp_inbox_reply_bypasses_customer_policy(
        channel=channel,
        payload=payload,
    )
    audience_type = (
        "operational"
        if use_operational_audience
        else "subscriber"
        if linked_subscriber_id is not None
        else "operational"
    )
    intent_subscriber_id = None if use_operational_audience else linked_subscriber_id
    result = submit(
        db,
        CommunicationIntent(
            subscriber_id=intent_subscriber_id,
            event_type="team_inbox.reply",
            category="service",
            communication_class=CommunicationClass.transactional,
            subject=subject,
            body=body,
            channels=(channel,),
            include_reseller=False,
            persist_policy_suppressions=False,
            recipients={channel: recipient},
            audience_type=audience_type,
            audience_id=(
                linked_subscriber_id
                if audience_type == "subscriber"
                else conversation.id
            ),
            resolve_subscriber_identity=False,
            metadata=intent_metadata,
            dedupe_key=payload.dedupe_key,
            delivery_latency=NotificationDeliveryLatency.immediate,
        ),
    )
    notification = next(
        (item for item in result.queued if item.status == NotificationStatus.queued),
        None,
    )
    if notification is None:
        return InboxReplyResult(
            kind="suppressed",
            conversation_id=str(conversation.id),
            to_email=recipient,
            reason=", ".join(result.suppressed)
            or "Communication policy suppressed reply",
        )

    queued_at = now or datetime.now(UTC)
    message.notification_id = notification.id
    message.channel_type = channel.value
    message.direction = InboxMessageDirection.outbound.value
    message.subject = subject
    message.body = body
    message.external_message_id = (
        email_thread_headers.message_id if email_thread_headers is not None else None
    )
    message.external_thread_id = conversation.external_thread_id
    message.from_address = from_address
    message.to_addresses = [recipient]
    message.cc_addresses = list(payload.cc_addresses)
    message.sent_at = queued_at
    message.metadata_ = {**intent_metadata, "delivery_status": "queued"}
    if existing_message is None:
        db.add(message)
    conversation.last_message_at = queued_at
    inbox_sla.record_outbound(
        db, message, occurred_at=message.sent_at or message.created_at
    )
    db.flush()
    author_name = str(
        intent_metadata.get("author_name")
        or intent_metadata.get("ai_display_name")
        or "Support"
    )
    sender_type = str(intent_metadata.get("sender_type") or "agent")
    team_inbox_realtime.publish_conversation_event(
        db,
        str(conversation.id),
        event_type=team_inbox_realtime.EventType.MESSAGE_NEW,
        payload=team_inbox_realtime.message_event_payload(
            conversation_id=str(conversation.id),
            message_id=str(message.id),
            body=message.body,
            direction=message.direction,
            channel_type=message.channel_type,
            created_at=message.created_at,
            author_name=author_name,
            extra={
                "sender_type": sender_type,
                "from_customer": False,
                "delivery_status": "queued",
            },
        ),
    )
    return InboxReplyResult(
        kind="queued",
        conversation_id=str(conversation.id),
        message_id=str(message.id),
        notification_id=notification.id,
        to_email=recipient,
    )


def _send_whatsapp_reply(
    db: Session,
    *,
    conversation: InboxConversation,
    payload: InboxReplyPayload,
    now: datetime | None,
    record_failure: bool = False,
    existing_message: InboxMessage | None = None,
) -> InboxReplyResult:
    recipient = normalize_phone_identifier(conversation.contact_address)
    if not recipient:
        return InboxReplyResult(
            kind="missing_recipient",
            conversation_id=str(conversation.id),
            reason="Conversation has no WhatsApp reply recipient",
        )
    body_text = _plain_text_reply(payload)
    if not body_text:
        return InboxReplyResult(
            kind="empty_body",
            conversation_id=str(conversation.id),
            reason="Reply body is required",
        )

    payload_metadata = dict(payload.metadata or {})
    raw_template_spec = payload_metadata.get("whatsapp_template")
    template_spec = raw_template_spec if isinstance(raw_template_spec, dict) else None
    template_name = (
        str(template_spec.get("name") or "").strip() if template_spec else ""
    )
    use_template = bool(template_spec and template_name)
    if not use_template:
        window = team_inbox_reply_window.decide_reply_window(
            db, conversation=conversation, now=now
        )
        if window.blocks_free_form:
            return InboxReplyResult(
                kind="reply_window_expired",
                conversation_id=str(conversation.id),
                reason=window.reason
                or "The 24-hour reply window has expired. Use an approved WhatsApp template or wait for the customer to message again.",
            )
    return _queue_outbox_reply(
        db,
        conversation=conversation,
        payload=payload,
        channel=NotificationChannel.whatsapp,
        recipient=recipient,
        subject=None,
        body=body_text if not use_template else f"[WhatsApp template: {template_name}]",
        now=now,
        metadata={
            "channel_type": InboxChannelType.whatsapp.value,
            "message_kind": "template" if use_template else "text",
            "whatsapp_template": template_spec if use_template else None,
        },
        existing_message=existing_message,
    )


def _send_field_job_reply(
    db: Session,
    *,
    conversation: InboxConversation,
    payload: InboxReplyPayload,
    now: datetime | None,
    existing_message: InboxMessage | None = None,
) -> InboxReplyResult:
    """Deliver a job-chat message in the app, over the conversation socket.

    There is no external transport and therefore no recipient address, no
    notification and no delivery receipt to wait for: both parties hold a
    bounded Sub session and subscribe to this conversation's topic. The
    message is sent the moment it is persisted and published.
    """
    body_text = _plain_text_reply(payload)
    if not body_text:
        return InboxReplyResult(
            kind="empty_body",
            conversation_id=str(conversation.id),
            reason="Reply body is required",
        )

    sent_at = now or datetime.now(UTC)
    message = existing_message or InboxMessage(conversation_id=conversation.id)
    message.channel_type = conversation.channel_type
    message.direction = InboxMessageDirection.outbound.value
    message.subject = None
    message.body = body_text
    message.external_thread_id = conversation.external_thread_id
    message.to_addresses = []
    message.cc_addresses = []
    message.sent_at = sent_at
    author_name = str((payload.metadata or {}).get("author_name") or "Technician")
    sender_type = str((payload.metadata or {}).get("sender_type") or "agent")
    message.metadata_ = {
        **(payload.metadata or {}),
        "channel_type": conversation.channel_type,
        "sent_by_person_id": str(payload.sent_by_person_id)
        if payload.sent_by_person_id
        else None,
        "delivery_status": "delivered",
    }
    if existing_message is None:
        db.add(message)
    conversation.last_message_at = sent_at
    db.flush()
    team_inbox_realtime.publish_conversation_event(
        db,
        str(conversation.id),
        event_type=team_inbox_realtime.EventType.MESSAGE_NEW,
        payload=team_inbox_realtime.message_event_payload(
            conversation_id=str(conversation.id),
            message_id=str(message.id),
            body=message.body,
            direction=message.direction,
            channel_type=message.channel_type,
            created_at=message.created_at,
            author_name=author_name,
            extra={
                "sender_type": sender_type,
                "from_customer": False,
                "delivery_status": "delivered",
            },
        ),
    )
    return InboxReplyResult(
        kind="queued",
        conversation_id=str(conversation.id),
        message_id=str(message.id),
    )


_SOCIAL_COMMENT_CHANNELS = {
    InboxChannelType.facebook_comment.value,
    InboxChannelType.instagram_comment.value,
}
_META_DM_CHANNELS = {
    InboxChannelType.facebook_messenger.value,
    InboxChannelType.instagram_dm.value,
}


def _social_value(
    conversation: InboxConversation,
    messages: list[InboxMessage],
    *keys: str,
) -> str:
    sources = [message.metadata_ or {} for message in reversed(messages)]
    sources.append(conversation.metadata_ or {})
    for source in sources:
        for key in keys:
            value = str(source.get(key) or "").strip()
            if value:
                return value
    return ""


def _social_comment_reply_target(
    messages: list[InboxMessage],
    payload: InboxReplyPayload,
) -> InboxMessage | None:
    reply_to_id = _social_comment_reply_target_id(payload)
    if reply_to_id is not None:
        return next(
            (
                message
                for message in messages
                if message.id == reply_to_id
                and message.direction == InboxMessageDirection.inbound.value
            ),
            None,
        )
    return next(
        (
            message
            for message in reversed(messages)
            if message.direction == InboxMessageDirection.inbound.value
        ),
        None,
    )


def _social_comment_reply_target_id(payload: InboxReplyPayload) -> UUID | None:
    reply_to = (payload.metadata or {}).get("reply_to")
    return (
        _coerce_uuid(str(reply_to.get("message_id") or ""))
        if isinstance(reply_to, dict)
        else None
    )


def _send_social_comment_reply(
    db: Session,
    *,
    conversation: InboxConversation,
    payload: InboxReplyPayload,
    now: datetime | None,
) -> InboxReplyResult:
    body_text = _plain_text_reply(payload)
    limit = (
        2_200
        if conversation.channel_type == InboxChannelType.instagram_comment.value
        else 8_000
    )
    if not body_text:
        return InboxReplyResult(
            kind="empty_body",
            conversation_id=str(conversation.id),
            reason="Reply body is required.",
        )
    if len(body_text) > limit:
        return InboxReplyResult(
            kind="invalid_body",
            conversation_id=str(conversation.id),
            reason=f"Reply must be {limit:,} characters or fewer.",
        )

    messages = (
        db.query(InboxMessage)
        .filter(InboxMessage.conversation_id == conversation.id)
        .order_by(InboxMessage.created_at.asc())
        .all()
    )
    requested_reply_to_id = _social_comment_reply_target_id(payload)
    inbound = _social_comment_reply_target(messages, payload)
    if requested_reply_to_id is not None and inbound is None:
        return InboxReplyResult(
            kind="invalid_reply_target",
            conversation_id=str(conversation.id),
            reason="Targeted public replies must target an inbound social comment.",
        )
    inbound_metadata = dict(inbound.metadata_ or {}) if inbound is not None else {}
    provider_comment_id = (
        str(inbound.external_message_id or "").strip() if inbound is not None else ""
    ) or _social_value(
        conversation,
        messages,
        "provider_comment_id",
        "comment_id",
        "external_comment_id",
    )
    account_id = _social_value(
        conversation,
        messages,
        "source_account_id",
        "provider_account_id",
        "provider_account_scope",
        "page_id",
        "instagram_account_id",
        "ig_account_id",
    )
    if not provider_comment_id or not account_id:
        return InboxReplyResult(
            kind="missing_provider_context",
            conversation_id=str(conversation.id),
            reason="This comment is missing its Meta reply details.",
        )
    provider_post_id = str(
        inbound_metadata.get("post_id") or ""
    ).strip() or _social_value(
        conversation,
        messages,
        "post_id",
    )
    provider_media_id = str(
        inbound_metadata.get("media_id") or ""
    ).strip() or _social_value(
        conversation,
        messages,
        "media_id",
    )
    root_provider_comment_id = (
        str(inbound_metadata.get("parent_provider_comment_id") or "").strip()
        or provider_comment_id
    )

    channel = (
        NotificationChannel.facebook_comment
        if conversation.channel_type == InboxChannelType.facebook_comment.value
        else NotificationChannel.instagram_comment
    )
    return _queue_outbox_reply(
        db,
        conversation=conversation,
        payload=payload,
        channel=channel,
        recipient=provider_comment_id,
        subject=None,
        body=body_text,
        now=now,
        from_address="Support",
        metadata={
            "channel_type": conversation.channel_type,
            "message_kind": "social_comment_reply",
            "provider": "meta",
            "provider_account_id": account_id,
            "parent_provider_comment_id": provider_comment_id,
            "root_provider_comment_id": root_provider_comment_id,
            "provider_post_id": provider_post_id or None,
            "provider_media_id": provider_media_id or None,
            "target_inbox_message_id": str(inbound.id) if inbound is not None else None,
        },
    )


def _send_meta_direct_reply(
    db: Session,
    *,
    conversation: InboxConversation,
    payload: InboxReplyPayload,
    now: datetime | None,
) -> InboxReplyResult:
    body_text = _plain_text_reply(payload)
    attachment_ids = (payload.metadata or {}).get("inbox_attachment_ids")
    has_attachments = isinstance(attachment_ids, list) and bool(attachment_ids)
    if not body_text and not has_attachments:
        return InboxReplyResult(
            kind="empty_body",
            conversation_id=str(conversation.id),
            reason="Reply body or attachment is required",
        )
    window = team_inbox_reply_window.decide_reply_window(
        db, conversation=conversation, now=now
    )
    if window.blocks_free_form:
        return InboxReplyResult(
            kind="reply_window_expired",
            conversation_id=str(conversation.id),
            reason=window.reason
            or "The 24-hour reply window has expired. A new free-form reply cannot be sent until the customer messages again.",
        )
    messages = (
        db.query(InboxMessage)
        .filter(InboxMessage.conversation_id == conversation.id)
        .order_by(InboxMessage.created_at.asc())
        .all()
    )
    account_id = _social_value(
        conversation,
        messages,
        "provider_account_id",
        "provider_account_scope",
        "page_or_account_id",
        "page_id",
        "instagram_account_id",
        "ig_account_id",
    )
    recipient = str(conversation.contact_address or "").strip()
    if not account_id or not recipient:
        return InboxReplyResult(
            kind="missing_provider_context",
            conversation_id=str(conversation.id),
            reason="This Meta conversation is missing its reply details.",
        )
    channel = (
        NotificationChannel.facebook_messenger
        if conversation.channel_type == InboxChannelType.facebook_messenger.value
        else NotificationChannel.instagram_dm
    )
    return _queue_outbox_reply(
        db,
        conversation=conversation,
        payload=payload,
        channel=channel,
        recipient=recipient,
        subject=None,
        body=body_text,
        now=now,
        from_address="Support",
        metadata={
            "channel_type": conversation.channel_type,
            "message_kind": "direct_message",
            "provider": "meta",
            "provider_account_id": account_id,
        },
    )


def send_inbox_reply(
    db: Session,
    *,
    conversation: InboxConversation,
    payload: InboxReplyPayload,
    now: datetime | None = None,
    record_failure: bool = False,
    existing_message: InboxMessage | None = None,
) -> InboxReplyResult:
    if not conversation.is_active:
        return InboxReplyResult(
            kind="invalid_conversation",
            conversation_id=str(conversation.id),
            reason="Conversation is inactive",
        )
    if conversation.status == InboxConversationStatus.resolved.value:
        return InboxReplyResult(
            kind="invalid_conversation",
            conversation_id=str(conversation.id),
            reason="Resolved conversations cannot be replied to",
        )
    if payload.sent_by_person_id is not None:
        ai_conversation_ownership.require_human_control(
            db,
            conversation_id=conversation.id,
            mutation=ai_conversation_ownership.HumanConversationMutation.reply,
        )

    if conversation.channel_type == InboxChannelType.whatsapp.value:
        return _send_whatsapp_reply(
            db,
            conversation=conversation,
            payload=payload,
            now=now,
            record_failure=record_failure,
            existing_message=existing_message,
        )

    if conversation.channel_type in {
        InboxChannelType.field_job.value,
        InboxChannelType.chat_widget.value,
    }:
        return _send_field_job_reply(
            db,
            conversation=conversation,
            payload=payload,
            now=now,
            existing_message=existing_message,
        )

    if conversation.channel_type in _META_DM_CHANNELS:
        return _send_meta_direct_reply(
            db,
            conversation=conversation,
            payload=payload,
            now=now,
        )

    if conversation.channel_type in _SOCIAL_COMMENT_CHANNELS:
        return _send_social_comment_reply(
            db,
            conversation=conversation,
            payload=payload,
            now=now,
        )

    if conversation.channel_type == InboxChannelType.website_fiber.value:
        return InboxReplyResult(
            kind="unsupported_channel",
            conversation_id=str(conversation.id),
            reason="Outbound replies for fiber website inquiries are not configured",
        )

    to_email = _reply_to_address(conversation, payload.to_email)
    if not to_email:
        return InboxReplyResult(
            kind="missing_recipient",
            conversation_id=str(conversation.id),
            reason="Conversation has no reply recipient",
        )

    body_html = (payload.body_html or "").strip()
    if not body_html:
        return InboxReplyResult(
            kind="empty_body",
            conversation_id=str(conversation.id),
            reason="Reply body is required",
        )

    owner_link = _owner_team_link(conversation)
    service_team_id = _owner_team_id(conversation)
    if owner_link is None and service_team_id is None:
        owner_link = (
            db.query(InboxConversationTeam)
            .filter(InboxConversationTeam.conversation_id == conversation.id)
            .filter(InboxConversationTeam.role == InboxTeamRole.owner.value)
            .filter(InboxConversationTeam.is_active.is_(True))
            .one_or_none()
        )
        service_team_id = owner_link.service_team_id if owner_link is not None else None
    sender = team_outbound.resolve_team_email_sender(
        db,
        service_team_id=service_team_id,
        activity="support_ticket",
        metadata_override=owner_link.metadata_ if owner_link is not None else None,
    )
    config = sender.config
    subject = _reply_subject(conversation, payload.subject)
    result = _queue_outbox_reply(
        db,
        conversation=conversation,
        payload=payload,
        channel=NotificationChannel.email,
        recipient=to_email,
        subject=subject,
        body=payload.body_text or _plain_text_reply(payload),
        now=now,
        from_address=config.get("from_email"),
        metadata={
            "service_team_id": sender.service_team_id,
            "sender_key": config.get("sender_key") or sender.sender_key,
            "activity": sender.activity,
        },
        existing_message=existing_message,
    )
    return InboxReplyResult(
        kind=result.kind,
        conversation_id=result.conversation_id,
        message_id=result.message_id,
        notification_id=result.notification_id,
        service_team_id=sender.service_team_id,
        sender_key=config.get("sender_key") or sender.sender_key,
        activity=sender.activity,
        from_address=config.get("from_email"),
        to_email=to_email,
        reason=result.reason,
    )


def send_ai_intake_follow_up(
    db: Session,
    *,
    conversation: InboxConversation,
    payload: AiIntakeFollowUpPayload,
    now: datetime | None = None,
) -> InboxReplyResult:
    """Queue one approved intake clarification through the channel owner."""

    question = " ".join(str(payload.question or "").split())
    approved_questions = {
        " ".join(item.split()) for item in APPROVED_FOLLOW_UP_QUESTIONS
    }
    if question not in approved_questions:
        return InboxReplyResult(
            kind="invalid_body",
            conversation_id=str(conversation.id),
            reason="AI intake follow-up question is not approved",
        )
    if conversation.channel_type not in {
        InboxChannelType.whatsapp.value,
        InboxChannelType.chat_widget.value,
        *_META_DM_CHANNELS,
    }:
        return InboxReplyResult(
            kind="unsupported_channel",
            conversation_id=str(conversation.id),
            reason="AI intake follow-up delivery is unsupported on this channel",
        )
    return send_inbox_reply(
        db,
        conversation=conversation,
        payload=InboxReplyPayload(
            body_html=question,
            body_text=question,
            metadata={
                "sender_type": "ai",
                "author_type": "ai",
                "automation_kind": "ai_intake",
                "ai_display_name": payload.display_name,
                "ai_intake_session_id": str(payload.session_id)
                if payload.session_id
                else None,
                "ai_intake_policy_id": str(payload.policy_id)
                if payload.policy_id
                else None,
                "ai_intake_policy_version_id": str(payload.policy_version_id)
                if payload.policy_version_id
                else None,
                "ai_message_purpose": "clarification",
                "ai_intake_follow_up": True,
                "ai_intake_config_id": str(payload.config_id)
                if payload.config_id is not None
                else None,
                "ai_intake_inbound_message_id": str(payload.inbound_message_id),
                "ai_intake_follow_up_count": payload.follow_up_count,
                "author_name": payload.display_name,
            },
            dedupe_key=f"ai-intake-follow-up:{payload.inbound_message_id}",
        ),
        now=now,
    )


def send_ai_intake_message(
    db: Session,
    *,
    conversation: InboxConversation,
    body_text: str,
    metadata: dict[str, Any],
    dedupe_key: str,
    now: datetime | None = None,
) -> InboxReplyResult:
    """Queue a customer-visible AI intake message through Team Inbox outbound."""

    if conversation.channel_type not in {
        InboxChannelType.whatsapp.value,
        InboxChannelType.chat_widget.value,
        *_META_DM_CHANNELS,
    }:
        return InboxReplyResult(
            kind="unsupported_channel",
            conversation_id=str(conversation.id),
            reason="AI intake delivery is unsupported on this channel",
        )
    clean_body = " ".join(str(body_text or "").split())
    if not clean_body:
        return InboxReplyResult(
            kind="empty_body",
            conversation_id=str(conversation.id),
            reason="AI intake message body is required",
        )
    return send_inbox_reply(
        db,
        conversation=conversation,
        payload=InboxReplyPayload(
            body_html=clean_body,
            body_text=clean_body,
            metadata={
                **metadata,
                "sender_type": "ai",
                "author_type": "ai",
                "automation_kind": metadata.get("automation_kind") or "ai_intake",
                "author_name": metadata.get("author_name")
                or metadata.get("ai_display_name")
                or "Dotmac Support",
            },
            dedupe_key=dedupe_key,
        ),
        now=now,
    )


def send_inbox_reply_for_conversation(
    db: Session,
    *,
    conversation_id: str | UUID,
    payload: InboxReplyPayload,
    now: datetime | None = None,
    record_failure: bool = False,
) -> InboxReplyResult:
    conversation_uuid = _coerce_uuid(conversation_id)
    conversation = (
        db.get(InboxConversation, conversation_uuid) if conversation_uuid else None
    )
    if conversation is None:
        return InboxReplyResult(
            kind="conversation_not_found",
            conversation_id=str(conversation_id),
            reason="Conversation not found",
        )
    return send_inbox_reply(
        db,
        conversation=conversation,
        payload=payload,
        now=now,
        record_failure=record_failure,
    )


def send_inbox_reply_for_conversation_committed(
    db: Session,
    *,
    conversation_id: str | UUID,
    payload: InboxReplyPayload,
    now: datetime | None = None,
    record_failure: bool = False,
) -> InboxReplyResult:
    return _commit(
        db,
        lambda: send_inbox_reply_for_conversation(
            db,
            conversation_id=conversation_id,
            payload=payload,
            now=now,
            record_failure=record_failure,
        ),
    )


def _record_failed_outbound(
    db: Session,
    *,
    conversation: InboxConversation,
    payload: InboxReplyPayload,
    channel_type: str,
    to_addresses: list[str],
    reason: str,
    provider_result: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    from_address: str | None = None,
    subject: str | None = None,
    now: datetime | None = None,
) -> str:
    attempted_at = now or datetime.now(UTC)
    combined_metadata = dict(payload.metadata or {})
    combined_metadata.update(metadata or {})
    combined_metadata.update(
        {
            "source": "team_inbox_reply",
            "delivery_status": "failed",
            "send_error": reason,
            "retry_count": 0,
            "sent_by_person_id": str(payload.sent_by_person_id)
            if payload.sent_by_person_id
            else None,
            "cc": list(payload.cc_addresses),
            "bcc": list(payload.bcc_addresses),
        }
    )
    if provider_result:
        combined_metadata["provider_result"] = provider_result
    message = InboxMessage(
        conversation_id=conversation.id,
        channel_type=channel_type,
        direction=InboxMessageDirection.outbound.value,
        subject=subject or payload.subject,
        body=payload.body_text or payload.body_html,
        external_thread_id=conversation.external_thread_id,
        from_address=from_address,
        to_addresses=to_addresses,
        cc_addresses=list(payload.cc_addresses),
        sent_at=attempted_at,
        metadata_=combined_metadata,
    )
    db.add(message)
    conversation.last_message_at = attempted_at
    db.flush()
    return str(message.id)


def retry_outbound_message(
    db: Session,
    *,
    message: InboxMessage,
    sent_by_person_id: str | UUID | None = None,
    now: datetime | None = None,
) -> InboxReplyResult:
    from app.services import team_inbox_receive

    metadata = dict(message.metadata_ or {})
    if metadata.get("delivery_status") != "failed":
        return InboxReplyResult(
            kind="invalid_message",
            conversation_id=str(message.conversation_id),
            message_id=str(message.id),
            reason="Only failed outbound inbox messages can be retried",
        )
    conversation = db.get(InboxConversation, message.conversation_id)
    if conversation is None:
        return InboxReplyResult(
            kind="invalid_conversation",
            conversation_id=str(message.conversation_id),
            message_id=str(message.id),
            reason="Conversation not found",
        )
    retry_count = int(metadata.get("retry_count") or 0) + 1
    retry_metadata = {
        key: value
        for key, value in metadata.items()
        if key
        not in {
            "delivery_status",
            "send_error",
            "last_retry_at",
            "last_retry_result",
            "retried_message_id",
        }
    }
    retry_metadata.update(
        {
            "source_route": "team_inbox_retry",
            "retry_of_message_id": str(message.id),
            "retry_count": retry_count,
        }
    )
    result = send_inbox_reply(
        db,
        conversation=conversation,
        payload=InboxReplyPayload(
            body_html=message.body or "",
            body_text=message.body,
            subject=message.subject,
            to_email=(message.to_addresses or [None])[0],
            cc_addresses=tuple(message.cc_addresses or ()),
            bcc_addresses=tuple(
                str(value)
                for value in metadata.get("bcc", ())
                if isinstance(value, str)
            )
            if isinstance(metadata.get("bcc"), list)
            else (),
            sent_by_person_id=sent_by_person_id,
            metadata=retry_metadata,
            email_thread_headers=(
                team_inbox_receive.EmailThreadHeaders.from_metadata(
                    metadata.get("email_thread")
                )
                if conversation.channel_type == InboxChannelType.email.value
                else None
            ),
        ),
        now=now,
        record_failure=False,
    )
    metadata["retry_count"] = retry_count
    metadata["last_retry_at"] = (now or datetime.now(UTC)).isoformat()
    metadata["last_retry_result"] = result.kind
    if result.kind in {"sent", "queued"}:
        metadata["delivery_status"] = "retried"
        metadata["retried_message_id"] = result.message_id
    message.metadata_ = metadata
    db.flush()
    return result


SCHEDULED_DELIVERY_STATUS = "scheduled"


def schedule_inbox_reply(
    db: Session,
    *,
    conversation: InboxConversation,
    payload: InboxReplyPayload,
    send_after: datetime,
) -> InboxMessage:
    """Record a reply to be sent later, without sending it now.

    Stored as a normal outbound ``InboxMessage`` with ``sent_at`` unset and a
    ``scheduled`` delivery status, so the thread shows what is queued rather
    than hiding it until it goes. ``release_due_scheduled_replies`` sends it.

    No new table: the message *is* the queue entry, which keeps one row per
    reply whether it was sent immediately or later, and means a scheduled reply
    already carries its attachments and provenance.
    """
    if send_after.tzinfo is None:
        send_after = send_after.replace(tzinfo=UTC)
    if send_after <= datetime.now(UTC):
        raise ValueError("Choose a send time in the future.")

    metadata = dict(payload.metadata or {})
    metadata.update(
        {
            "source": "team_inbox_reply",
            "delivery_status": SCHEDULED_DELIVERY_STATUS,
            "scheduled_for": send_after.isoformat(),
            "body_text": payload.body_text,
            "body_html": payload.body_html,
            "sent_by_person_id": str(payload.sent_by_person_id)
            if payload.sent_by_person_id
            else None,
            "cc": list(payload.cc_addresses),
            "bcc": list(payload.bcc_addresses),
        }
    )
    message = InboxMessage(
        conversation_id=conversation.id,
        channel_type=conversation.channel_type,
        direction="outbound",
        subject=_reply_subject(conversation, payload.subject),
        body=payload.body_text,
        from_address=None,
        cc_addresses=list(payload.cc_addresses),
        sent_at=None,
        metadata_=metadata,
    )
    db.add(message)
    db.flush()
    return message


def due_scheduled_replies(
    db: Session, *, now: datetime | None = None, limit: int = 50
) -> list[InboxMessage]:
    """Scheduled replies whose send time has passed."""
    moment = (now or datetime.now(UTC)).isoformat()
    return (
        db.query(InboxMessage)
        .filter(InboxMessage.direction == "outbound")
        .filter(InboxMessage.sent_at.is_(None))
        .filter(
            InboxMessage.metadata_["delivery_status"].as_string()
            == SCHEDULED_DELIVERY_STATUS
        )
        .filter(InboxMessage.metadata_["scheduled_for"].as_string() <= moment)
        .order_by(InboxMessage.created_at.asc())
        .limit(limit)
        # Two maintenance workers must never claim the same scheduled reply.
        # The owner transaction holds these row locks through intent staging.
        .with_for_update(skip_locked=True)
        .all()
    )


def send_scheduled_reply(db: Session, *, message: InboxMessage) -> InboxReplyResult:
    """Send one previously scheduled reply through the normal outbound path."""
    conversation = db.get(InboxConversation, message.conversation_id)
    if conversation is None or not conversation.is_active:
        metadata = dict(message.metadata_ or {})
        metadata["delivery_status"] = "cancelled"
        metadata["cancel_reason"] = "conversation is no longer active"
        message.metadata_ = metadata
        db.flush()
        return InboxReplyResult(
            kind="cancelled",
            conversation_id=str(message.conversation_id),
            reason="conversation is no longer active",
        )

    metadata = dict(message.metadata_ or {})
    release_metadata = {
        key: value
        for key, value in metadata.items()
        if key not in {"delivery_status", "scheduled_for", "body_html", "body_text"}
    }
    release_metadata["source"] = "team_inbox_scheduled_reply"
    result = send_inbox_reply(
        db,
        conversation=conversation,
        payload=InboxReplyPayload(
            body_html=str(metadata.get("body_html") or ""),
            body_text=str(metadata.get("body_text") or message.body or ""),
            subject=message.subject,
            sent_by_person_id=metadata.get("sent_by_person_id"),
            cc_addresses=tuple(
                str(value) for value in metadata.get("cc", ()) if isinstance(value, str)
            )
            if isinstance(metadata.get("cc"), list)
            else (),
            bcc_addresses=tuple(
                str(value)
                for value in metadata.get("bcc", ())
                if isinstance(value, str)
            )
            if isinstance(metadata.get("bcc"), list)
            else (),
            metadata=release_metadata,
        ),
        record_failure=True,
        existing_message=message,
    )
    released_at = datetime.now(UTC)
    current_metadata = dict(message.metadata_ or {})
    current_metadata["scheduled_released_at"] = released_at.isoformat()
    if result.kind not in {"sent", "queued"}:
        current_metadata["delivery_status"] = "failed"
        current_metadata["send_error"] = result.reason or "Scheduled reply failed"
        current_metadata["retry_count"] = int(current_metadata.get("retry_count") or 0)
        message.sent_at = released_at
    message.metadata_ = current_metadata
    db.flush()
    return result


def send_transcript(
    db: Session,
    *,
    conversation: InboxConversation,
    recipient: str,
    subject: str,
    body_html: str,
    sent_by_person_id: str | UUID | None = None,
) -> InboxReplyResult:
    """Deliver a transcript to an arbitrary address.

    Uses the same communication intent as a reply so the team's sender and
    delivery handling apply, but records no `InboxMessage`: a transcript is a
    copy of the conversation, not a new turn in it, and adding it to the thread
    would make the next transcript include the previous one.
    """
    result = submit(
        db,
        CommunicationIntent(
            subscriber_id=conversation.subscriber_id,
            event_type="team_inbox.transcript",
            category="service",
            communication_class=CommunicationClass.transactional,
            subject=subject,
            body=body_html,
            channels=(NotificationChannel.email,),
            include_reseller=False,
            persist_policy_suppressions=False,
            recipients={NotificationChannel.email: recipient},
            metadata={
                "source": "team_inbox_transcript",
                "conversation_id": str(conversation.id),
                "sent_by_person_id": str(sent_by_person_id)
                if sent_by_person_id
                else None,
            },
            delivery_latency=NotificationDeliveryLatency.immediate,
        ),
    )
    notification = next(
        (item for item in result.queued if item.status == NotificationStatus.queued),
        None,
    )
    if notification is None:
        return InboxReplyResult(
            kind="suppressed",
            conversation_id=str(conversation.id),
            to_email=recipient,
            reason=", ".join(result.suppressed)
            or "Communication policy suppressed the transcript",
        )
    return InboxReplyResult(
        kind="queued",
        conversation_id=str(conversation.id),
        to_email=recipient,
    )
