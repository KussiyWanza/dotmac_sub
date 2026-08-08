"""SMS service with multi-provider support.

Supports:
- Twilio
- Africa's Talking
- Generic HTTP webhook

Operational configuration is settings-owned (`notification` domain, declared
in `settings_spec`): sms_enabled, sms_provider, sms_from_number,
sms_webhook_url, sms_api_timeout_seconds, sms_max_length. Their `env_var`s are
bootstrap inputs, materialised by the seed — never a live override of a stored
row.

Provider credentials are held from boot in `app.config` (SMS_API_KEY,
SMS_API_SECRET) and are not settings (ADR-0009).
"""

import logging
import re
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.models.domain_settings import SettingDomain
from app.models.notification import (
    DeliveryStatus,
    NotificationChannel,
    NotificationDelivery,
    NotificationStatus,
    NotificationTemplate,
)
from app.schemas.notification import NotificationCreate
from app.services import settings_spec
from app.services.customer_identity_normalization import normalize_phone_identifier
from app.services.notification import notifications as notification_records
from app.services.notification_template_renderer import render_template_text

logger = logging.getLogger(__name__)

_UNRESOLVED_TEMPLATE_RE = re.compile(r"\{\{?\s*[a-zA-Z0-9_]+\s*\}?\}")


def _sms_credentials() -> tuple[str, str]:
    """The provider credentials, HELD from boot rather than resolved.

    They used to be read through the same settings lookup as the operational
    values, from `domain_settings` rows nothing could write. A credential is
    not a setting (ADR-0009): `app.config` materialises these once at startup
    and nothing on a read path reaches for them.
    """

    return settings.sms_api_key, settings.sms_api_secret


def _normalize_phone(phone: str) -> str:
    """Normalize phone number to E.164 format."""
    return normalize_phone_identifier(phone) or phone


def _send_via_twilio(
    api_key: str,
    api_secret: str,
    from_number: str,
    to_phone: str,
    body: str,
    timeout: float = 30.0,
) -> tuple[bool, str | None, str | None]:
    """Send SMS via Twilio.

    Returns: (success, message_sid, error_message)
    """
    try:
        # Twilio uses account_sid as api_key and auth_token as api_secret
        url = f"https://api.twilio.com/2010-04-01/Accounts/{api_key}/Messages.json"

        response = httpx.post(
            url,
            auth=(api_key, api_secret),
            data={
                "From": from_number,
                "To": to_phone,
                "Body": body,
            },
            timeout=timeout,
        )

        if response.status_code in (200, 201):
            data = response.json()
            return True, data.get("sid"), None
        else:
            error_data = response.json() if response.content else {}
            error_msg = error_data.get("message", f"HTTP {response.status_code}")
            if response.status_code in (401, 403):
                logger.error(
                    "sms_auth_failed provider=twilio status=%s message=%s",
                    response.status_code,
                    error_msg,
                )
            return False, None, error_msg

    except Exception as exc:
        logger.exception("Twilio SMS failed")
        return False, None, str(exc)


def _send_via_africastalking(
    api_key: str,
    username: str,
    from_number: str | None,
    to_phone: str,
    body: str,
    timeout: float = 30.0,
) -> tuple[bool, str | None, str | None]:
    """Send SMS via Africa's Talking.

    Returns: (success, message_id, error_message)
    """
    try:
        url = "https://api.africastalking.com/version1/messaging"

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "apiKey": api_key,
        }

        data: dict[str, Any] = {
            "username": username,
            "to": to_phone,
            "message": body,
        }
        if from_number:
            data["from"] = from_number

        response = httpx.post(url, headers=headers, data=data, timeout=timeout)

        if response.status_code in (200, 201):
            resp_data = response.json()
            sms_data = resp_data.get("SMSMessageData", {})
            recipients = sms_data.get("Recipients", [])
            if recipients:
                recipient = recipients[0]
                status = recipient.get("status", "")
                if status in ("Success", "Sent"):
                    return True, recipient.get("messageId"), None
                else:
                    return False, None, status
            return False, None, "No recipients in response"
        else:
            if response.status_code in (401, 403):
                logger.error(
                    "sms_auth_failed provider=africastalking status=%s body=%s",
                    response.status_code,
                    response.text,
                )
            return False, None, f"HTTP {response.status_code}"

    except Exception as exc:
        logger.exception("Africa's Talking SMS failed")
        return False, None, str(exc)


def _send_via_webhook(
    webhook_url: str,
    api_key: str | None,
    to_phone: str,
    body: str,
    timeout: float = 30.0,
) -> tuple[bool, str | None, str | None]:
    """Send SMS via generic HTTP webhook.

    Returns: (success, external_id, error_message)
    """
    try:
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        payload = {
            "to": to_phone,
            "message": body,
        }

        response = httpx.post(
            webhook_url, headers=headers, json=payload, timeout=timeout
        )

        if response.status_code in (200, 201, 202):
            try:
                data = response.json()
                external_id = (
                    data.get("message_id") or data.get("id") or data.get("sid")
                )
                return True, external_id, None
            except Exception:
                return True, None, None
        else:
            if response.status_code in (401, 403):
                logger.error(
                    "sms_auth_failed provider=webhook status=%s body=%s",
                    response.status_code,
                    response.text,
                )
            return False, None, f"HTTP {response.status_code}"

    except Exception as exc:
        logger.exception("Webhook SMS failed")
        return False, None, str(exc)


