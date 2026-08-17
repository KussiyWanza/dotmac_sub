import logging
import os
import re
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session, selectinload

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.network_monitoring import AlertSeverity, AlertStatus
from app.models.notification import (
    AlertNotificationLog,
    AlertNotificationPolicy,
    AlertNotificationPolicyStep,
    DeliveryStatus,
    Notification,
    NotificationChannel,
    NotificationDelivery,
    NotificationStatus,
    NotificationTemplate,
    OnCallRotation,
    OnCallRotationMember,
)
from app.schemas.notification import (
    AlertNotificationPolicyCreate,
    AlertNotificationPolicyStepCreate,
    AlertNotificationPolicyStepUpdate,
    AlertNotificationPolicyUpdate,
    NotificationBulkCreateRequest,
    NotificationCreate,
    NotificationDeliveryBulkUpdateRequest,
    NotificationDeliveryCreate,
    NotificationDeliveryLatency,
    NotificationDeliveryUpdate,
    NotificationTemplateCreate,
    NotificationTemplateUpdate,
    NotificationUpdate,
    OnCallRotationCreate,
    OnCallRotationMemberCreate,
    OnCallRotationMemberUpdate,
    OnCallRotationUpdate,
)
from app.services.common import (
    apply_ordering,
    apply_pagination,
    validate_enum,
)
from app.services.customer_notification_policy import (
    channel_disabled_in_config,
    has_recent_notification,
    is_notification_enabled_for_subscriber,
    quiet_hours_send_at,
    resolve_notification_category,
    resolve_subscriber_id_for_recipient,
    status_allows_notification_for_subscriber,
)
from app.services.email_template import html_to_text
from app.services.notification_template_conditions import validate_conditions
from app.services.notification_template_renderer import (
    validate_template_activation_text,
    validate_template_text,
)
from app.services.response import ListResponseMixin, list_response
from app.services.session_hooks import run_after_commit

logger = logging.getLogger(__name__)

# Unrendered double-brace template tokens (e.g. "{{amount}}") that leaked into a
# stored/sent notification body — they must never reach the in-app feed.
_LEAKED_TOKEN_RE = re.compile(r"\{\{\s*[a-zA-Z0-9_.]+\s*\}\}")


def _request_immediate_delivery(notification_id: UUID) -> None:
    try:
        from app.tasks.notifications import deliver_notification

        deliver_notification.apply_async(args=[str(notification_id)], retry=False)
    except Exception:
        logger.warning(
            "notification_immediate_delivery_dispatch_failed",
            extra={"notification_id": str(notification_id)},
            exc_info=True,
        )


def _delivery_is_due(notification: Notification) -> bool:
    if notification.send_at is None:
        return True
    return notification.send_at <= datetime.now(UTC)


def _schedule_latency_wakeup(
    db: Session,
    notification: Notification,
    *,
    delivery_latency: NotificationDeliveryLatency,
) -> None:
    if delivery_latency is not NotificationDeliveryLatency.immediate:
        return
    if notification.status is not NotificationStatus.queued:
        return
    if not _delivery_is_due(notification):
        return
    run_after_commit(
        db,
        lambda _callback_db: _request_immediate_delivery(notification.id),
    )


def _clean_feed_body(body: str | None) -> str | None:
    """Sanitise a notification body for the in-app feed: convert email HTML to
    readable text (so an email notification doesn't dump raw ``<!DOCTYPE html>``)
    and drop any unrendered ``{{token}}`` placeholders that leaked into the
    content. Returns None when nothing readable remains — the app then shows
    just the title."""
    if not body:
        return body
    text = html_to_text(body)
    text = _LEAKED_TOKEN_RE.sub("", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text or None


def _severity_rank(severity: AlertSeverity) -> int:
    order = {
        AlertSeverity.info: 0,
        AlertSeverity.warning: 1,
        AlertSeverity.critical: 2,
    }
    return order.get(severity, 0)


def _get_setting_value(db: Session, key: str) -> str | None:
    setting = (
        db.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.notification)
        .filter(DomainSetting.key == key)
        .filter(DomainSetting.is_active.is_(True))
        .first()
    )
    if not setting:
        return None
    if setting.value_text:
        return setting.value_text
    if setting.value_json is not None:
        return str(setting.value_json)
    return None


def _setting_bool(db: Session, key: str, env_key: str, default: bool) -> bool:
    env_value = os.getenv(env_key)
    if env_value is not None and env_value != "":
        return env_value.strip().lower() in {"1", "true", "yes", "on"}
    value = _get_setting_value(db, key)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _setting_int(db: Session, key: str, env_key: str, default: int) -> int:
    env_value = os.getenv(env_key)
    if env_value is not None and env_value != "":
        try:
            return int(env_value)
        except ValueError:
            return default
    value = _get_setting_value(db, key)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _setting_str(
    db: Session, key: str, env_key: str, default: str | None
) -> str | None:
    env_value = os.getenv(env_key)
    if env_value is not None and env_value != "":
        return env_value
    value = _get_setting_value(db, key)
    if value is None:
        return default
    return str(value)


