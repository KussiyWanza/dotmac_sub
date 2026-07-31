"""Celery tasks for notification delivery."""

import json
import logging
import time
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.models.domain_settings import SettingDomain
from app.models.notification import (
    DeliveryStatus,
    Notification,
    NotificationChannel,
    NotificationDelivery,
    NotificationStatus,
)
from app.services import communication_attachments, communication_eligibility
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
_AT_MOST_ONCE_EVENT_TYPES = frozenset({"service_bulk_message"})


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


def _deliver_notification_queue_stats(db, batch_size: int = 50) -> dict[str, int]:
    now = datetime.now(UTC)
    max_retries = _max_retries(db)
    stuck_threshold = now - timedelta(minutes=_sending_timeout_minutes(db))
    channel_limit = _per_channel_rate_limit(db)

    expired = _expire_stale_notifications(db, now)

    # Query queued, stuck "sending", and retryable failed notifications
    notifications = (
        db.query(Notification)
        .filter(Notification.is_active.is_(True))
        .filter(
            Notification.channel.in_(
                [
                    NotificationChannel.email,
                    NotificationChannel.sms,
                    NotificationChannel.whatsapp,
                    NotificationChannel.facebook_comment,
                    NotificationChannel.instagram_comment,
                    NotificationChannel.push,
                ]
            )
        )
        .filter(
            or_(
                # Queued notifications ready to send
                Notification.status == NotificationStatus.queued,
                # Stuck "sending" notifications (likely crashed during send)
                (
                    (Notification.status == NotificationStatus.sending)
                    & (Notification.updated_at < stuck_threshold)
                ),
                # Failed notifications eligible for retry (under max retries)
                (
                    (Notification.status == NotificationStatus.failed)
                    & (Notification.retry_count < max_retries)
                ),
            )
        )
        .filter((Notification.send_at.is_(None)) | (Notification.send_at <= now))
        .order_by(Notification.created_at.asc())
        .limit(batch_size)
        .all()
    )
    delivered = 0
    retried = 0
    failed = 0
    reclaimed = 0
    suppressed = 0
    stuck_dropped = 0
    rate_limited = 0
    materialization_rejected = 0
    channel_counts: dict[NotificationChannel, int] = {}
    for notification in notifications:
        current_count = channel_counts.get(notification.channel, 0)
        if current_count >= channel_limit:
            rate_limited += 1
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
                whatsapp_payload = None
                configured_template = delivery_metadata.get("whatsapp_template")
                if isinstance(configured_template, dict) and configured_template.get(
                    "name"
                ):
                    whatsapp_payload = configured_template
                if body:
                    try:
                        parsed_body = json.loads(body)
                        if (
                            isinstance(parsed_body, dict)
                            and parsed_body.get("__whatsapp_template__")
                            and whatsapp_payload is None
                        ):
                            whatsapp_payload = parsed_body
                    except json.JSONDecodeError:
                        whatsapp_payload = None
                if whatsapp_payload:
                    result = whatsapp_service.send_template_message(
                        db=db,
                        recipient=notification.recipient,
                        template_name=str(whatsapp_payload.get("name") or ""),
                        language=str(whatsapp_payload.get("language") or "") or None,
                        variables=whatsapp_payload.get("variables") or {},
                        components=whatsapp_payload.get("components") or [],
                        dry_run=False,
                        correlation_id=(
                            f"notification:{notification.id}:"
                            f"attempt:{notification.retry_count}"
                        ),
                    )
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
                else:
                    result = whatsapp_service.send_text_message(
                        db=db,
                        recipient=notification.recipient,
                        body=body,
                        dry_run=False,
                        correlation_id=(
                            f"notification:{notification.id}:"
                            f"attempt:{notification.retry_count}"
                        ),
                    )
                success = bool(result.get("ok"))
                db.add(
                    NotificationDelivery(
                        notification_id=notification.id,
                        provider=str(result.get("provider") or "whatsapp"),
                        provider_message_id=None,
                        status=DeliveryStatus.delivered
                        if success
                        else DeliveryStatus.failed,
                        response_code=str(result.get("status_code") or ""),
                        response_body=str(
                            result.get("response") or result.get("message") or ""
                        )
                        or None,
                    )
                )
                if not success:
                    notification.last_error = str(
                        result.get("response") or "whatsapp_send_failed"
                    )
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
                except ValueError:
                    success = False
                    notification.retry_count = max_retries - 1
                    provider_error = "meta_comment_configuration_rejected"
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
                success = push_service.send_push(
                    db=db,
                    subscriber_id=notification.subscriber_id,
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


@celery_app.task(name="app.tasks.notifications.deliver_notification_queue")
def deliver_notification_queue() -> dict[str, int]:
    """Process queued notifications and retry failed ones."""
    started = time.monotonic()
    with db_session_adapter.session() as session:
        result = _deliver_notification_queue_stats(session)
        record_notification_queue_result(
            session,
            task_name="app.tasks.notifications.deliver_notification_queue",
            result=result,
            duration_seconds=time.monotonic() - started,
        )
        session.commit()
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
