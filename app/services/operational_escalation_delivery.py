from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.operational_escalation import (
    OperationalDeliveryStatus,
    OperationalEscalationDelivery,
    OperationalEscalationEvent,
    OperationalEscalationStatus,
    OperationalNotificationChannel,
    OperationalParticipantType,
    OperationalRoomLink,
    OperationalRoomProvider,
)
from app.models.service_team import ServiceTeamMember
from app.models.subscriber import Reseller, ResellerUser, Subscriber
from app.models.system_user import SystemUser
from app.services.notification_status_policy import status_allows_notification

logger = logging.getLogger(__name__)

DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_BACKOFF_SECONDS = (60, 300, 900)


@dataclass(frozen=True)
class DeliveryTarget:
    recipient_type: str
    recipient_id: str | None
    address: str


def dispatch_pending_deliveries(
    db: Session,
    *,
    limit: int = 100,
    now: datetime | None = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> list[OperationalEscalationDelivery]:
    timestamp = now or datetime.now(UTC)
    deliveries = (
        db.query(OperationalEscalationDelivery)
        .join(OperationalEscalationEvent)
        .filter(
            OperationalEscalationDelivery.delivery_status.in_(
                [
                    OperationalDeliveryStatus.pending,
                    OperationalDeliveryStatus.failed,
                ]
            )
        )
        .order_by(OperationalEscalationDelivery.created_at.asc())
        .all()
    )
    due = [
        delivery
        for delivery in deliveries
        if _delivery_due(delivery, timestamp, max_retries=max_retries)
    ][:limit]
    return [dispatch_delivery(db, delivery, now=timestamp) for delivery in due]


def dispatch_delivery(
    db: Session,
    delivery: OperationalEscalationDelivery,
    *,
    now: datetime | None = None,
) -> OperationalEscalationDelivery:
    timestamp = now or datetime.now(UTC)
    event = delivery.event
    if event.status != OperationalEscalationStatus.open:
        return _suppress_delivery(
            db,
            delivery,
            reason=f"event.{event.status}",
            timestamp=timestamp,
        )
    if suppression_reason := _target_suppression_reason(db, delivery):
        return _suppress_delivery(
            db,
            delivery,
            reason=suppression_reason,
            timestamp=timestamp,
        )

    try:
        targets = _delivery_targets(db, delivery)
        if not targets:
            return _fail_delivery(db, delivery, "No delivery target", timestamp)

        results = [
            _send_to_target(db, delivery=delivery, target=target) for target in targets
        ]
        metadata = {
            **(delivery.metadata_ or {}),
            "dispatch_results": results,
            "dispatched_at": timestamp.isoformat(),
        }
        delivery.metadata_ = metadata
        if all(result.get("ok") for result in results):
            delivery.delivery_status = OperationalDeliveryStatus.sent
            delivery.sent_at = timestamp
            delivery.error_message = None
        else:
            errors = [
                str(result.get("error") or result.get("message") or "send_failed")
                for result in results
                if not result.get("ok")
            ]
            return _fail_delivery(
                db,
                delivery,
                "; ".join(errors) or "Delivery failed",
                timestamp,
            )
        db.flush()
        return delivery
    except Exception as exc:
        logger.warning(
            "operational_escalation_delivery_failed delivery_id=%s error=%s",
            delivery.id,
            exc,
        )
        return _fail_delivery(db, delivery, str(exc), timestamp)


def delivery_audit_for_entity(
    db: Session,
    *,
    entity_type: str,
    entity_id: str | UUID,
    recent_limit: int = 5,
) -> dict[str, object]:
    deliveries = (
        db.query(OperationalEscalationDelivery)
        .join(OperationalEscalationEvent)
        .filter(OperationalEscalationEvent.entity_type == entity_type)
        .filter(OperationalEscalationEvent.entity_id == str(entity_id))
        .order_by(OperationalEscalationDelivery.created_at.desc())
        .all()
    )
    counts = {
        OperationalDeliveryStatus.pending: 0,
        OperationalDeliveryStatus.sent: 0,
        OperationalDeliveryStatus.failed: 0,
        OperationalDeliveryStatus.suppressed: 0,
        OperationalDeliveryStatus.acknowledged: 0,
    }
    for delivery in deliveries:
        counts[delivery.delivery_status] = counts.get(delivery.delivery_status, 0) + 1
    return {
        "total": len(deliveries),
        "counts": counts,
        "recent": deliveries[:recent_limit],
    }


def _send_to_target(
    db: Session,
    *,
    delivery: OperationalEscalationDelivery,
    target: DeliveryTarget,
) -> dict[str, Any]:
    channel = delivery.channel
    title, body = _delivery_title_body(delivery)
    if channel == OperationalNotificationChannel.whatsapp:
        from app.services.integrations import whatsapp_capability as whatsapp_connector

        result = whatsapp_connector.send_text_message(
            db,
            recipient=target.address,
            body=body,
            dry_run=False,
            correlation_id=(
                f"operational-escalation:{delivery.id}:attempt:"
                f"{int((delivery.metadata_ or {}).get('retry_count') or 0)}"
            ),
        )
        return {
            "ok": bool(result.get("ok")),
            "channel": channel,
            "recipient": target.address,
            "provider": result.get("provider"),
            "response": result.get("response"),
            "status_code": result.get("status_code"),
        }

    if channel == OperationalNotificationChannel.push:
        return _send_push(db, delivery=delivery, target=target, title=title, body=body)

    if channel == OperationalNotificationChannel.nextcloud_talk:
        return _send_nextcloud_talk(db, delivery=delivery, message=body)

    adapter_channel = (
        "websocket" if channel == OperationalNotificationChannel.web else channel
    )
    from app.services.notification_adapter import send_notification

    notification_result = send_notification(
        adapter_channel,
        target.address,
        body,
        title=title,
        subject=title,
        metadata=_delivery_metadata(delivery),
        idempotency_key=delivery.dedup_key,
    )
    return {
        "ok": bool(notification_result.success),
        "channel": channel,
        "recipient": target.address,
        "message": notification_result.message,
        "status": notification_result.status.value,
        "error": notification_result.error,
    }


def _send_push(
    db: Session,
    *,
    delivery: OperationalEscalationDelivery,
    target: DeliveryTarget,
    title: str,
    body: str,
) -> dict[str, Any]:
    from app.services import push

    if target.recipient_type == OperationalParticipantType.subscriber:
        ok = push.send_push(
            db,
            target.address,
            title,
            body,
            data=_delivery_metadata(delivery),
            notification_id=str(delivery.id),
        )
        return {
            "ok": ok,
            "channel": OperationalNotificationChannel.push,
            "recipient": target.address,
        }
    if target.recipient_type == OperationalParticipantType.person:
        ok = push.send_push_to_system_user(
            db,
            target.address,
            title,
            body,
            data=_delivery_metadata(delivery),
            notification_id=str(delivery.id),
        )
        return {
            "ok": ok,
            "channel": OperationalNotificationChannel.push,
            "recipient": target.address,
        }
    return {
        "ok": False,
        "channel": OperationalNotificationChannel.push,
        "recipient": target.address,
        "error": "Push requires subscriber or person recipient",
    }


def _send_nextcloud_talk(
    db: Session,
    *,
    delivery: OperationalEscalationDelivery,
    message: str,
) -> dict[str, Any]:
    from app.services.nextcloud_talk import resolve_talk_client

    metadata = _delivery_metadata(delivery)
    room_token = (
        delivery.recipient_address
        or metadata.get("room_token")
        or _entity_room_token(db, delivery.event)
    )
    if not room_token:
        return {
            "ok": False,
            "channel": OperationalNotificationChannel.nextcloud_talk,
            "error": "No Nextcloud Talk room token",
        }
    client = resolve_talk_client(
        db,
        base_url=metadata.get("nextcloud_base_url"),
        username=metadata.get("nextcloud_username"),
        app_password=metadata.get("nextcloud_app_password"),
        timeout_sec=metadata.get("nextcloud_timeout_sec"),
        connector_config_id=metadata.get("nextcloud_connector_config_id"),
    )
    response = client.post_message(str(room_token), message)
    return {
        "ok": True,
        "channel": OperationalNotificationChannel.nextcloud_talk,
        "recipient": str(room_token),
        "response": response,
    }


def _delivery_targets(
    db: Session,
    delivery: OperationalEscalationDelivery,
) -> list[DeliveryTarget]:
    if delivery.recipient_address:
        return [
            DeliveryTarget(
                recipient_type=delivery.recipient_type,
                recipient_id=delivery.recipient_id,
                address=delivery.recipient_address,
            )
        ]

    if delivery.recipient_type == OperationalParticipantType.team:
        return _team_targets(db, delivery)
    if delivery.recipient_type == OperationalParticipantType.person:
        return _person_target(db, delivery)
    if delivery.recipient_type == OperationalParticipantType.subscriber:
        return _subscriber_target(db, delivery)
    if delivery.recipient_type == OperationalParticipantType.reseller:
        return _reseller_target(db, delivery)
    if delivery.recipient_type == OperationalParticipantType.external:
        address = _delivery_metadata(delivery).get("recipient_address")
        return (
            [
                DeliveryTarget(
                    recipient_type=delivery.recipient_type,
                    recipient_id=delivery.recipient_id,
                    address=str(address),
                )
            ]
            if address
            else []
        )
    if delivery.recipient_type == OperationalParticipantType.duty_role:
        return _duty_role_targets(delivery)
    return []


def _team_targets(
    db: Session,
    delivery: OperationalEscalationDelivery,
) -> list[DeliveryTarget]:
    if not delivery.recipient_id:
        return []
    members = (
        db.query(SystemUser)
        .join(
            ServiceTeamMember,
            ServiceTeamMember.person_id == SystemUser.person_party_id,
        )
        .filter(ServiceTeamMember.team_id == _uuid(delivery.recipient_id))
        .filter(ServiceTeamMember.is_active.is_(True))
        .filter(SystemUser.is_active.is_(True))
        .all()
    )
    targets: list[DeliveryTarget] = []
    for member in members:
        targets.extend(
            _target_for_system_user(
                db,
                person_id=str(member.id),
                channel=delivery.channel,
            )
        )
    return targets


def _person_target(
    db: Session,
    delivery: OperationalEscalationDelivery,
) -> list[DeliveryTarget]:
    if not delivery.recipient_id:
        return []
    return _target_for_system_user(
        db,
        person_id=delivery.recipient_id,
        channel=delivery.channel,
    )


def _target_for_system_user(
    db: Session,
    *,
    person_id: str,
    channel: str,
) -> list[DeliveryTarget]:
    user = db.get(SystemUser, _uuid(person_id))
    if user is None or not user.is_active:
        return []
    if channel == OperationalNotificationChannel.email and user.email:
        address = user.email
    elif (
        channel
        in {
            OperationalNotificationChannel.sms,
            OperationalNotificationChannel.whatsapp,
        }
        and user.phone
    ):
        address = user.phone
    elif channel in {
        OperationalNotificationChannel.web,
        OperationalNotificationChannel.push,
    }:
        address = str(user.id)
    else:
        return []
    return [
        DeliveryTarget(
            recipient_type=OperationalParticipantType.person,
            recipient_id=str(user.id),
            address=address,
        )
    ]


def _subscriber_target(
    db: Session,
    delivery: OperationalEscalationDelivery,
) -> list[DeliveryTarget]:
    if not delivery.recipient_id:
        return []
    subscriber = db.get(Subscriber, _uuid(delivery.recipient_id))
    if subscriber is None:
        return []
    if delivery.channel == OperationalNotificationChannel.email and subscriber.email:
        address = subscriber.email
    elif (
        delivery.channel
        in {
            OperationalNotificationChannel.sms,
            OperationalNotificationChannel.whatsapp,
        }
        and subscriber.phone
    ):
        address = subscriber.phone
    elif delivery.channel in {
        OperationalNotificationChannel.web,
        OperationalNotificationChannel.push,
    }:
        address = str(subscriber.id)
    else:
        return []
    return [
        DeliveryTarget(
            recipient_type=OperationalParticipantType.subscriber,
            recipient_id=str(subscriber.id),
            address=address,
        )
    ]


def _reseller_target(
    db: Session,
    delivery: OperationalEscalationDelivery,
) -> list[DeliveryTarget]:
    if not delivery.recipient_id:
        return []
    reseller = db.get(Reseller, _uuid(delivery.recipient_id))
    if reseller is None:
        return []
    targets: list[DeliveryTarget] = []
    if (
        delivery.channel == OperationalNotificationChannel.email
        and reseller.contact_email
    ):
        targets.append(
            DeliveryTarget(
                recipient_type=OperationalParticipantType.reseller,
                recipient_id=str(reseller.id),
                address=reseller.contact_email,
            )
        )
    elif (
        delivery.channel
        in {
            OperationalNotificationChannel.sms,
            OperationalNotificationChannel.whatsapp,
        }
        and reseller.contact_phone
    ):
        targets.append(
            DeliveryTarget(
                recipient_type=OperationalParticipantType.reseller,
                recipient_id=str(reseller.id),
                address=reseller.contact_phone,
            )
        )
    targets.extend(
        _reseller_user_targets(db, reseller_id=reseller.id, delivery=delivery)
    )
    return _dedupe_targets(targets)


def _reseller_user_targets(
    db: Session,
    *,
    reseller_id: UUID,
    delivery: OperationalEscalationDelivery,
) -> list[DeliveryTarget]:
    users = (
        db.query(ResellerUser)
        .filter(ResellerUser.reseller_id == reseller_id)
        .filter(ResellerUser.is_active.is_(True))
        .order_by(ResellerUser.created_at.asc())
        .all()
    )
    targets: list[DeliveryTarget] = []
    for user in users:
        address: str | None = None
        if delivery.channel == OperationalNotificationChannel.email:
            address = user.email
            if not address and user.subscriber_id:
                subscriber = db.get(Subscriber, user.subscriber_id)
                if subscriber is not None and _subscriber_can_receive(
                    subscriber, delivery
                ):
                    address = subscriber.email
        elif (
            delivery.channel
            in {
                OperationalNotificationChannel.sms,
                OperationalNotificationChannel.whatsapp,
            }
            and user.subscriber_id
        ):
            subscriber = db.get(Subscriber, user.subscriber_id)
            if subscriber is not None and _subscriber_can_receive(subscriber, delivery):
                address = subscriber.phone
        if not address:
            continue
        targets.append(
            DeliveryTarget(
                recipient_type=OperationalParticipantType.reseller,
                recipient_id=f"reseller_user:{user.id}",
                address=address,
            )
        )
    return targets


def _dedupe_targets(targets: list[DeliveryTarget]) -> list[DeliveryTarget]:
    deduped: list[DeliveryTarget] = []
    seen: set[str] = set()
    for target in targets:
        key = target.address.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(target)
    return deduped


def _duty_role_targets(delivery: OperationalEscalationDelivery) -> list[DeliveryTarget]:
    role_channels = _delivery_metadata(delivery).get("duty_role_channels")
    if not isinstance(role_channels, dict):
        return []
    channel_targets = role_channels.get(delivery.channel)
    if isinstance(channel_targets, str):
        channel_targets = [channel_targets]
    if not isinstance(channel_targets, list):
        return []
    return [
        DeliveryTarget(
            recipient_type=OperationalParticipantType.duty_role,
            recipient_id=delivery.recipient_id,
            address=str(address),
        )
        for address in channel_targets
        if address
    ]


def _entity_room_token(db: Session, event: OperationalEscalationEvent) -> str | None:
    link = (
        db.query(OperationalRoomLink)
        .filter(OperationalRoomLink.entity_type == event.entity_type)
        .filter(OperationalRoomLink.entity_id == event.entity_id)
        .filter(OperationalRoomLink.provider == OperationalRoomProvider.nextcloud_talk)
        .filter(OperationalRoomLink.is_active.is_(True))
        .order_by(OperationalRoomLink.created_at.desc())
        .first()
    )
    if link is None:
        return None
    metadata = link.metadata_ or {}
    return str(metadata.get("room_token") or link.room_id or "")


def _delivery_title_body(delivery: OperationalEscalationDelivery) -> tuple[str, str]:
    metadata = _delivery_metadata(delivery)
    title = str(
        metadata.get("title")
        or metadata.get("subject")
        or f"Operational escalation: {delivery.event.entity_type}"
    )
    body = str(
        metadata.get("body") or metadata.get("message") or _default_body(delivery)
    )
    return title, body


def _default_body(delivery: OperationalEscalationDelivery) -> str:
    event = delivery.event
    return (
        f"Escalation level {event.level}: {event.trigger} "
        f"for {event.entity_type} {event.entity_id}"
    )


def _delivery_metadata(delivery: OperationalEscalationDelivery) -> dict[str, Any]:
    event_metadata = (
        delivery.event.metadata_ if isinstance(delivery.event.metadata_, dict) else {}
    )
    delivery_metadata = (
        delivery.metadata_ if isinstance(delivery.metadata_, dict) else {}
    )
    return {
        **event_metadata,
        **delivery_metadata,
        "delivery_id": str(delivery.id),
        "event_id": str(delivery.event_id),
        "entity_type": delivery.event.entity_type,
        "entity_id": delivery.event.entity_id,
        "trigger": delivery.event.trigger,
        "level": delivery.event.level,
    }


def _target_suppression_reason(
    db: Session,
    delivery: OperationalEscalationDelivery,
) -> str | None:
    if delivery.recipient_type == OperationalParticipantType.subscriber:
        return _subscriber_suppression_reason(db, delivery.recipient_id, delivery)
    if delivery.recipient_type == OperationalParticipantType.reseller:
        return _reseller_suppression_reason(db, delivery.recipient_id)
    return None


def _subscriber_suppression_reason(
    db: Session,
    subscriber_id: str | None,
    delivery: OperationalEscalationDelivery,
) -> str | None:
    if not subscriber_id:
        return None
    subscriber = db.get(Subscriber, _uuid(subscriber_id))
    if subscriber is None:
        return None
    if not _subscriber_can_receive(subscriber, delivery):
        if not subscriber.is_active:
            return "subscriber.inactive"
        status = subscriber.status.value if subscriber.status else "unknown"
        return f"subscriber.status.{status}"
    return None


def _subscriber_can_receive(
    subscriber: Subscriber,
    delivery: OperationalEscalationDelivery,
) -> bool:
    if not subscriber.is_active:
        return False
    category = str(_delivery_metadata(delivery).get("category") or "service")
    return status_allows_notification(subscriber.status, category)


def _reseller_suppression_reason(
    db: Session,
    reseller_id: str | None,
) -> str | None:
    if not reseller_id:
        return None
    reseller = db.get(Reseller, _uuid(reseller_id))
    if reseller is not None and not reseller.is_active:
        return "reseller.inactive"
    return None


def _suppress_delivery(
    db: Session,
    delivery: OperationalEscalationDelivery,
    *,
    reason: str,
    timestamp: datetime,
) -> OperationalEscalationDelivery:
    delivery.delivery_status = OperationalDeliveryStatus.suppressed
    delivery.error_message = reason
    delivery.metadata_ = {
        **(delivery.metadata_ or {}),
        "suppressed_reason": reason,
        "suppressed_at": timestamp.isoformat(),
    }
    db.flush()
    return delivery


def _fail_delivery(
    db: Session,
    delivery: OperationalEscalationDelivery,
    error: str,
    timestamp: datetime,
) -> OperationalEscalationDelivery:
    retry_count = int((delivery.metadata_ or {}).get("retry_count") or 0) + 1
    delivery.delivery_status = OperationalDeliveryStatus.failed
    delivery.error_message = error
    delivery.metadata_ = {
        **(delivery.metadata_ or {}),
        "retry_count": retry_count,
        "failed_at": timestamp.isoformat(),
        "next_retry_at": _next_retry_at(timestamp, retry_count).isoformat(),
    }
    db.flush()
    return delivery


def _delivery_due(
    delivery: OperationalEscalationDelivery,
    now: datetime,
    *,
    max_retries: int,
) -> bool:
    if delivery.event.status != OperationalEscalationStatus.open:
        return True
    if delivery.delivery_status == OperationalDeliveryStatus.pending:
        if delivery.cooldown_until and _coerce_aware(delivery.cooldown_until) > now:
            return False
        return True
    if delivery.delivery_status != OperationalDeliveryStatus.failed:
        return False
    metadata = delivery.metadata_ or {}
    retry_count = int(metadata.get("retry_count") or 0)
    if retry_count >= max_retries:
        return False
    next_retry_at = _parse_datetime(metadata.get("next_retry_at"))
    return next_retry_at is None or next_retry_at <= now


def _next_retry_at(timestamp: datetime, retry_count: int) -> datetime:
    index = min(max(retry_count - 1, 0), len(DEFAULT_RETRY_BACKOFF_SECONDS) - 1)
    return timestamp + timedelta(seconds=DEFAULT_RETRY_BACKOFF_SECONDS[index])


def _parse_datetime(value: object) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return _coerce_aware(value)
    if not isinstance(value, str):
        return None
    try:
        return _coerce_aware(datetime.fromisoformat(value))
    except ValueError:
        return None


def _coerce_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _uuid(value: str | UUID) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))