class Templates(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: NotificationTemplateCreate):
        data = payload.model_dump()
        data["conditions"] = validate_conditions(data.get("conditions"))
        if data["is_active"]:
            validate_template_activation_text(
                subject=data.get("subject"),
                body=data["body"],
                code=data["code"],
            )
        else:
            validate_template_text(
                data.get("subject"),
                data["body"],
                code=data["code"],
            )
        template = NotificationTemplate(**data)
        db.add(template)
        db.commit()
        db.refresh(template)
        return template

    @staticmethod
    def get(db: Session, template_id: str):
        template = db.get(NotificationTemplate, template_id)
        if not template:
            raise HTTPException(status_code=404, detail="Template not found")
        return template

    @staticmethod
    def list(
        db: Session,
        channel: str | None,
        is_active: bool | None,
        search: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(NotificationTemplate)
        if channel:
            query = query.filter(
                NotificationTemplate.channel
                == validate_enum(channel, NotificationChannel, "channel")
            )
        if is_active is not None:
            query = query.filter(NotificationTemplate.is_active == is_active)
        if search:
            pattern = f"%{search.strip()}%"
            query = query.filter(
                (NotificationTemplate.name.ilike(pattern))
                | (NotificationTemplate.code.ilike(pattern))
                | (NotificationTemplate.subject.ilike(pattern))
            )
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": NotificationTemplate.created_at,
                "name": NotificationTemplate.name,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def count(
        db: Session,
        channel: str | None = None,
        is_active: bool | None = None,
        search: str | None = None,
    ) -> int:
        """Count templates, optionally filtered by channel/status/search."""
        query = db.query(func.count(NotificationTemplate.id))
        if channel:
            query = query.filter(
                NotificationTemplate.channel
                == validate_enum(channel, NotificationChannel, "channel")
            )
        if is_active is not None:
            query = query.filter(NotificationTemplate.is_active == is_active)
        if search:
            pattern = f"%{search.strip()}%"
            query = query.filter(
                (NotificationTemplate.name.ilike(pattern))
                | (NotificationTemplate.code.ilike(pattern))
                | (NotificationTemplate.subject.ilike(pattern))
            )
        return query.scalar() or 0

    @staticmethod
    def channel_counts(db: Session) -> dict[str, int]:
        """Return per-channel counts of active templates."""
        rows = (
            db.query(NotificationTemplate.channel, func.count(NotificationTemplate.id))
            .filter(NotificationTemplate.is_active.is_(True))
            .group_by(NotificationTemplate.channel)
            .all()
        )
        totals: dict[NotificationChannel, int] = {row[0]: row[1] for row in rows}
        return {
            "email": totals.get(NotificationChannel.email, 0),
            "sms": totals.get(NotificationChannel.sms, 0),
            "push": totals.get(NotificationChannel.push, 0),
            "whatsapp": totals.get(NotificationChannel.whatsapp, 0),
            "webhook": totals.get(NotificationChannel.webhook, 0),
        }

    @staticmethod
    def update(db: Session, template_id: str, payload: NotificationTemplateUpdate):
        template = db.get(NotificationTemplate, template_id)
        if not template:
            raise HTTPException(status_code=404, detail="Template not found")
        changes = payload.model_dump(exclude_unset=True)
        effective_subject = changes.get("subject", template.subject)
        effective_body = changes.get("body", template.body)
        effective_code = changes.get("code", template.code)
        effective_active = changes.get("is_active", template.is_active)
        if effective_active:
            validate_template_activation_text(
                subject=effective_subject,
                body=effective_body,
                code=effective_code,
            )
        else:
            validate_template_text(
                effective_subject,
                effective_body,
                code=effective_code,
            )
        for key, value in changes.items():
            if key == "conditions":
                value = validate_conditions(value)
            setattr(template, key, value)
        db.commit()
        db.refresh(template)
        return template

    @staticmethod
    def delete(db: Session, template_id: str):
        template = db.get(NotificationTemplate, template_id)
        if not template:
            raise HTTPException(status_code=404, detail="Template not found")
        template.is_active = False
        db.commit()


class Notifications(ListResponseMixin):
    @staticmethod
    def queue_customer_notifications_for_policy(
        db: Session,
        *,
        subscriber,
        template_code: str | None,
        event_type: str | None,
        category: str | None,
        default_channels: Iterable[NotificationChannel | str],
        subject: str | None,
        body: str | None,
        commit: bool = True,
    ) -> int:
        """Queue customer notifications after channel-policy resolution.

        Domain code supplies the intent. This service owns channel selection,
        recipient selection, policy gates, and row creation.
        """
        if subscriber is None:
            return 0
        from app.services.communication_intents import (
            CommunicationIntent,
            submit,
        )

        normalized_defaults = tuple(
            channel
            if isinstance(channel, NotificationChannel)
            else NotificationChannel(str(channel))
            for channel in default_channels
        )
        result = submit(
            db,
            CommunicationIntent(
                subscriber_id=subscriber.id,
                event_type=event_type or "customer_notification",
                category=category or "general",
                template_code=template_code,
                subject=subject,
                body=body,
                default_channels=normalized_defaults,
                persist_policy_suppressions=True,
            ),
        )
        if commit:
            db.commit()
        return len(result.queued)

    @staticmethod
    def _apply_customer_queue_policy(
        db: Session,
        data: dict,
        *,
        recipient: str | None,
        requested_status: NotificationStatus,
    ) -> None:
        """Apply shared customer notification gates to a pending notification."""
        channel = data["channel"]
        subscriber_id = data.get("subscriber_id")
        category = str(data.get("category") or "general")
        data["category"] = category
        event_type = data.get("event_type")

        if requested_status != NotificationStatus.queued:
            return

        if channel_disabled_in_config(db, channel):
            data["status"] = NotificationStatus.canceled
            data["last_error"] = "Suppressed by notification channel configuration"
            return

        if subscriber_id is None:
            return

        if not status_allows_notification_for_subscriber(
            db,
            subscriber_id=subscriber_id,
            category=category,
        ):
            data["status"] = NotificationStatus.canceled
            data["last_error"] = "Suppressed by account notification status policy"
            return

        if not is_notification_enabled_for_subscriber(
            db,
            subscriber_id=subscriber_id,
            channel=channel,
            category=category,
            recipient=recipient,
        ):
            data["status"] = NotificationStatus.canceled
            data["last_error"] = "Suppressed by customer notification preferences"
            return

        from app.services.communication_eligibility import suppression_reason

        durable_suppression = suppression_reason(
            db,
            channel=channel,
            category=category,
            address=recipient,
        )
        if durable_suppression:
            data["status"] = NotificationStatus.canceled
            data["last_error"] = (
                "Suppressed by communication ledger: " + durable_suppression
            )
            return

        if has_recent_notification(
            db,
            subscriber_id=subscriber_id,
            channel=channel,
            event_type=event_type,
            category=category,
            recipient=recipient,
        ):
            data["status"] = NotificationStatus.canceled
            data["last_error"] = "Suppressed duplicate customer notification"
            return

        if requested_status == NotificationStatus.queued and not data.get("send_at"):
            data["send_at"] = quiet_hours_send_at(db)

    @staticmethod
    def _queue_internal(
        db: Session,
        payload: NotificationCreate,
        *,
        apply_customer_policy: bool,
        resolve_customer_identity: bool,
        persist_policy_suppression: bool,
    ) -> Notification | None:
        """Queue a notification without committing the caller's transaction."""
        if payload.template_id:
            template = db.get(NotificationTemplate, payload.template_id)
            if not template:
                raise HTTPException(status_code=404, detail="Template not found")
        data = payload.model_dump()
        delivery_latency = NotificationDeliveryLatency(data.pop("delivery_latency"))
        subscriber_id = data.get("subscriber_id")
        if resolve_customer_identity:
            subscriber_id = subscriber_id or resolve_subscriber_id_for_recipient(
                db,
                data.get("recipient"),
            )
        data["subscriber_id"] = subscriber_id
        category = data.get("category") or resolve_notification_category(
            data.get("event_type")
        )
        data["category"] = category
        metadata = dict(data.get("metadata_") or {})
        metadata["delivery_latency"] = delivery_latency.value
        data["metadata_"] = metadata
        requested_status = data["status"]
        if apply_customer_policy:
            Notifications._apply_customer_queue_policy(
                db,
                data,
                recipient=data.get("recipient"),
                requested_status=requested_status,
            )
        if (
            apply_customer_policy
            and not persist_policy_suppression
            and requested_status == NotificationStatus.queued
            and data["status"] == NotificationStatus.canceled
        ):
            return None
        notification = Notification(**data)
        db.add(notification)
        db.flush()
        _schedule_latency_wakeup(
            db,
            notification,
            delivery_latency=delivery_latency,
        )
        return notification

    @staticmethod
    def _queue_customer_delivery(
        db: Session, payload: NotificationCreate
    ) -> Notification:
        notification = Notifications._queue_internal(
            db,
            payload,
            apply_customer_policy=True,
            resolve_customer_identity=True,
            persist_policy_suppression=True,
        )
        if notification is None:  # pragma: no cover - impossible with persistence on.
            raise RuntimeError("notification was unexpectedly suppressed")
        return notification

    @staticmethod
    def _queue_event_delivery(
        db: Session, payload: NotificationCreate
    ) -> Notification | None:
        return Notifications._queue_internal(
            db,
            payload,
            apply_customer_policy=True,
            resolve_customer_identity=True,
            persist_policy_suppression=False,
        )

    @staticmethod
    def queue_customer_notification(
        db: Session, payload: NotificationCreate
    ) -> Notification:
        """Create a durable intent, then queue its customer-facing delivery."""
        if payload.communication_intent_id is not None:
            return Notifications._queue_customer_delivery(db, payload)
        from app.services.communication_intents import CommunicationIntent, submit

        subscriber_id = payload.subscriber_id or resolve_subscriber_id_for_recipient(
            db, payload.recipient
        )
        result = submit(
            db,
            CommunicationIntent(
                subscriber_id=subscriber_id,
                event_type=payload.event_type or "customer_notification",
                category=payload.category or "general",
                subject=payload.subject,
                body=payload.body,
                template_id=payload.template_id,
                channels=(payload.channel,),
                include_reseller=False,
                recipients={payload.channel: payload.recipient},
                metadata=dict(payload.metadata_ or {}),
                send_at=payload.send_at,
                requested_status=payload.status,
                requested_last_error=payload.last_error,
                delivery_latency=payload.delivery_latency,
            ),
        )
        notification = next(
            (
                item
                for item in result.deliveries
                if item.audience_type == "subscriber"
                and item.recipient == payload.recipient
            ),
            None,
        )
        if notification is None:
            raise RuntimeError("customer communication intent produced no delivery")
        return notification

    @staticmethod
    def queue_event_notification(
        db: Session, payload: NotificationCreate
    ) -> Notification | None:
        """Create a durable event intent and drop policy-suppressed deliveries."""
        if payload.communication_intent_id is not None:
            return Notifications._queue_event_delivery(db, payload)
        from app.services.communication_intents import CommunicationIntent, submit

        subscriber_id = payload.subscriber_id or resolve_subscriber_id_for_recipient(
            db, payload.recipient
        )
        result = submit(
            db,
            CommunicationIntent(
                subscriber_id=subscriber_id,
                event_type=payload.event_type or "event_notification",
                category=payload.category or "general",
                subject=payload.subject,
                body=payload.body,
                template_id=payload.template_id,
                channels=(payload.channel,),
                include_reseller=False,
                recipients={payload.channel: payload.recipient},
                persist_policy_suppressions=False,
                metadata=dict(payload.metadata_ or {}),
                send_at=payload.send_at,
                requested_status=payload.status,
                requested_last_error=payload.last_error,
                delivery_latency=payload.delivery_latency,
            ),
        )
        return next(
            (
                item
                for item in result.queued
                if item.audience_type == "subscriber"
                and item.recipient == payload.recipient
            ),
            None,
        )

    @staticmethod
    def queue_internal_notification(
        db: Session, payload: NotificationCreate
    ) -> Notification:
        """Queue a staff/internal notification without customer policy gates."""
        notification = Notifications._queue_internal(
            db,
            payload,
            apply_customer_policy=False,
            resolve_customer_identity=False,
            persist_policy_suppression=True,
        )
        if notification is None:  # pragma: no cover - impossible without policy drop.
            raise RuntimeError("internal notification was unexpectedly suppressed")
        return notification

    @staticmethod
    def record_transport_attempt(
        db: Session,
        *,
        channel: NotificationChannel,
        recipient: str,
        subject: str | None = None,
        body: str | None = None,
        notification_id: str | UUID | None = None,
        commit: bool = False,
    ) -> Notification:
        """Create or update the notification row for an active transport send."""
        notification = (
            db.get(Notification, notification_id) if notification_id else None
        )
        if notification is None:
            notification = Notification(
                channel=channel,
                recipient=recipient,
                subject=subject,
                body=body,
                status=NotificationStatus.sending,
            )
            db.add(notification)
        else:
            notification.channel = channel
            notification.recipient = recipient
            notification.subject = subject
            notification.body = body
            notification.status = NotificationStatus.sending
            notification.last_error = None
        if commit:
            db.commit()
            db.refresh(notification)
        else:
            db.flush()
        return notification

    @staticmethod
    def queue(db: Session, payload: NotificationCreate) -> Notification:
        """Backward-compatible customer notification queue entry point."""
        return Notifications.queue_customer_notification(db, payload)

    @staticmethod
    def queue_or_drop_suppressed(
        db: Session, payload: NotificationCreate
    ) -> Notification | None:
        """Backward-compatible event notification queue entry point."""
        return Notifications.queue_event_notification(db, payload)

    @staticmethod
    def create(db: Session, payload: NotificationCreate):
        notification = Notifications.create_customer_notification(db, payload)
        return notification

    @staticmethod
    def create_customer_notification(
        db: Session, payload: NotificationCreate
    ) -> Notification:
        notification = Notifications.queue_customer_notification(db, payload)
        db.commit()
        db.refresh(notification)
        return notification

    @staticmethod
    def create_internal_notification(
        db: Session, payload: NotificationCreate
    ) -> Notification:
        notification = Notifications.queue_internal_notification(db, payload)
        db.commit()
        db.refresh(notification)
        return notification

    @staticmethod
    def bulk_create(
        db: Session, payload: NotificationBulkCreateRequest
    ) -> list[Notification]:
        if not payload.recipients:
            raise HTTPException(status_code=400, detail="Recipients required")
        template = None
        if payload.template_id:
            template = db.get(NotificationTemplate, payload.template_id)
            if not template:
                raise HTTPException(status_code=404, detail="Template not found")
        notifications: list[Notification] = []
        for recipient in payload.recipients:
            subscriber_id = resolve_subscriber_id_for_recipient(db, recipient)
            category = payload.category or resolve_notification_category(
                payload.event_type
            )
            notification = Notifications.queue_customer_notification(
                db,
                NotificationCreate(
                    template_id=payload.template_id,
                    subscriber_id=subscriber_id,
                    channel=payload.channel,
                    event_type=payload.event_type,
                    category=category,
                    recipient=recipient,
                    subject=payload.subject or (template.subject if template else None),
                    body=payload.body or (template.body if template else None),
                    status=payload.status,
                    send_at=payload.send_at,
                ),
            )
            notifications.append(notification)
        db.commit()
        for notification in notifications:
            db.refresh(notification)
        return notifications

    @staticmethod
    def bulk_create_response(
        db: Session, payload: NotificationBulkCreateRequest
    ) -> dict:
        notifications = Notifications.bulk_create(db, payload)
        return {
            "created": len(notifications),
            "notification_ids": [notification.id for notification in notifications],
        }

    @staticmethod
    def get(db: Session, notification_id: str):
        notification = db.get(Notification, notification_id)
        if not notification:
            raise HTTPException(status_code=404, detail="Notification not found")
        return notification

    @staticmethod
    def list(
        db: Session,
        channel: str | None,
        status: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(Notification)
        if channel:
            query = query.filter(
                Notification.channel
                == validate_enum(channel, NotificationChannel, "channel")
            )
        if status:
            query = query.filter(
                Notification.status
                == validate_enum(status, NotificationStatus, "status")
            )
        if is_active is None:
            query = query.filter(Notification.is_active.is_(True))
        else:
            query = query.filter(Notification.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": Notification.created_at, "status": Notification.status},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def list_for_subscriber(db, subscriber_id, limit, offset):
        """Active notifications addressed to one subscriber, newest first."""
        query = (
            db.query(Notification)
            .filter(Notification.subscriber_id == subscriber_id)
            .filter(Notification.is_active.is_(True))
            .order_by(Notification.created_at.desc())
        )
        return apply_pagination(query, limit, offset).all()

    @classmethod
    def list_response_for_subscriber(cls, db, subscriber_id, limit, offset):
        items = cls.list_for_subscriber(db, subscriber_id, limit, offset)
        # Sanitise bodies for the in-app feed (strip email HTML, drop leaked
        # {{tokens}}). Detach each row first so these display-only edits are
        # never flushed back to the database.
        for n in items:
            db.expunge(n)
            n.body = _clean_feed_body(n.body)
        return list_response(items, limit, offset)

    @staticmethod
    def count(
        db: Session,
        channel: str | None = None,
        status: str | None = None,
    ) -> int:
        """Count active notifications, optionally filtered."""
        query = db.query(func.count(Notification.id)).filter(
            Notification.is_active.is_(True)
        )
        if channel:
            query = query.filter(
                Notification.channel
                == validate_enum(channel, NotificationChannel, "channel")
            )
        if status:
            query = query.filter(
                Notification.status
                == validate_enum(status, NotificationStatus, "status")
            )
        return query.scalar() or 0

    @staticmethod
    def status_counts(db: Session) -> dict[str, int]:
        """Return per-status counts of active notifications."""
        rows = (
            db.query(Notification.status, func.count(Notification.id))
            .filter(Notification.is_active.is_(True))
            .group_by(Notification.status)
            .all()
        )
        totals: dict[NotificationStatus, int] = {row[0]: row[1] for row in rows}
        return {
            "queued": totals.get(NotificationStatus.queued, 0),
            "sending": totals.get(NotificationStatus.sending, 0),
            "delivered": totals.get(NotificationStatus.delivered, 0),
            "failed": totals.get(NotificationStatus.failed, 0),
        }

    @staticmethod
    def update(db: Session, notification_id: str, payload: NotificationUpdate):
        notification = db.get(Notification, notification_id)
        if not notification:
            raise HTTPException(status_code=404, detail="Notification not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(notification, key, value)
        db.commit()
        db.refresh(notification)
        return notification

    @staticmethod
    def delete(db: Session, notification_id: str):
        notification = db.get(Notification, notification_id)
        if not notification:
            raise HTTPException(status_code=404, detail="Notification not found")
        notification.is_active = False
        db.commit()


class Deliveries(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: NotificationDeliveryCreate):
        notification = db.get(Notification, payload.notification_id)
        if not notification:
            raise HTTPException(status_code=404, detail="Notification not found")
        delivery = NotificationDelivery(**payload.model_dump())
        db.add(delivery)
        db.commit()
        db.refresh(delivery)
        return delivery

    @staticmethod
    def get(db: Session, delivery_id: str):
        delivery = db.get(NotificationDelivery, delivery_id)
        if not delivery:
            raise HTTPException(status_code=404, detail="Delivery not found")
        return delivery

    @staticmethod
    def list(
        db: Session,
        notification_id: str | None,
        status: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(NotificationDelivery).options(
            selectinload(NotificationDelivery.notification)
        )
        if notification_id:
            query = query.filter(
                NotificationDelivery.notification_id == notification_id
            )
        if status:
            query = query.filter(
                NotificationDelivery.status
                == validate_enum(status, DeliveryStatus, "status")
            )
        if is_active is None:
            query = query.filter(NotificationDelivery.is_active.is_(True))
        else:
            query = query.filter(NotificationDelivery.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "occurred_at": NotificationDelivery.occurred_at,
                "status": NotificationDelivery.status,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def count(db: Session, status: str | None = None) -> int:
        """Count active deliveries, optionally filtered by status."""
        query = db.query(func.count(NotificationDelivery.id)).filter(
            NotificationDelivery.is_active.is_(True)
        )
        if status:
            query = query.filter(
                NotificationDelivery.status
                == validate_enum(status, DeliveryStatus, "status")
            )
        return query.scalar() or 0

    @staticmethod
    def update(db: Session, delivery_id: str, payload: NotificationDeliveryUpdate):
        delivery = db.get(NotificationDelivery, delivery_id)
        if not delivery:
            raise HTTPException(status_code=404, detail="Delivery not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(delivery, key, value)
        db.commit()
        db.refresh(delivery)
        return delivery

    @staticmethod
    def bulk_update(db: Session, payload: NotificationDeliveryBulkUpdateRequest) -> int:
        if not payload.delivery_ids:
            raise HTTPException(status_code=400, detail="delivery_ids required")
        ids = [delivery_id for delivery_id in payload.delivery_ids]
        deliveries = (
            db.query(NotificationDelivery)
            .filter(NotificationDelivery.id.in_(ids))
            .all()
        )
        if len(deliveries) != len(ids):
            raise HTTPException(
                status_code=404, detail="One or more deliveries not found"
            )
        data = payload.model_dump(exclude={"delivery_ids"}, exclude_unset=True)
        for delivery in deliveries:
            for key, value in data.items():
                setattr(delivery, key, value)
        db.commit()
        return len(deliveries)

    @staticmethod
    def bulk_update_response(
        db: Session, payload: NotificationDeliveryBulkUpdateRequest
    ) -> dict:
        updated = Deliveries.bulk_update(db, payload)
        return {"updated": updated}

    @staticmethod
    def delete(db: Session, delivery_id: str):
        delivery = db.get(NotificationDelivery, delivery_id)
        if not delivery:
            raise HTTPException(status_code=404, detail="Delivery not found")
        delivery.is_active = False
        db.commit()


class AlertNotificationPolicies(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: AlertNotificationPolicyCreate):
        policy = AlertNotificationPolicy(**payload.model_dump())
        db.add(policy)
        db.commit()
        db.refresh(policy)
        return policy

    @staticmethod
    def get(db: Session, policy_id: str):
        policy = db.get(AlertNotificationPolicy, policy_id)
        if not policy:
            raise HTTPException(
                status_code=404, detail="Alert notification policy not found"
            )
        return policy

    @staticmethod
    def list(
        db: Session,
        channel: str | None,
        status: str | None,
        severity_min: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(AlertNotificationPolicy)
        if channel:
            query = query.filter(
                AlertNotificationPolicy.channel
                == validate_enum(channel, NotificationChannel, "channel")
            )
        if status:
            query = query.filter(
                AlertNotificationPolicy.status
                == validate_enum(status, AlertStatus, "status")
            )
        if severity_min:
            query = query.filter(
                AlertNotificationPolicy.severity_min
                == validate_enum(severity_min, AlertSeverity, "severity_min")
            )
        if is_active is None:
            query = query.filter(AlertNotificationPolicy.is_active.is_(True))
        else:
            query = query.filter(AlertNotificationPolicy.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": AlertNotificationPolicy.created_at,
                "name": AlertNotificationPolicy.name,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def count(
        db: Session,
        channel: str | None,
        status: str | None,
        severity_min: str | None,
        is_active: bool | None,
    ) -> int:
        query = db.query(func.count(AlertNotificationPolicy.id))
        if channel:
            query = query.filter(
                AlertNotificationPolicy.channel
                == validate_enum(channel, NotificationChannel, "channel")
            )
        if status:
            query = query.filter(
                AlertNotificationPolicy.status
                == validate_enum(status, AlertStatus, "status")
            )
        if severity_min:
            query = query.filter(
                AlertNotificationPolicy.severity_min
                == validate_enum(severity_min, AlertSeverity, "severity_min")
            )
        if is_active is None:
            query = query.filter(AlertNotificationPolicy.is_active.is_(True))
        else:
            query = query.filter(AlertNotificationPolicy.is_active == is_active)
        return query.scalar() or 0

    @staticmethod
    def update(db: Session, policy_id: str, payload: AlertNotificationPolicyUpdate):
        policy = db.get(AlertNotificationPolicy, policy_id)
        if not policy:
            raise HTTPException(
                status_code=404, detail="Alert notification policy not found"
            )
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(policy, key, value)
        db.commit()
        db.refresh(policy)
        return policy

    @staticmethod
    def delete(db: Session, policy_id: str):
        policy = db.get(AlertNotificationPolicy, policy_id)
        if not policy:
            raise HTTPException(
                status_code=404, detail="Alert notification policy not found"
            )
        policy.is_active = False
        db.commit()

    @staticmethod
    def emit_for_alert(db: Session, alert, status: AlertStatus) -> int:
        enabled = _setting_bool(
            db,
            "alert_notifications_enabled",
            "ALERT_NOTIFICATIONS_ENABLED",
            True,
        )
        if not enabled:
            return 0
        default_channel = _setting_str(
            db,
            "alert_notifications_default_channel",
            "ALERT_NOTIFICATIONS_DEFAULT_CHANNEL",
            "email",
        )
        default_recipient = _setting_str(
            db,
            "alert_notifications_default_recipient",
            "ALERT_NOTIFICATIONS_DEFAULT_RECIPIENT",
            None,
        )
        default_template_id = _setting_str(
            db,
            "alert_notifications_default_template_id",
            "ALERT_NOTIFICATIONS_DEFAULT_TEMPLATE_ID",
            None,
        )
        default_rotation_id = _setting_str(
            db,
            "alert_notifications_default_rotation_id",
            "ALERT_NOTIFICATIONS_DEFAULT_ROTATION_ID",
            None,
        )
        default_delay_minutes = _setting_int(
            db,
            "alert_notifications_default_delay_minutes",
            "ALERT_NOTIFICATIONS_DEFAULT_DELAY_MINUTES",
            0,
        )
        policies = (
            db.query(AlertNotificationPolicy)
            .filter(AlertNotificationPolicy.is_active.is_(True))
            .filter(AlertNotificationPolicy.status == status)
            .all()
        )
        if not policies:
            return 0
        emitted = 0
        for policy in policies:
            if policy.rule_id and policy.rule_id != alert.rule_id:
                continue
            if policy.device_id and policy.device_id != alert.device_id:
                continue
            if policy.interface_id and policy.interface_id != alert.interface_id:
                continue
            if _severity_rank(alert.severity) < _severity_rank(policy.severity_min):
                continue
            steps = (
                db.query(AlertNotificationPolicyStep)
                .filter(AlertNotificationPolicyStep.policy_id == policy.id)
                .filter(AlertNotificationPolicyStep.is_active.is_(True))
                .filter(AlertNotificationPolicyStep.status == status)
                .order_by(AlertNotificationPolicyStep.step_index.asc())
                .all()
            )
            if not steps:
                channel_value = policy.channel or validate_enum(
                    default_channel, NotificationChannel, "channel"
                )
                steps = [
                    AlertNotificationPolicyStep(
                        policy_id=policy.id,
                        step_index=0,
                        delay_minutes=max(default_delay_minutes, 0),
                        channel=channel_value,
                        recipient=policy.recipient or default_recipient,
                        template_id=policy.template_id
                        or (default_template_id if default_template_id else None),
                        rotation_id=default_rotation_id
                        if default_rotation_id
                        else None,
                        severity_min=policy.severity_min,
                        status=policy.status,
                        is_active=True,
                    )
                ]
            for step in steps:
                if _severity_rank(alert.severity) < _severity_rank(step.severity_min):
                    continue
                recipient = step.recipient or default_recipient
                rotation_id = step.rotation_id or default_rotation_id
                if rotation_id:
                    member = (
                        db.query(OnCallRotationMember)
                        .filter(OnCallRotationMember.rotation_id == rotation_id)
                        .filter(OnCallRotationMember.is_active.is_(True))
                        .order_by(
                            OnCallRotationMember.priority.asc(),
                            OnCallRotationMember.last_used_at.asc().nullsfirst(),
                        )
                        .first()
                    )
                    if member:
                        recipient = member.contact
                        member.last_used_at = datetime.now(UTC)
                if not recipient:
                    continue
                subject = f"Alert {alert.severity.value}: {alert.metric_type.value}"
                body = (
                    f"Alert {alert.id} is {status.value}. "
                    f"Metric {alert.metric_type.value} measured {alert.measured_value}."
                )
                template_id = step.template_id or (
                    default_template_id if default_template_id else None
                )
                template_uuid = None
                if isinstance(template_id, UUID):
                    template_uuid = template_id
                elif template_id:
                    try:
                        template_uuid = UUID(str(template_id))
                    except ValueError:
                        template_uuid = None
                if template_uuid:
                    template = db.get(NotificationTemplate, template_uuid)
                    if template:
                        subject = template.subject or subject
                        body = template.body
                send_at = None
                delay_minutes = step.delay_minutes
                if delay_minutes is None:
                    delay_minutes = default_delay_minutes
                if delay_minutes and delay_minutes > 0:
                    send_at = datetime.now(UTC) + timedelta(minutes=delay_minutes)
                notification = Notifications.queue_internal_notification(
                    db,
                    NotificationCreate(
                        template_id=template_uuid,
                        channel=step.channel,
                        recipient=recipient,
                        subject=subject,
                        body=body,
                        send_at=send_at,
                    ),
                )
                log = AlertNotificationLog(
                    alert_id=alert.id,
                    policy_id=policy.id,
                    notification_id=notification.id,
                )
                db.add(log)
                emitted += 1
        return emitted


class AlertNotificationLogs(ListResponseMixin):
    @staticmethod
    def list(
        db: Session,
        alert_id: str | None,
        policy_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(AlertNotificationLog)
        if alert_id:
            query = query.filter(AlertNotificationLog.alert_id == alert_id)
        if policy_id:
            query = query.filter(AlertNotificationLog.policy_id == policy_id)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": AlertNotificationLog.created_at},
        )
        return apply_pagination(query, limit, offset).all()


class AlertNotificationPolicySteps(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: AlertNotificationPolicyStepCreate):
        step = AlertNotificationPolicyStep(**payload.model_dump())
        db.add(step)
        db.commit()
        db.refresh(step)
        return step

    @staticmethod
    def get(db: Session, step_id: str):
        step = db.get(AlertNotificationPolicyStep, step_id)
        if not step:
            raise HTTPException(status_code=404, detail="Alert policy step not found")
        return step

    @staticmethod
    def list(
        db: Session,
        policy_id: str | None,
        status: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(AlertNotificationPolicyStep)
        if policy_id:
            query = query.filter(AlertNotificationPolicyStep.policy_id == policy_id)
        if status:
            query = query.filter(
                AlertNotificationPolicyStep.status
                == validate_enum(status, AlertStatus, "status")
            )
        if is_active is None:
            query = query.filter(AlertNotificationPolicyStep.is_active.is_(True))
        else:
            query = query.filter(AlertNotificationPolicyStep.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": AlertNotificationPolicyStep.created_at,
                "step_index": AlertNotificationPolicyStep.step_index,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, step_id: str, payload: AlertNotificationPolicyStepUpdate):
        step = db.get(AlertNotificationPolicyStep, step_id)
        if not step:
            raise HTTPException(status_code=404, detail="Alert policy step not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(step, key, value)
        db.commit()
        db.refresh(step)
        return step

    @staticmethod
    def delete(db: Session, step_id: str):
        step = db.get(AlertNotificationPolicyStep, step_id)
        if not step:
            raise HTTPException(status_code=404, detail="Alert policy step not found")
        step.is_active = False
        db.commit()


class OnCallRotations(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: OnCallRotationCreate):
        rotation = OnCallRotation(**payload.model_dump())
        db.add(rotation)
        db.commit()
        db.refresh(rotation)
        return rotation

    @staticmethod
    def get(db: Session, rotation_id: str):
        rotation = db.get(OnCallRotation, rotation_id)
        if not rotation:
            raise HTTPException(status_code=404, detail="On-call rotation not found")
        return rotation

    @staticmethod
    def list(
        db: Session,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(OnCallRotation)
        if is_active is None:
            query = query.filter(OnCallRotation.is_active.is_(True))
        else:
            query = query.filter(OnCallRotation.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": OnCallRotation.created_at, "name": OnCallRotation.name},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, rotation_id: str, payload: OnCallRotationUpdate):
        rotation = db.get(OnCallRotation, rotation_id)
        if not rotation:
            raise HTTPException(status_code=404, detail="On-call rotation not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(rotation, key, value)
        db.commit()
        db.refresh(rotation)
        return rotation

    @staticmethod
    def delete(db: Session, rotation_id: str):
        rotation = db.get(OnCallRotation, rotation_id)
        if not rotation:
            raise HTTPException(status_code=404, detail="On-call rotation not found")
        rotation.is_active = False
        db.commit()


class OnCallRotationMembers(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: OnCallRotationMemberCreate):
        member = OnCallRotationMember(**payload.model_dump())
        db.add(member)
        db.commit()
        db.refresh(member)
        return member

    @staticmethod
    def get(db: Session, member_id: str):
        member = db.get(OnCallRotationMember, member_id)
        if not member:
            raise HTTPException(status_code=404, detail="On-call member not found")
        return member

    @staticmethod
    def list(
        db: Session,
        rotation_id: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(OnCallRotationMember)
        if rotation_id:
            query = query.filter(OnCallRotationMember.rotation_id == rotation_id)
        if is_active is None:
            query = query.filter(OnCallRotationMember.is_active.is_(True))
        else:
            query = query.filter(OnCallRotationMember.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": OnCallRotationMember.created_at,
                "priority": OnCallRotationMember.priority,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, member_id: str, payload: OnCallRotationMemberUpdate):
        member = db.get(OnCallRotationMember, member_id)
        if not member:
            raise HTTPException(status_code=404, detail="On-call member not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(member, key, value)
        db.commit()
        db.refresh(member)
        return member

    @staticmethod
    def delete(db: Session, member_id: str):
        member = db.get(OnCallRotationMember, member_id)
        if not member:
            raise HTTPException(status_code=404, detail="On-call member not found")
        member.is_active = False
        db.commit()


templates = Templates()
notifications = Notifications()
deliveries = Deliveries()
alert_notification_policies = AlertNotificationPolicies()
alert_notification_logs = AlertNotificationLogs()
alert_notification_policy_steps = AlertNotificationPolicySteps()
on_call_rotations = OnCallRotations()
on_call_rotation_members = OnCallRotationMembers()
