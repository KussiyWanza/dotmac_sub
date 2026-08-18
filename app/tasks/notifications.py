"""Celery tasks for notification delivery."""

import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import func, or_
from sqlalchemy.orm import Query, Session

from app.celery_app import celery_app
from app.models.domain_settings import SettingDomain
from app.models.notification import (
    DeliveryStatus,
    Notification,
    NotificationChannel,
    NotificationDelivery,
    NotificationStatus,
)
from app.services import (
    communication_attachments,
    communication_eligibility,
    team_inbox_media,
    team_inbox_reply_window,
)
from app.services import email as email_service
from app.services import push as push_service
from app.services import sms as sms_service
from app.services.communication_intents import record_delivery_outcome
from app.services.db_session_adapter import db_session_adapter
from app.services.email_template import render_email_bodies
from app.services.ephemeral_communication_actions import (
    EphemeralActionRejected,
    has_ephemeral_action,
    materialize_email,
)
from app.services.integrations import whatsapp_capability as whatsapp_service
from app.services.nextcloud_talk_staff import deliver_due_staff_talk_notifications
from app.services.observability import record_notification_queue_result
from app.services.settings_spec import resolve_value
from app.services.whatsapp_notification_templates import provider_template_from_template

logger = logging.getLogger(__name__)

# Timeout before a "sending" notification is treated as stuck and reclaimed.
# Kept comfortably longer than normal provider latency so a slow-but-live send
# is not reclaimed (and re-sent) out from under us.
SENDING_TIMEOUT_MINUTES = 10
# Maximum delivery retries before marking as permanently failed
MAX_RETRIES = 3
# Default max age before a still-undelivered notification is expired instead
# of sent (guards against draining weeks of stale dunning when the queue
# runner is re-enabled). 0 disables expiry.
DEFAULT_MAX_QUEUE_AGE_HOURS = 72

_DELIVERABLE_CHANNELS = (
    NotificationChannel.email,
    NotificationChannel.sms,
    NotificationChannel.whatsapp,
    NotificationChannel.facebook_messenger,
    NotificationChannel.instagram_dm,
    NotificationChannel.facebook_comment,
    NotificationChannel.instagram_comment,
    NotificationChannel.push,
)


@dataclass(frozen=True, slots=True)
class _ProviderFailure:
    code: str
    message: str
    retryable: bool


@dataclass(frozen=True, slots=True)
class _WhatsAppTemplateDelivery:
    """Normalized provider-template intent at the notification adapter boundary."""

    name: str
    language: str | None
    variables: dict[str, object]
    components: list[dict[str, object]]


def _safe_provider_failure(
    *,
    channel: NotificationChannel,
    status_code: int | None = None,
    error_code: str | None = None,
    detail: Any = None,
) -> _ProviderFailure:
    raw = " ".join(
        str(part or "")
        for part in (channel.value, status_code, error_code, detail)
        if part is not None
    ).lower()
    if (
        "131047" in raw
        or "24 hour" in raw
        or "24-hour" in raw
        or "conversation window" in raw
        or "outside the allowed window" in raw
        or "reply window" in raw
    ):
        return _ProviderFailure(
            code="reply_window_expired",
            message=(
                "The 24-hour reply window has expired. A new free-form reply "
                "cannot be sent until the customer messages again."
            ),
            retryable=False,
        )
    if status_code == 429 or "rate" in raw and "limit" in raw:
        return _ProviderFailure(
            code="provider_rate_limited",
            message="The provider rate-limited this message. It will retry later.",
            retryable=True,
        )
    if status_code in {401, 403} or "auth" in raw or "permission" in raw:
        return _ProviderFailure(
            code="provider_permission_denied",
            message="The provider rejected this message because access is not allowed.",
            retryable=False,
        )
    if "template" in raw:
        return _ProviderFailure(
            code="template_unavailable",
            message="The provider rejected the selected template.",
            retryable=False,
        )
    if status_code is not None and 400 <= status_code < 500:
        return _ProviderFailure(
            code="invalid_provider_message",
            message="The provider rejected this message.",
            retryable=False,
        )
    if (
        status_code is not None
        and status_code >= 500
        or "timeout" in raw
        or "unavailable" in raw
    ):
        return _ProviderFailure(
            code="provider_unavailable",
            message="The provider is temporarily unavailable. It will retry later.",
            retryable=True,
        )
    reference = abs(hash(raw)) % 1_000_000
    return _ProviderFailure(
        code=f"provider_unknown_failure:{reference:06d}",
        message=f"The provider rejected this message. Reference {reference:06d}.",
        retryable=False,
    )


def _optional_status_code(value: object) -> int | None:
    text = str(value or "")
    return int(text) if text.isdigit() else None


def _team_inbox_conversation_id(notification: Notification) -> str:
    metadata = notification.metadata_ or {}
    value = metadata.get("conversation_id")
    return str(value or "").strip()


def _whatsapp_template_delivery(
    value: object,
    *,
    require_marker: bool,
) -> _WhatsAppTemplateDelivery | None:
    if not isinstance(value, dict):
        return None
    if require_marker and not value.get("__whatsapp_template__"):
        return None
    name = str(value.get("name") or "").strip()
    if not name:
        return None
    language = str(value.get("language") or "").strip() or None
    variables = value.get("variables")
    components = value.get("components")
    return _WhatsAppTemplateDelivery(
        name=name,
        language=language,
        variables=variables if isinstance(variables, dict) else {},
        components=(
            [item for item in components if isinstance(item, dict)]
            if isinstance(components, list)
            else []
        ),
    )


