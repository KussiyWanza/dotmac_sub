"""Source of truth for customer and reseller communication decisions."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.notification import (
    CommunicationIntentRecord,
    Notification,
    NotificationChannel,
    NotificationStatus,
)
from app.models.subscriber import Reseller, ResellerUser, Subscriber, SubscriberContact
from app.schemas.notification import NotificationCreate
from app.services.communication_eligibility import suppression_reason
from app.services.customer_notification_policy import (
    resolve_subscriber_id_for_recipient,
)
from app.services.notification_channel_policy import resolve_notification_channels

MAX_EMAIL_ATTACHMENT_BYTES = 10 * 1024 * 1024


class CommunicationClass(enum.StrEnum):
    transactional = "transactional"
    marketing = "marketing"
    operational = "operational"


class CommunicationAttachmentKind(enum.StrEnum):
    invoice_pdf = "invoice_pdf"


@dataclass(frozen=True)
class CommunicationAttachment:
    """Durable reference to content materialized only during delivery."""

    kind: CommunicationAttachmentKind
    entity_id: UUID
    filename: str
    content_type: str = "application/pdf"

    def to_metadata(self) -> dict[str, str]:
        return {
            "kind": self.kind.value,
            "entity_id": str(self.entity_id),
            "filename": self.filename,
            "content_type": self.content_type,
        }


@dataclass(frozen=True)
class CommunicationIntent:
    subscriber_id: UUID | None
    event_type: str
    category: str
    subject: str | None
    body: str | None
    template_id: UUID | None = None
    template_code: str | None = None
    communication_class: CommunicationClass = CommunicationClass.transactional
    default_channels: tuple[NotificationChannel, ...] = (NotificationChannel.email,)
    channels: tuple[NotificationChannel, ...] | None = None
    include_reseller: bool = True
    persist_policy_suppressions: bool = True
    recipients: dict[NotificationChannel, str] = field(default_factory=dict)
    audience_type: str = "subscriber"
    audience_id: UUID | None = None
    resolve_subscriber_identity: bool = True
    metadata: dict[str, object] = field(default_factory=dict)
    attachments: tuple[CommunicationAttachment, ...] = ()
    dedupe_key: str | None = None
    send_at: datetime | None = None
    requested_status: NotificationStatus = NotificationStatus.queued
    requested_last_error: str | None = None


@dataclass(frozen=True)
class CommunicationIntentResult:
    intent_id: UUID
    deliveries: tuple[Notification, ...]
    queued: tuple[Notification, ...]
    suppressed: tuple[str, ...]


def list_intents(
    db: Session,
    *,
    subscriber_id: UUID | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[CommunicationIntentRecord]:
    query = db.query(CommunicationIntentRecord)
    if subscriber_id is not None:
        query = query.filter(CommunicationIntentRecord.subscriber_id == subscriber_id)
    if status:
        query = query.filter(CommunicationIntentRecord.status == status)
    return list(
        query.order_by(CommunicationIntentRecord.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )


def _subscriber_addresses(
    db: Session, subscriber: Subscriber, channel: NotificationChannel
) -> list[str]:
    addresses: list[str] = []
    if channel == NotificationChannel.email:
        if subscriber.email:
            addresses.append(subscriber.email)
    elif channel in {NotificationChannel.sms, NotificationChannel.whatsapp}:
        if subscriber.phone:
            addresses.append(subscriber.phone)
    elif channel == NotificationChannel.push:
        addresses.append(str(subscriber.id))

    if channel != NotificationChannel.push:
        contacts = (
            db.query(SubscriberContact)
            .filter(SubscriberContact.subscriber_id == subscriber.id)
            .filter(SubscriberContact.receives_notifications.is_(True))
            .all()
        )
        for contact in contacts:
            if channel == NotificationChannel.email and contact.email:
                addresses.append(contact.email)
            elif channel == NotificationChannel.sms and contact.phone:
                addresses.append(contact.phone)
            elif channel == NotificationChannel.whatsapp:
                address = contact.whatsapp or contact.phone
                if address:
                    addresses.append(address)
    return list(dict.fromkeys(address.strip() for address in addresses if address))


def _reseller_addresses(
    db: Session, reseller: Reseller, channel: NotificationChannel
) -> list[str]:
    addresses: list[str] = []
    if channel == NotificationChannel.email and reseller.contact_email:
        addresses.append(reseller.contact_email)
    elif channel in {NotificationChannel.sms, NotificationChannel.whatsapp}:
        if reseller.contact_phone:
            addresses.append(reseller.contact_phone)
    if channel == NotificationChannel.email:
        addresses.extend(
            email
            for (email,) in db.query(ResellerUser.email)
            .filter(ResellerUser.reseller_id == reseller.id)
            .filter(ResellerUser.is_active.is_(True))
            .filter(ResellerUser.email.is_not(None))
            .all()
            if email
        )
    return list(dict.fromkeys(address.strip() for address in addresses if address))


def submit(db: Session, intent: CommunicationIntent) -> CommunicationIntentResult:
    from app.services.notification import notifications as notification_service

    if intent.audience_type not in {"subscriber", "system_user", "reseller_user"}:
        raise ValueError("Unsupported communication audience type")
    if intent.audience_type != "subscriber" and (
        intent.subscriber_id is not None
        or intent.include_reseller
        or intent.communication_class == CommunicationClass.marketing
    ):
        raise ValueError("Internal communication cannot use customer policies")
    if intent.audience_type != "subscriber" and intent.audience_id is None:
        raise ValueError("Internal communication requires an audience identifier")
    if intent.attachments and NotificationChannel.email not in (
        intent.channels or intent.default_channels
    ):
        raise ValueError("Communication attachments require an email channel")

    attachment_metadata = [item.to_metadata() for item in intent.attachments]
    attachment_envelope: dict[str, object] = (
        {"attachments": attachment_metadata} if attachment_metadata else {}
    )

    resolved_subscriber_id = intent.subscriber_id
    if (
        intent.audience_type == "subscriber"
        and intent.resolve_subscriber_identity
        and resolved_subscriber_id is None
    ):
        for identity_recipient in intent.recipients.values():
            resolved_subscriber_id = resolve_subscriber_id_for_recipient(
                db, identity_recipient
            )
            if resolved_subscriber_id is not None:
                break
    subscriber = (
        db.get(Subscriber, resolved_subscriber_id) if resolved_subscriber_id else None
    )
    if (
        intent.audience_type == "subscriber"
        and intent.subscriber_id is not None
        and subscriber is None
    ):
        raise ValueError("Subscriber not found")
    channels = intent.channels or resolve_notification_channels(
        db,
        template_code=intent.template_code,
        event_type=intent.event_type,
        category=intent.category,
        default_channels=intent.default_channels,
    )
    if intent.dedupe_key:
        existing = (
            db.query(CommunicationIntentRecord)
            .filter(CommunicationIntentRecord.dedupe_key == intent.dedupe_key)
            .one_or_none()
        )
        if existing is not None:
            return CommunicationIntentResult(
                intent_id=existing.id,
                deliveries=tuple(existing.notifications),
                queued=tuple(
                    item
                    for item in existing.notifications
                    if item.status == NotificationStatus.queued
                ),
                suppressed=tuple(existing.suppression_reasons or []),
            )

    record = CommunicationIntentRecord(
        subscriber_id=subscriber.id if subscriber else None,
        event_type=intent.event_type,
        category=intent.category,
        communication_class=intent.communication_class.value,
        template_id=intent.template_id,
        template_code=intent.template_code,
        subject=intent.subject,
        body=intent.body,
        channels=[channel.value for channel in channels],
        include_reseller=intent.include_reseller,
        status="pending",
        suppression_reasons=[],
        dedupe_key=intent.dedupe_key,
        scheduled_for=intent.send_at,
        metadata_={
            **intent.metadata,
            **attachment_envelope,
            "audience_type": intent.audience_type,
            "audience_id": str(intent.audience_id) if intent.audience_id else None,
        },
    )
    db.add(record)
    db.flush()

    suppressed: list[str] = []
    if intent.communication_class == CommunicationClass.marketing and (
        subscriber is None or not subscriber.marketing_opt_in
    ):
        record.status = "suppressed"
        record.suppression_reasons = ["marketing_opt_out"]
        record.processed_at = datetime.now(UTC)
        db.flush()
        return CommunicationIntentResult(
            intent_id=record.id,
            deliveries=(),
            queued=(),
            suppressed=("marketing_opt_out",),
        )

    queued: list[Notification] = []
    for channel in channels:
        explicit_recipient = intent.recipients.get(channel)
        delivery_recipients = (
            [explicit_recipient]
            if explicit_recipient
            else _subscriber_addresses(db, subscriber, channel)
            if subscriber
            else []
        )
        if not delivery_recipients:
            suppressed.append(f"subscriber:{channel.value}:missing_address")
        for delivery_recipient in delivery_recipients:
            reason = suppression_reason(
                db,
                channel=channel,
                category=intent.category,
                address=delivery_recipient,
            )
            if reason:
                suppressed.append(f"subscriber:{channel.value}:{reason}")
                if not intent.persist_policy_suppressions:
                    continue
            if not reason or intent.persist_policy_suppressions:
                payload = NotificationCreate(
                    template_id=intent.template_id,
                    subscriber_id=subscriber.id if subscriber else None,
                    communication_intent_id=record.id,
                    audience_type=intent.audience_type,
                    audience_id=(
                        subscriber.id if subscriber is not None else intent.audience_id
                    ),
                    channel=channel,
                    event_type=intent.event_type,
                    category=intent.category,
                    recipient=delivery_recipient,
                    subject=intent.subject,
                    body=intent.body,
                    status=intent.requested_status,
                    send_at=intent.send_at,
                    last_error=intent.requested_last_error,
                    metadata_={
                        **intent.metadata,
                        **attachment_envelope,
                    },
                )
                if intent.audience_type == "subscriber":
                    notification = (
                        notification_service.queue_customer_notification(db, payload)
                        if intent.persist_policy_suppressions
                        else notification_service.queue_event_notification(db, payload)
                    )
                else:
                    notification = notification_service.queue_internal_notification(
                        db, payload
                    )
                if notification is None:
                    suppressed.append(f"subscriber:{channel.value}:customer_policy")
                else:
                    queued.append(notification)

        reseller = subscriber.reseller if subscriber else None
        if (
            not intent.include_reseller
            or reseller is None
            or reseller.is_house
            or not reseller.is_active
        ):
            continue
        for reseller_recipient in _reseller_addresses(db, reseller, channel):
            reseller_reason = suppression_reason(
                db,
                channel=channel,
                category=intent.category,
                address=reseller_recipient,
            )
            if reseller_reason:
                suppressed.append(f"reseller:{channel.value}:{reseller_reason}")
                continue
            queued.append(
                notification_service.queue_internal_notification(
                    db,
                    NotificationCreate(
                        template_id=intent.template_id,
                        communication_intent_id=record.id,
                        audience_type="reseller",
                        audience_id=reseller.id,
                        channel=channel,
                        event_type=intent.event_type,
                        category=intent.category,
                        recipient=reseller_recipient,
                        subject=intent.subject,
                        body=intent.body,
                        status=intent.requested_status,
                        send_at=intent.send_at,
                        metadata_={
                            **intent.metadata,
                            **attachment_envelope,
                            "subject_subscriber_id": str(subscriber.id)
                            if subscriber
                            else None,
                        },
                    ),
                )
            )
    active_queued = any(
        notification.status == NotificationStatus.queued for notification in queued
    )
    record.status = (
        "partial"
        if active_queued and suppressed
        else "expanded"
        if active_queued
        else "suppressed"
    )
    record.suppression_reasons = suppressed
    record.processed_at = datetime.now(UTC)
    db.flush()
    return CommunicationIntentResult(
        intent_id=record.id,
        deliveries=tuple(queued),
        queued=tuple(
            item for item in queued if item.status == NotificationStatus.queued
        ),
        suppressed=tuple(suppressed),
    )


def record_delivery_outcome(db: Session, notification: Notification) -> None:
    """Project outbox delivery state into its intent, campaign, and inbox lineage."""
    from app.models.comms_campaign import (
        CampaignRecipient,
        CampaignRecipientStatus,
    )
    from app.models.team_inbox import InboxMessage

    db.flush()

    message = (
        db.query(InboxMessage)
        .filter(InboxMessage.notification_id == notification.id)
        .one_or_none()
    )
    if message is not None:
        metadata = dict(message.metadata_ or {})
        metadata["delivery_status"] = notification.status.value
        if notification.last_error:
            metadata["send_error"] = notification.last_error
        else:
            metadata.pop("send_error", None)
        message.metadata_ = metadata
        if notification.status == NotificationStatus.delivered:
            message.sent_at = notification.sent_at or datetime.now(UTC)

    campaign_recipient = (
        db.query(CampaignRecipient)
        .filter(CampaignRecipient.notification_id == notification.id)
        .one_or_none()
    )
    if campaign_recipient is not None:
        if notification.status == NotificationStatus.delivered:
            campaign_recipient.status = CampaignRecipientStatus.delivered.value
            campaign_recipient.delivered_at = notification.sent_at or datetime.now(UTC)
            campaign_recipient.failed_reason = None
        elif (
            notification.status == NotificationStatus.failed
            and notification.send_at is None
        ):
            campaign_recipient.status = CampaignRecipientStatus.failed.value
            campaign_recipient.failed_reason = notification.last_error
        elif notification.status == NotificationStatus.canceled:
            campaign_recipient.status = CampaignRecipientStatus.skipped.value
            campaign_recipient.failed_reason = notification.last_error
        from app.services.comms_campaigns import refresh_campaign_delivery_state

        refresh_campaign_delivery_state(db, campaign_recipient.campaign_id)

    if notification.communication_intent_id is None:
        return
    intent_record = db.get(
        CommunicationIntentRecord, notification.communication_intent_id
    )
    if intent_record is None:
        return
    delivery_rows = (
        db.query(Notification.status, Notification.send_at)
        .filter(Notification.communication_intent_id == intent_record.id)
        .all()
    )
    states = {status for status, _send_at in delivery_rows}
    if states & {NotificationStatus.queued, NotificationStatus.sending}:
        intent_record.status = "delivering"
    elif states and states <= {NotificationStatus.delivered}:
        intent_record.status = "delivered"
    elif any(
        status == NotificationStatus.failed and send_at is not None
        for status, send_at in delivery_rows
    ):
        intent_record.status = "retrying"
    elif NotificationStatus.delivered in states:
        intent_record.status = "partial"
    elif states:
        intent_record.status = "failed"
    intent_record.updated_at = datetime.now(UTC)
    db.flush()