def send_sms(
    db: Session,
    to_phone: str,
    body: str,
    track: bool = True,
    notification_id: str | None = None,
) -> bool:
    """Send an SMS message.

    Args:
        db: Database session
        to_phone: Recipient phone number
        body: Message content
        track: Whether to create notification/delivery records

    Returns:
        True if SMS was sent successfully
    """
    if track and notification_id is None:
        queued = notification_records.create_customer_notification(
            db,
            NotificationCreate(
                channel=NotificationChannel.sms,
                recipient=_normalize_phone(to_phone),
                body=body,
                event_type="direct.sms",
                category="general",
                metadata_={"source": "sms_service"},
            ),
        )
        return queued.status == NotificationStatus.queued

    # Fail closed. This defaulted to "true", so a deployment that had never
    # configured SMS still presented the channel as live and queued sends into
    # a provider that did not exist — production accumulated 4,053
    # expired_in_queue and 716 send_failed rows that way, with nothing
    # surfacing that the channel was dead. An unconfigured customer channel
    # must be off, not silently broken.
    if not settings_spec.resolve_boolean(db, SettingDomain.notification, "sms_enabled"):
        logger.debug("SMS sending is disabled")
        return False

    # No default provider either: "webhook" with no webhook URL is a guaranteed
    # failure dressed up as a configured channel.
    provider = settings_spec.resolve_string(
        db, SettingDomain.notification, "sms_provider"
    )
    api_key, api_secret = _sms_credentials()
    from_number = settings_spec.resolve_string(
        db, SettingDomain.notification, "sms_from_number"
    )
    webhook_url = settings_spec.resolve_string(
        db, SettingDomain.notification, "sms_webhook_url"
    )
    timeout = float(
        settings_spec.resolve_integer(
            db, SettingDomain.notification, "sms_api_timeout_seconds"
        )
    )

    normalized_phone = _normalize_phone(to_phone)
    max_length = settings_spec.resolve_integer(
        db, SettingDomain.notification, "sms_max_length"
    )
    if max_length > 0 and len(body) > max_length:
        logger.warning(
            "SMS body length %d exceeds configured max %d; truncating",
            len(body),
            max_length,
        )
        body = body[:max_length]

    # Create notification record if tracking
    notification = None
    if notification_id or track:
        notification = notification_records.record_transport_attempt(
            db,
            notification_id=notification_id,
            channel=NotificationChannel.sms,
            recipient=normalized_phone,
            body=body,
        )

    # Send based on provider
    success = False
    external_id = None
    error_message = None

    if provider == "twilio":
        if not api_key or not api_secret or not from_number:
            error_message = "Twilio configuration incomplete"
            logger.error(error_message)
        else:
            success, external_id, error_message = _send_via_twilio(
                api_key,
                api_secret,
                from_number,
                normalized_phone,
                body,
                timeout=timeout,
            )

    elif provider == "africastalking":
        username = settings_spec.resolve_string(
            db, SettingDomain.notification, "sms_username"
        )
        if not api_key:
            error_message = "Africa's Talking API key not configured"
            logger.error(error_message)
        elif not username:
            error_message = "Africa's Talking username not configured"
            logger.error(error_message)
        else:
            success, external_id, error_message = _send_via_africastalking(
                api_key,
                username,
                from_number,
                normalized_phone,
                body,
                timeout=timeout,
            )

    elif provider == "webhook":
        if not webhook_url:
            error_message = "SMS webhook URL not configured"
            logger.error(error_message)
        else:
            success, external_id, error_message = _send_via_webhook(
                webhook_url,
                api_key,
                normalized_phone,
                body,
                timeout=timeout,
            )

    elif not provider:
        error_message = (
            "No SMS provider configured (set sms_provider to twilio, "
            "africastalking or webhook)"
        )
        logger.error(error_message)

    else:
        error_message = f"Unknown SMS provider: {provider}"
        logger.error(error_message)

    # Update notification status
    if notification:
        notification.status = (
            NotificationStatus.delivered if success else NotificationStatus.failed
        )
        notification.sent_at = datetime.now(UTC) if success else None
        notification.last_error = None if success else error_message

        # Create delivery record
        delivery = NotificationDelivery(
            notification_id=notification.id,
            provider=str(provider or "sms"),
            provider_message_id=external_id,
            status=DeliveryStatus.delivered if success else DeliveryStatus.failed,
            occurred_at=datetime.now(UTC),
            response_code="sent" if success else "error",
            response_body=error_message if error_message else body[:2000],
        )
        db.add(delivery)
        db.commit()

    if success:
        logger.info(f"SMS sent to {normalized_phone}")
    else:
        logger.error(f"SMS failed to {normalized_phone}: {error_message}")

    return success


def send_with_template(
    db: Session,
    template_code: str,
    to_phone: str,
    context: dict[str, Any],
) -> bool:
    """Send SMS using a notification template.

    Args:
        db: Database session
        template_code: The template's code identifier
        to_phone: Recipient phone number
        context: Template variables for substitution

    Returns:
        True if SMS was sent successfully
    """
    template = (
        db.query(NotificationTemplate)
        .filter(NotificationTemplate.code == template_code)
        .filter(NotificationTemplate.channel == NotificationChannel.sms)
        .filter(NotificationTemplate.is_active.is_(True))
        .first()
    )

    if not template:
        logger.error(f"SMS template not found: {template_code}")
        return False

    body = render_template_text(template.body, context)
    unresolved = sorted(set(_UNRESOLVED_TEMPLATE_RE.findall(body)))
    if unresolved:
        logger.error(
            "SMS template %s has unresolved variable(s): %s",
            template_code,
            ", ".join(unresolved),
        )
        return False

    return send_sms(db, to_phone, body, track=True)