def _team_inbox_whatsapp_template(
    notification: Notification, body: str
) -> _WhatsAppTemplateDelivery | None:
    metadata = notification.metadata_ or {}
    configured = _whatsapp_template_delivery(
        metadata.get("whatsapp_template"), require_marker=False
    )
    if configured is not None:
        return configured
    if not body:
        return None
    try:
        parsed_body = json.loads(body)
    except json.JSONDecodeError:
        return None
    return _whatsapp_template_delivery(parsed_body, require_marker=True)


def _preflight_team_inbox_meta_window(
    db: Session,
    *,
    notification: Notification,
    body: str,
) -> _ProviderFailure | None:
    if notification.channel not in {
        NotificationChannel.whatsapp,
        NotificationChannel.facebook_messenger,
        NotificationChannel.instagram_dm,
    }:
        return None
    if notification.channel == NotificationChannel.whatsapp and (
        _team_inbox_whatsapp_template(notification, body) is not None
    ):
        return None
    conversation_id = team_inbox_reply_window.coerce_conversation_id(
        _team_inbox_conversation_id(notification)
    )
    if conversation_id is None:
        return None
    from app.models.team_inbox import InboxConversation

    conversation = db.get(InboxConversation, conversation_id)
    if conversation is None:
        return _ProviderFailure(
            code="invalid_conversation",
            message="The conversation is no longer available.",
            retryable=False,
        )
    decision = team_inbox_reply_window.decide_reply_window(
        db, conversation=conversation
    )
    if not decision.blocks_free_form:
        return None
    return _ProviderFailure(
        code="reply_window_expired",
        message=decision.reason
        or "The 24-hour reply window has expired. A new free-form reply cannot be sent until the customer messages again.",
        retryable=False,
    )


def _metadata_text(source: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = str(source.get(key) or "").strip()
        if value:
            return value
    return ""


def _meta_comment_target_validation_error(
    db: Session,
    *,
    notification: Notification,
    delivery_metadata: dict[str, Any],
    account_id: str,
    comment_id: str,
) -> str | None:
    target_message_id = str(
        delivery_metadata.get("target_inbox_message_id") or ""
    ).strip()
    if not target_message_id:
        return None
    try:
        target_uuid = UUID(target_message_id)
    except ValueError:
        return "meta_comment_target_invalid"

    from app.models.team_inbox import InboxMessage

    target = db.get(InboxMessage, target_uuid)
    if target is None:
        return "meta_comment_target_missing"
    if target.direction != "inbound":
        return "meta_comment_target_not_inbound"
    if target.channel_type != notification.channel.value:
        return "meta_comment_target_channel_mismatch"

    target_metadata = dict(target.metadata_ or {})
    target_comment_id = str(target.external_message_id or "").strip() or _metadata_text(
        target_metadata,
        "provider_comment_id",
        "comment_id",
        "external_comment_id",
    )
    if not target_comment_id or target_comment_id != comment_id:
        return "meta_comment_target_comment_mismatch"

    target_account_id = _metadata_text(
        target_metadata,
        "source_account_id",
        "provider_account_id",
        "provider_account_scope",
        "page_id",
        "instagram_account_id",
        "ig_account_id",
    )
    if target_account_id and target_account_id != account_id:
        return "meta_comment_target_account_mismatch"

    provider_post_id = str(delivery_metadata.get("provider_post_id") or "").strip()
    target_post_id = _metadata_text(target_metadata, "post_id")
    if provider_post_id and target_post_id and provider_post_id != target_post_id:
        return "meta_comment_target_post_mismatch"

    provider_media_id = str(delivery_metadata.get("provider_media_id") or "").strip()
    target_media_id = _metadata_text(target_metadata, "media_id")
    if provider_media_id and target_media_id and provider_media_id != target_media_id:
        return "meta_comment_target_media_mismatch"

    root_provider_comment_id = str(
        delivery_metadata.get("root_provider_comment_id") or ""
    ).strip()
    target_root_id = (
        _metadata_text(target_metadata, "parent_provider_comment_id")
        or target_comment_id
    )
    if root_provider_comment_id and target_root_id != root_provider_comment_id:
        return "meta_comment_target_root_mismatch"
    return None


# Per-channel reclaim policy for notifications stuck in "sending" (the worker
# may have crashed AFTER handing the message to the provider but BEFORE the
# status commit, so re-sending risks a duplicate). We have no provider-side
# idempotency key yet, so the policy is content-driven:
#   - at_most_once: noisy/low-value bulk (a duplicate blast harms sender
#     reputation and the message is disposable) — do NOT re-send a stuck one.
#   - at_least_once: everything else — critical transactional notices
#     (billing/service/account/auth) where silently losing one is worse than a
#     rare duplicate; re-send, but bounded by MAX_RETRIES.
# A duplicate is the lesser evil for criticals; loss is the lesser evil for
# bulk. True exactly-once (provider idempotency keys) is a follow-up.
_AT_MOST_ONCE_CATEGORIES = frozenset({"general"})
_AT_MOST_ONCE_EVENT_TYPES = frozenset(
    {"service_bulk_message", "cabinet_service_notice"}
)


def _reclaim_policy(notification) -> str:
    """Return 'at_most_once' for noisy/bulk notifications, else 'at_least_once'."""
    if (notification.category or "") in _AT_MOST_ONCE_CATEGORIES:
        return "at_most_once"
    if (notification.event_type or "") in _AT_MOST_ONCE_EVENT_TYPES:
        return "at_most_once"
    return "at_least_once"


def _max_queue_age_hours(db) -> int:
    value = resolve_value(
        db, SettingDomain.notification, "notification_max_queue_age_hours"
    )
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return DEFAULT_MAX_QUEUE_AGE_HOURS


def _notification_setting_int(db, key: str, default: int) -> int:
    value = resolve_value(db, SettingDomain.notification, key)
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _max_retries(db) -> int:
    return max(
        1, _notification_setting_int(db, "notification_max_retries", MAX_RETRIES)
    )


def _sending_timeout_minutes(db) -> int:
    return max(
        2,
        _notification_setting_int(
            db,
            "notification_sending_timeout_minutes",
            SENDING_TIMEOUT_MINUTES,
        ),
    )


def _per_channel_rate_limit(db) -> int:
    return max(
        1,
        _notification_setting_int(db, "notification_per_channel_rate_limit", 50),
    )


def _retry_backoff_minutes(db, retry_count: int) -> int:
    value = resolve_value(
        db,
        SettingDomain.notification,
        "notification_retry_backoff_minutes",
    )
    raw_steps = str(value or "1,5,15").split(",")
    steps: list[int] = []
    for raw in raw_steps:
        try:
            steps.append(max(1, int(raw.strip())))
        except ValueError:
            continue
    if not steps:
        steps = [1, 5, 15]
    index = min(max(retry_count - 1, 0), len(steps) - 1)
    return steps[index]


def _expire_stale_notifications(db, now) -> int:
    """Cancel undelivered notifications that have sat in the queue too long."""
    max_age_hours = _max_queue_age_hours(db)
    if max_age_hours <= 0:
        return 0
    cutoff = now - timedelta(hours=max_age_hours)
    expired_notifications = (
        db.query(Notification)
        .filter(Notification.is_active.is_(True))
        .filter(
            Notification.status.in_(
                [
                    NotificationStatus.queued,
                    NotificationStatus.sending,
                    NotificationStatus.failed,
                ]
            )
        )
        .filter(Notification.created_at < cutoff)
        # Deliberately future-scheduled sends are expired only once their
        # send_at is itself past the cutoff.
        .filter((Notification.send_at.is_(None)) | (Notification.send_at < cutoff))
        .all()
    )
    for notification in expired_notifications:
        notification.status = NotificationStatus.canceled
        notification.last_error = "expired_in_queue"
        record_delivery_outcome(db, notification)
    if expired_notifications:
        db.commit()
        logger.info(
            "Expired %d stale notifications older than %dh",
            len(expired_notifications),
            max_age_hours,
        )
    return len(expired_notifications)


def _eligible_notification_query(
    db: Session,
    *,
    now: datetime,
    max_retries: int,
    stuck_threshold: datetime,
) -> Query[Notification]:
    return (
        db.query(Notification)
        .filter(Notification.is_active.is_(True))
        .filter(Notification.channel.in_(_DELIVERABLE_CHANNELS))
        .filter(
            or_(
                Notification.status == NotificationStatus.queued,
                (
                    (Notification.status == NotificationStatus.sending)
                    & (Notification.updated_at < stuck_threshold)
                ),
                (
                    (Notification.status == NotificationStatus.failed)
                    & (Notification.retry_count < max_retries)
                ),
            )
        )
        .filter((Notification.send_at.is_(None)) | (Notification.send_at <= now))
    )


def _empty_delivery_stats(*, expired: int = 0) -> dict[str, int]:
    return {
        "delivered": 0,
        "retried": 0,
        "failed": 0,
        "expired": expired,
        "reclaimed": 0,
        "suppressed": 0,
        "stuck_dropped": 0,
        "rate_limited": 0,
        "materialization_rejected": 0,
        "stale_due": 0,
        "scheduled_queued": 0,
    }


def _deliver_notification_queue_stats(
    db: Session,
    batch_size: int = 50,
    *,
    notification_id: UUID | None = None,
) -> dict[str, int]:
    now = datetime.now(UTC)
    max_retries = _max_retries(db)
    stuck_threshold = now - timedelta(minutes=_sending_timeout_minutes(db))
    channel_limit = _per_channel_rate_limit(db)

    # The periodic sweep owns global expiry. An immediate single-row wake-up
    # must remain bounded to the notification the committed Inbox command
    # returned.
    expired = 0 if notification_id is not None else _expire_stale_notifications(db, now)

    candidate_query = _eligible_notification_query(
        db,
        now=now,
        max_retries=max_retries,
        stuck_threshold=stuck_threshold,
    )
    if notification_id is not None:
        candidate_query = candidate_query.filter(Notification.id == notification_id)
    notification_candidates = (
        candidate_query.with_entities(Notification.id, Notification.channel)
        .order_by(Notification.created_at.asc())
        .limit(batch_size)
        .all()
    )
    stats = _empty_delivery_stats(expired=expired)
    delivered = stats["delivered"]
    retried = stats["retried"]
    failed = stats["failed"]
    reclaimed = stats["reclaimed"]
    suppressed = stats["suppressed"]
    stuck_dropped = stats["stuck_dropped"]
    rate_limited = stats["rate_limited"]
    materialization_rejected = stats["materialization_rejected"]
    channel_counts: dict[NotificationChannel, int] = {}
    for candidate_id, candidate_channel in notification_candidates:
        current_count = channel_counts.get(candidate_channel, 0)
        if current_count >= channel_limit:
            rate_limited += 1
            continue
        # Candidate discovery is intentionally lock-free. Claim each exact row
        # immediately before delivery so concurrent immediate tasks and the
        # periodic recovery sweep cannot both hand it to a provider.
        notification = (
            _eligible_notification_query(
                db,
                now=now,
                max_retries=max_retries,
                stuck_threshold=stuck_threshold,
            )
            .filter(Notification.id == candidate_id)
            .with_for_update(skip_locked=True)
            .one_or_none()
        )
        if notification is None:
            continue
        channel_counts[notification.channel] = current_count + 1
        # Reclaim handling: a notification still in "sending" was stuck past the
        # timeout — the worker likely crashed mid-send, possibly AFTER the
        # provider was already called. Apply the per-channel reclaim policy
        # before re-handing it to the provider.
        if notification.status == NotificationStatus.sending:
            notification.retry_count = (notification.retry_count or 0) + 1
            if _reclaim_policy(notification) == "at_most_once":
                # No provider-side dedupe for bulk; a duplicate blast is worse
                # than dropping a disposable message — do not re-send.
                notification.status = NotificationStatus.failed
                notification.last_error = "stuck_sending_not_resent (at-most-once)"
                stuck_dropped += 1
                record_delivery_outcome(db, notification)
                db.commit()
                continue
            if notification.retry_count > max_retries:
                notification.status = NotificationStatus.failed
                notification.last_error = "stuck_sending_reclaim_exhausted"
                failed += 1
                logger.warning(
                    "Notification %s reclaim exhausted after %d attempts; giving up",
                    notification.id,
                    notification.retry_count,
                )
                record_delivery_outcome(db, notification)
                db.commit()
                continue
            reclaimed += 1
            logger.info(
                "Reclaiming stuck-sending notification %s "
                "(at-least-once, attempt %d/%d)",
                notification.id,
                notification.retry_count,
                max_retries,
            )
        # The consent gate. This is the ONLY place all four transports are
        # called, so it is the only place the check is guaranteed to run --
        # putting it in each caller means the one that forgets is the one that
        # mails an unsubscribed customer.
        #
        # A marketing suppression stops marketing and nothing else: an
        # unsubscribe must never stop an invoice. `may_send` owns that rule.
        if not communication_eligibility.may_send(
            db,
            channel=notification.channel,
            address=notification.recipient,
            category=notification.category,
        ):
            notification.status = NotificationStatus.canceled
            notification.last_error = "suppressed"
            suppressed += 1
            logger.info(
                "Notification %s suppressed (channel=%s category=%s)",
                notification.id,
                notification.channel.value,
                notification.category,
            )
            record_delivery_outcome(db, notification)
            db.commit()
            continue

        # Update status before sending
        notification.status = NotificationStatus.sending
        record_delivery_outcome(db, notification)
        db.commit()

        subject = notification.subject or "Notification"
        body = notification.body or ""
        delivery_metadata = dict(notification.metadata_ or {})
        raw_inbox_attachment_ids = delivery_metadata.get("inbox_attachment_ids")
        inbox_attachment_ids = (
            [str(value) for value in raw_inbox_attachment_ids if isinstance(value, str)]
            if isinstance(raw_inbox_attachment_ids, list)
            else []
        )
        ephemeral_delivery = has_ephemeral_action(notification)
        try:
            if notification.channel == NotificationChannel.email:
                sender_key: str | None
                activity: str
                body_html: str
                body_text: str | None
                if ephemeral_delivery:
                    rendered = materialize_email(db, notification)
                    subject = rendered.subject
                    body_html = rendered.body_html
                    body_text = rendered.body_text
                    sender_key = rendered.sender_key
                    activity = rendered.activity
                else:
                    # Queue bodies are usually plain text — wrap them in the
                    # branded template and keep the text as the text/plain part.
                    resolved_brand = None
                    if notification.subscriber_id:
                        from app.services.brand_profiles import resolve_brand

                        resolved_brand = resolve_brand(
                            db, subscriber_id=notification.subscriber_id
                        ).to_dict()
                    configured_html = delivery_metadata.get("body_html")
                    configured_text = delivery_metadata.get("body_text")
                    if isinstance(configured_html, str) and configured_html.strip():
                        body_html = configured_html
                        body_text = (
                            configured_text
                            if isinstance(configured_text, str)
                            else body
                        )
                    else:
                        body_html, body_text = render_email_bodies(
                            body, subject=subject, brand=resolved_brand
                        )
                    sender_key = str(delivery_metadata.get("sender_key") or "") or None
                    activity = str(
                        delivery_metadata.get("activity") or "notification_queue"
                    )
                resolved_attachments = (
                    communication_attachments.resolve_email_attachments(
                        db, notification
                    )
                    + team_inbox_media.resolve_delivery_attachments(
                        db, tuple(inbox_attachment_ids)
                    )
                )
                success = email_service.send_email(
                    db=db,
                    to_email=notification.recipient,
                    subject=subject,
                    body_html=body_html,
                    body_text=body_text,
                    sender_key=sender_key,
                    track=False,
                    activity=activity,
                    notification_id=str(notification.id),
                    sensitive_content=ephemeral_delivery,
                    cc_addresses=(
                        [
                            str(value)
                            for value in delivery_metadata.get("cc", ())
                            if isinstance(value, str)
                        ]
                        if isinstance(delivery_metadata.get("cc"), list)
                        else []
                    ),
                    bcc_addresses=(
                        [
                            str(value)
                            for value in delivery_metadata.get("bcc", ())
                            if isinstance(value, str)
                        ]
                        if isinstance(delivery_metadata.get("bcc"), list)
                        else []
                    ),
                    attachments=tuple(
                        email_service.EmailAttachment(
                            filename=item.filename,
                            content_type=item.content_type,
                            content=item.content,
                        )
                        for item in resolved_attachments
                    ),
                )
            elif notification.channel == NotificationChannel.sms:
                success = sms_service.send_sms(
                    db=db,
                    to_phone=notification.recipient,
                    body=body,
                    track=False,
                    notification_id=str(notification.id),
                )
            elif notification.channel == NotificationChannel.whatsapp:
                resolved_inbox_attachments = (
                    team_inbox_media.resolve_delivery_attachments(
                        db, tuple(inbox_attachment_ids)
                    )
                    if inbox_attachment_ids
                    else ()
                )
                whatsapp_payload = _team_inbox_whatsapp_template(notification, body)
                provider_messages: list[str] = []
                preflight_failure = _preflight_team_inbox_meta_window(
                    db, notification=notification, body=body
                )
                if preflight_failure is not None:
                    notification.retry_count = max_retries - 1
                    result = {
                        "ok": False,
                        "provider": "whatsapp",
                        "error_code": preflight_failure.code,
                        "response": preflight_failure.message,
                    }
                elif whatsapp_payload:
                    result = whatsapp_service.send_template_message(
                        db=db,
                        recipient=notification.recipient,
                        template_name=whatsapp_payload.name,
                        language=whatsapp_payload.language,
                        variables=whatsapp_payload.variables,
                        components=whatsapp_payload.components,
                        dry_run=False,
                        correlation_id=(
                            f"notification:{notification.id}:"
                            f"attempt:{notification.retry_count}"
                        ),
                    )
                    if result.get("provider_message_id"):
                        provider_messages.append(str(result["provider_message_id"]))
                elif notification.template:
                    provider_template = provider_template_from_template(
                        notification.template
                    )
                    result = whatsapp_service.send_template_message(
                        db=db,
                        recipient=notification.recipient,
                        template_name=str(
                            (provider_template or {}).get("name")
                            or notification.template.code
                        ),
                        language=str((provider_template or {}).get("language") or "")
                        or None,
                        variables=(provider_template or {}).get("variables") or {},
                        dry_run=False,
                        correlation_id=(
                            f"notification:{notification.id}:"
                            f"attempt:{notification.retry_count}"
                        ),
                    )
                    if result.get("provider_message_id"):
                        provider_messages.append(str(result["provider_message_id"]))
                else:
                    result = {"ok": True, "provider": "whatsapp"}
                    if body and not resolved_inbox_attachments:
                        result = whatsapp_service.send_text_message(
                            db=db,
                            recipient=notification.recipient,
                            body=body,
                            dry_run=False,
                            correlation_id=(
                                f"notification:{notification.id}:"
                                f"attempt:{notification.retry_count}:text"
                            ),
                        )
                        if result.get("provider_message_id"):
                            provider_messages.append(str(result["provider_message_id"]))
                    if result.get("ok"):
                        for index, attachment in enumerate(
                            resolved_inbox_attachments, start=1
                        ):
                            result = whatsapp_service.send_media_message(
                                db=db,
                                recipient=notification.recipient,
                                media_type=attachment.asset_type,
                                content=attachment.content,
                                content_type=attachment.content_type,
                                filename=attachment.filename,
                                caption=body if index == 1 else None,
                                dry_run=False,
                                correlation_id=(
                                    f"notification:{notification.id}:"
                                    f"attempt:{notification.retry_count}:media:{index}"
                                ),
                            )
                            if result.get("provider_message_id"):
                                provider_messages.append(
                                    str(result["provider_message_id"])
                                )
                            if not result.get("ok"):
                                break
                success = bool(result.get("ok"))
                provider_failure = (
                    None
                    if success
                    else _safe_provider_failure(
                        channel=notification.channel,
                        status_code=(_optional_status_code(result.get("status_code"))),
                        error_code=str(result.get("error_code") or ""),
                        detail=result.get("response") or result.get("message"),
                    )
                )
                if provider_failure is not None and not provider_failure.retryable:
                    notification.retry_count = max_retries - 1
                db.add(
                    NotificationDelivery(
                        notification_id=notification.id,
                        provider=str(result.get("provider") or "whatsapp"),
                        provider_message_id=provider_messages[-1]
                        if provider_messages
                        else None,
                        status=DeliveryStatus.delivered
                        if success
                        else DeliveryStatus.failed,
                        response_code=(
                            "accepted"
                            if success
                            else (
                                provider_failure.code
                                if provider_failure is not None
                                else "provider_failed"
                            )
                        ),
                        response_body=(
                            "WhatsApp message accepted"
                            if success
                            else (
                                provider_failure.message
                                if provider_failure is not None
                                else "WhatsApp message failed"
                            )
                        ),
                    )
                )
                if not success:
                    notification.last_error = (
                        provider_failure.code
                        if provider_failure is not None
                        else "whatsapp_send_failed"
                    )
                elif provider_messages:
                    from app.models.team_inbox import InboxMessage

                    message = (
                        db.query(InboxMessage)
                        .filter(InboxMessage.notification_id == notification.id)
                        .one_or_none()
                    )
                    if message is not None:
                        message.external_message_id = provider_messages[-1]
                        message_metadata = dict(message.metadata_ or {})
                        message_metadata["provider_message_ids"] = provider_messages
                        message_metadata["provider_message_id"] = provider_messages[-1]
                        message.metadata_ = message_metadata
            elif notification.channel in {
                NotificationChannel.facebook_messenger,
                NotificationChannel.instagram_dm,
            }:
                from app.models.team_inbox import InboxMessage
                from app.services.integrations import meta_social_capability
                from app.services.integrations.meta_social_contracts import (
                    MetaDirectMessageCommand,
                    MetaSocialChannel,
                )

                account_id = str(
                    delivery_metadata.get("provider_account_id") or ""
                ).strip()
                provider_message_id = ""
                provider_error = "meta_direct_message_failed"
                meta_provider_failure: _ProviderFailure | None = None
                try:
                    preflight_failure = _preflight_team_inbox_meta_window(
                        db, notification=notification, body=body
                    )
                    if preflight_failure is not None:
                        meta_provider_failure = preflight_failure
                        notification.retry_count = max_retries - 1
                        raise ValueError(preflight_failure.code)
                    if not account_id or not notification.recipient:
                        meta_provider_failure = _safe_provider_failure(
                            channel=notification.channel,
                            error_code="meta_direct_message_context_missing",
                        )
                        raise ValueError("meta_direct_message_context_missing")
                    outcome = meta_social_capability.send_direct_message(
                        db,
                        MetaDirectMessageCommand(
                            channel=(
                                MetaSocialChannel.facebook_messenger
                                if notification.channel
                                == NotificationChannel.facebook_messenger
                                else MetaSocialChannel.instagram_dm
                            ),
                            provider_account_id=account_id,
                            recipient_id=notification.recipient,
                            body=body,
                            correlation_id=(
                                f"notification:{notification.id}:"
                                f"attempt:{notification.retry_count}"
                            ),
                        ),
                    )
                    provider_message_id = outcome.provider_message_id or ""
                    success = outcome.accepted and bool(provider_message_id)
                    if not success:
                        provider_error = (
                            outcome.error_code or "meta_direct_message_not_accepted"
                        )
                        meta_provider_failure = _safe_provider_failure(
                            channel=notification.channel,
                            error_code=provider_error,
                            detail=outcome.operation_status,
                        )
                        if (
                            outcome.operation_status == "rejected"
                            or not meta_provider_failure.retryable
                        ):
                            notification.retry_count = max_retries - 1
                except ValueError:
                    success = False
                    notification.retry_count = max_retries - 1
                    if meta_provider_failure is None:
                        meta_provider_failure = _safe_provider_failure(
                            channel=notification.channel,
                            error_code="meta_direct_message_configuration_rejected",
                        )
                    provider_error = meta_provider_failure.code
                except (httpx.TimeoutException, httpx.NetworkError):
                    success = False
                    meta_provider_failure = _safe_provider_failure(
                        channel=notification.channel,
                        error_code="meta_direct_message_provider_unavailable",
                    )
                    provider_error = meta_provider_failure.code
                except Exception:
                    success = False
                    meta_provider_failure = _safe_provider_failure(
                        channel=notification.channel,
                        error_code="meta_direct_message_provider_failed",
                    )
                    provider_error = meta_provider_failure.code
                notification.last_error = None if success else provider_error
                db.add(
                    NotificationDelivery(
                        notification_id=notification.id,
                        provider="meta",
                        provider_message_id=provider_message_id or None,
                        status=(
                            DeliveryStatus.delivered
                            if success
                            else DeliveryStatus.failed
                        ),
                        response_code="accepted" if success else provider_error,
                        response_body=(
                            "Meta direct message accepted"
                            if success
                            else (
                                meta_provider_failure.message
                                if meta_provider_failure is not None
                                else "Meta direct message failed"
                            )
                        ),
                    )
                )
                if success:
                    message = (
                        db.query(InboxMessage)
                        .filter(InboxMessage.notification_id == notification.id)
                        .one_or_none()
                    )
                    if message is not None:
                        message.external_message_id = provider_message_id
                        message_metadata = dict(message.metadata_ or {})
                        message_metadata["provider_message_id"] = provider_message_id
                        message.metadata_ = message_metadata
            elif notification.channel in {
                NotificationChannel.facebook_comment,
                NotificationChannel.instagram_comment,
            }:
                from app.models.team_inbox import InboxMessage
                from app.services import meta_pages

                account_id = str(
                    delivery_metadata.get("provider_account_id") or ""
                ).strip()
                comment_id = str(
                    delivery_metadata.get("parent_provider_comment_id")
                    or notification.recipient
                    or ""
                ).strip()
                provider_reply_id = ""
                provider_error = "meta_comment_reply_failed"
                try:
                    if not account_id or not comment_id:
                        raise ValueError("meta_comment_context_missing")
                    target_error = _meta_comment_target_validation_error(
                        db,
                        notification=notification,
                        delivery_metadata=delivery_metadata,
                        account_id=account_id,
                        comment_id=comment_id,
                    )
                    if target_error is not None:
                        raise ValueError(target_error)
                    if notification.channel == NotificationChannel.facebook_comment:
                        provider_result = meta_pages.reply_to_comment_sync(
                            db,
                            page_id=account_id,
                            comment_id=comment_id,
                            message=body,
                        )
                    else:
                        provider_result = meta_pages.reply_to_instagram_comment_sync(
                            db,
                            ig_account_id=account_id,
                            comment_id=comment_id,
                            message=body,
                        )
                    provider_reply_id = str(provider_result.get("id") or "").strip()
                    if not provider_reply_id:
                        raise ValueError("meta_reply_id_missing")
                    success = True
                except httpx.HTTPStatusError as exc:
                    success = False
                    status_code = exc.response.status_code
                    if status_code not in {408, 409, 425, 429} and status_code < 500:
                        notification.retry_count = max_retries - 1
                    provider_error = f"meta_comment_http_{status_code}"
                except ValueError as exc:
                    success = False
                    notification.retry_count = max_retries - 1
                    error_code = str(exc)
                    provider_error = (
                        error_code
                        if error_code.startswith("meta_comment_")
                        else "meta_comment_configuration_rejected"
                    )
                except (httpx.TimeoutException, httpx.NetworkError):
                    success = False
                    provider_error = "meta_comment_provider_unavailable"
                except Exception:
                    success = False
                    provider_error = "meta_comment_provider_failed"
                notification.last_error = None if success else provider_error
                db.add(
                    NotificationDelivery(
                        notification_id=notification.id,
                        provider="meta",
                        provider_message_id=provider_reply_id or None,
                        status=(
                            DeliveryStatus.delivered
                            if success
                            else DeliveryStatus.failed
                        ),
                        response_code="accepted" if success else provider_error,
                        response_body=(
                            "Meta comment reply accepted"
                            if success
                            else "Meta comment reply failed"
                        ),
                    )
                )
                if success:
                    message = (
                        db.query(InboxMessage)
                        .filter(InboxMessage.notification_id == notification.id)
                        .one_or_none()
                    )
                    if message is not None:
                        message.external_message_id = provider_reply_id
                        metadata = dict(message.metadata_ or {})
                        metadata["provider_message_id"] = provider_reply_id
                        message.metadata_ = metadata
            elif notification.channel == NotificationChannel.push:
                if notification.subscriber_id is None:
                    success = False
                    notification.last_error = "push_missing_subscriber"
                else:
                    success = push_service.send_push(
                        db=db,
                        subscriber_id=str(notification.subscriber_id),
                        title=subject,
                        body=body,
                        notification_id=str(notification.id),
                    )
            else:
                success = False
                notification.last_error = (
                    f"unsupported_channel:{notification.channel.value}"
                )
        except EphemeralActionRejected as exc:
            notification.status = NotificationStatus.canceled
            notification.last_error = f"ephemeral_action_rejected:{exc.code}"
            materialization_rejected += 1
            logger.warning(
                "Ephemeral notification %s rejected during materialization (%s)",
                notification.id,
                exc.code,
            )
            record_delivery_outcome(db, notification)
            db.commit()
            continue
        except Exception as exc:
            success = False
            if ephemeral_delivery:
                # An exception raised after materialization may carry rendered
                # content. Never persist or log its message.
                notification.last_error = "ephemeral_delivery_failed"
                logger.warning(
                    "Ephemeral notification %s failed during delivery",
                    notification.id,
                )
            else:
                notification.last_error = str(exc)

        if success:
            notification.status = NotificationStatus.delivered
            notification.sent_at = datetime.now(UTC)
            notification.last_error = None
            delivered += 1
        else:
            notification.retry_count = (notification.retry_count or 0) + 1
            if notification.retry_count >= max_retries:
                notification.status = NotificationStatus.failed
                notification.send_at = None
                failed += 1
                logger.warning(
                    "Notification %s permanently failed after %d retries: %s",
                    notification.id,
                    notification.retry_count,
                    notification.last_error,
                )
            else:
                # Schedule for retry — set back to failed, will be picked up next run
                notification.status = NotificationStatus.failed
                notification.send_at = now + timedelta(
                    minutes=_retry_backoff_minutes(db, notification.retry_count)
                )
                retried += 1
                logger.info(
                    "Notification %s retry %d/%d scheduled",
                    notification.id,
                    notification.retry_count,
                    max_retries,
                )
            if not notification.last_error:
                notification.last_error = "send_failed"
        record_delivery_outcome(db, notification)
        db.commit()

    stale_due = 0
    scheduled_queued = 0
    if notification_id is None:
        stale_due_minutes = max(
            _notification_setting_int(db, "notification_stale_due_minutes", 5),
            1,
        )
        stale_due = (
            db.query(func.count(Notification.id))
            .filter(Notification.is_active.is_(True))
            .filter(Notification.status == NotificationStatus.queued)
            .filter((Notification.send_at.is_(None)) | (Notification.send_at <= now))
            .filter(
                Notification.created_at < now - timedelta(minutes=stale_due_minutes)
            )
            .scalar()
            or 0
        )
        scheduled_queued = (
            db.query(func.count(Notification.id))
            .filter(Notification.is_active.is_(True))
            .filter(Notification.status == NotificationStatus.queued)
            .filter(Notification.send_at > now)
            .scalar()
            or 0
        )
    return {
        "delivered": delivered,
        "retried": retried,
        "failed": failed,
        "expired": expired,
        "reclaimed": reclaimed,
        "suppressed": suppressed,
        "stuck_dropped": stuck_dropped,
        "rate_limited": rate_limited,
        "materialization_rejected": materialization_rejected,
        "stale_due": int(stale_due),
        "scheduled_queued": int(scheduled_queued),
    }


def _deliver_notification_queue(db, batch_size: int = 50) -> int:
    return _deliver_notification_queue_stats(db, batch_size=batch_size)["delivered"]


def deliver_inbound_smtp_health_probe(
    db: Session,
    *,
    recipient: str,
    message_id: str,
    marker: str,
) -> bool:
    """Deliver one fixed operational probe through the canonical email path."""
    if not communication_eligibility.may_send(
        db,
        channel=NotificationChannel.email,
        address=recipient,
        category="observability",
    ):
        logger.error("inbound_smtp_health_probe_suppressed recipient=%s", recipient)
        return False
    return email_service.send_email(
        db=db,
        to_email=recipient,
        subject="[Dotmac probe] Team inbox SMTP delivery",
        body_html=(
            "<p>Synthetic channel-health probe. This message verifies "
            "canonical outbound SMTP and inbound team-inbox delivery.</p>"
        ),
        body_text=(
            "Synthetic channel-health probe. This message verifies canonical "
            "outbound SMTP and inbound team-inbox delivery."
        ),
        track=False,
        activity="observability_smtp_probe",
        headers={"Message-ID": message_id, "X-Dotmac-Probe": marker},
    )


def _record_notification_task_result(
    session: Session,
    *,
    task_name: str,
    result: dict[str, int],
    started: float,
) -> None:
    record_notification_queue_result(
        session,
        task_name=task_name,
        result=result,
        duration_seconds=time.monotonic() - started,
    )
    session.commit()


@celery_app.task(name="app.tasks.notifications.deliver_notification_queue")
def deliver_notification_queue() -> dict[str, int]:
    """Process queued notifications and retry failed ones."""
    started = time.monotonic()
    with db_session_adapter.session() as session:
        batch_size = min(
            max(
                _notification_setting_int(
                    session,
                    "notification_queue_batch_size",
                    50,
                ),
                1,
            ),
            500,
        )
        result = _deliver_notification_queue_stats(session, batch_size=batch_size)
        talk_result = deliver_due_staff_talk_notifications(session)
        result.update(
            {
                "talk_claimed": talk_result.claimed,
                "talk_delivered": talk_result.delivered,
                "talk_retried": talk_result.retried,
                "talk_failed": talk_result.failed,
                "talk_reconciled": talk_result.reconciled,
            }
        )
        _record_notification_task_result(
            session,
            task_name="app.tasks.notifications.deliver_notification_queue",
            result=result,
            started=started,
        )
        logger.info(
            "Notification queue processed: delivered=%d, retried=%d, failed=%d, "
            "expired=%d, rate_limited=%d",
            result["delivered"],
            result["retried"],
            result["failed"],
            result["expired"],
            result["rate_limited"],
        )
        return result


@celery_app.task(name="app.tasks.notifications.deliver_notification")
def deliver_notification(notification_id: str) -> dict[str, int]:
    """Immediately deliver one committed notification outbox row.

    The string is the Celery transport representation. The delivery owner
    validates it into the precise identifier before querying authoritative
    state. A missing, already-claimed, future, or terminal row is a safe no-op;
    the periodic queue runner remains the recovery path.
    """

    try:
        typed_notification_id = UUID(notification_id)
    except (TypeError, ValueError):
        logger.warning("notification_delivery_wakeup_invalid_id")
        return _empty_delivery_stats()

    started = time.monotonic()
    with db_session_adapter.session() as session:
        result = _deliver_notification_queue_stats(
            session,
            batch_size=1,
            notification_id=typed_notification_id,
        )
        _record_notification_task_result(
            session,
            task_name="app.tasks.notifications.deliver_notification",
            result=result,
            started=started,
        )
        return result
