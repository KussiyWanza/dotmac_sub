"""Unified Notification Adapter - Multi-channel notification delivery.

Provides a single interface for sending notifications across:
- Email (SMTP/SendGrid/etc.)
- SMS (Twilio/AfricasTalking/etc.)
- Webhook (HTTP POST to external URLs)
- WebSocket (Real-time browser notifications)
- Push (Mobile push notifications)

Usage:
    from app.services.notification_adapter import notify, NotificationPriority

    # Simple notification
    notify.send(
        channel="email",
        recipient="user@example.com",
        subject="Alert",
        message="Something happened",
    )

    # Multi-channel notification
    notify.send_multi(
        channels=["email", "websocket"],
        recipient_id=user_id,
        event_type="ont_offline",
        context={"ont_serial": "HWTC12345678"},
    )

    # Operator alert (WebSocket + optional email)
    notify.alert_operators(
        title="OLT Connection Failed",
        message="BOI Huawei OLT is unreachable",
        severity="critical",
        metadata={"olt_id": olt_id},
    )
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from app.models.domain_settings import SettingDomain
from app.services import settings_spec
from app.services.adapters import adapter_registry

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums and Constants
# ---------------------------------------------------------------------------


class NotificationChannel(str, Enum):
    """Available notification channels."""

    email = "email"
    sms = "sms"
    whatsapp = "whatsapp"
    webhook = "webhook"
    websocket = "websocket"
    push = "push"


class NotificationPriority(str, Enum):
    """Notification priority levels."""

    low = "low"
    normal = "normal"
    high = "high"
    critical = "critical"


class NotificationCategory(str, Enum):
    """Notification categories for filtering/routing."""

    # Customer-facing
    billing = "billing"
    subscription = "subscription"
    usage = "usage"
    service = "service"

    # Operator/Admin
    network_alert = "network_alert"
    system_alert = "system_alert"
    security_alert = "security_alert"
    provisioning = "provisioning"

    # General
    general = "general"


class DeliveryStatus(str, Enum):
    """Notification delivery status."""

    pending = "pending"
    queued = "queued"
    sent = "sent"
    delivered = "delivered"
    failed = "failed"
    bounced = "bounced"


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------


@dataclass
class NotificationRequest:
    """A notification to be sent."""

    channel: NotificationChannel
    recipient: str  # Email, phone, user_id, webhook URL
    message: str
    subject: str | None = None
    title: str | None = None
    priority: NotificationPriority = NotificationPriority.normal
    category: NotificationCategory = NotificationCategory.general
    template_code: str | None = None
    context: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    scheduled_for: datetime | None = None
    expires_at: datetime | None = None
    idempotency_key: str | None = None


@dataclass
class NotificationResult:
    """Result of a notification send attempt."""

    success: bool
    message: str
    channel: NotificationChannel
    status: DeliveryStatus = DeliveryStatus.pending
    notification_id: str | None = None
    external_id: str | None = None  # Provider's ID
    delivered_at: datetime | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "message": self.message,
            "channel": self.channel.value,
            "status": self.status.value,
            "notification_id": self.notification_id,
            "external_id": self.external_id,
            "delivered_at": self.delivered_at.isoformat()
            if self.delivered_at
            else None,
            "error": self.error,
        }


@dataclass
class MultiChannelResult:
    """Result of sending to multiple channels."""

    results: dict[NotificationChannel, NotificationResult] = field(default_factory=dict)

    @property
    def success(self) -> bool:
        """True if at least one channel succeeded."""
        return any(r.success for r in self.results.values())

    @property
    def all_success(self) -> bool:
        """True if all channels succeeded."""
        return all(r.success for r in self.results.values())

    @property
    def failed_channels(self) -> list[NotificationChannel]:
        return [ch for ch, r in self.results.items() if not r.success]

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "all_success": self.all_success,
            "results": {ch.value: r.to_dict() for ch, r in self.results.items()},
        }


# ---------------------------------------------------------------------------
# Channel Providers (Protocol)
# ---------------------------------------------------------------------------


@runtime_checkable
class ChannelProvider(Protocol):
    """Protocol for channel-specific delivery."""

    @property
    def channel(self) -> NotificationChannel: ...

    def send(self, request: NotificationRequest) -> NotificationResult: ...

    def is_available(self) -> bool: ...


# ---------------------------------------------------------------------------
# Email Provider
# ---------------------------------------------------------------------------


class EmailProvider:
    """Email notification delivery."""

    @property
    def channel(self) -> NotificationChannel:
        return NotificationChannel.email

    def is_available(self) -> bool:
        # SMTP config lives in DB sender profiles (with env/legacy fallback),
        # not app.config attributes — resolve it the way send_email does.
        try:
            from app.services.db_session_adapter import db_session_adapter
            from app.services.email import _get_smtp_config

            with db_session_adapter.session() as db:
                config = _get_smtp_config(db)
            return bool(config and config.get("host"))
        except Exception:
            return False

    def send(self, request: NotificationRequest) -> NotificationResult:
        try:
            from app.services.db_session_adapter import db_session_adapter
            from app.services.email import send_email

            subject = request.subject or request.title or "Notification"
            with db_session_adapter.session() as db:
                html_body = self._render_html(request, db=db)
                success = send_email(
                    db=db,
                    to_email=request.recipient,
                    subject=subject,
                    body_html=html_body,
                    body_text=request.message,
                )
            if not success:
                return NotificationResult(
                    success=False,
                    message="Email send failed",
                    channel=self.channel,
                    status=DeliveryStatus.failed,
                    error="send_failed",
                )

            return NotificationResult(
                success=True,
                message="Email sent successfully",
                channel=self.channel,
                status=DeliveryStatus.sent,
                delivered_at=datetime.now(UTC),
            )

        except Exception as exc:
            logger.error("Email send failed to %s: %s", request.recipient, exc)
            return NotificationResult(
                success=False,
                message=f"Email send failed: {exc}",
                channel=self.channel,
                status=DeliveryStatus.failed,
                error=str(exc),
            )

    def _render_html(self, request: NotificationRequest, db=None) -> str:
        """Render HTML email body."""
        if request.template_code:
            try:
                from app.services.notification_template_renderer import render_template

                return render_template(
                    request.template_code,
                    request.context,
                    channel="email",
                )
            except Exception:
                pass

        # Fallback: branded wrapper around the plain message
        from html import escape

        from app.services.email_template import wrap_email_html

        title = request.title or request.subject or "Notification"
        body = (
            f'<h2 style="margin: 0 0 16px; font-size: 18px;">{escape(title)}</h2>'
            f'<p style="margin: 0; font-size: 14px; line-height: 1.6;">'
            f"{escape(request.message or '')}</p>"
        )
        resolved_brand = None
        subscriber_id = request.metadata.get("subscriber_id") or request.context.get(
            "subscriber_id"
        )
        if db is not None and subscriber_id:
            from app.services.brand_profiles import resolve_brand

            resolved_brand = resolve_brand(
                db, subscriber_id=str(subscriber_id)
            ).to_dict()
        return wrap_email_html(body, subject=title, brand=resolved_brand)


# ---------------------------------------------------------------------------
# SMS Provider
# ---------------------------------------------------------------------------


class SmsProvider:
    """SMS notification delivery."""

    @property
    def channel(self) -> NotificationChannel:
        return NotificationChannel.sms

    def is_available(self) -> bool:
        """Mirror `send_sms`'s configuration requirements exactly.

        This carried its own `(db_key, env_key, default)` table and read the
        environment ABOVE the stored row, with `sms_enabled` defaulting to
        `"true"` — while `send_sms`, the readiness probe and the customer
        policy all defaulted it to `"false"`. A deployment with only
        `SMS_WEBHOOK_URL` set therefore advertised a channel the send path
        refused. The spec owns those defaults now, so there is one answer.

        Failing closed when the database is unreachable is deliberate. The old
        fallback read env directly so a DB-less health probe could still report
        an env-configured channel; env is a declared BOOTSTRAP input now,
        materialised into rows by the seed, so a probe with no database
        genuinely cannot know — and an unconfigured customer channel must be
        off, not optimistically on.
        """

        try:
            from app.services.db_session_adapter import db_session_adapter
            from app.services.sms import _sms_credentials

            with db_session_adapter.session() as db:
                if not settings_spec.resolve_boolean(
                    db, SettingDomain.notification, "sms_enabled"
                ):
                    return False
                provider = settings_spec.resolve_string(
                    db, SettingDomain.notification, "sms_provider"
                )
                api_key, api_secret = _sms_credentials()
                if provider == "twilio":
                    from_number = settings_spec.resolve_string(
                        db, SettingDomain.notification, "sms_from_number"
                    )
                    return bool(api_key and api_secret and from_number)
                if provider == "africastalking":
                    return bool(api_key)
                if provider == "webhook":
                    return bool(
                        settings_spec.resolve_string(
                            db, SettingDomain.notification, "sms_webhook_url"
                        )
                    )
                return False
        except Exception:
            return False

    def send(self, request: NotificationRequest) -> NotificationResult:
        try:
            from app.services.db_session_adapter import db_session_adapter
            from app.services.sms import send_sms

            with db_session_adapter.session() as db:
                success = send_sms(
                    db=db,
                    to_phone=request.recipient,
                    body=request.message,
                )
            if not success:
                return NotificationResult(
                    success=False,
                    message="SMS send failed",
                    channel=self.channel,
                    status=DeliveryStatus.failed,
                    error="send_failed",
                )

            return NotificationResult(
                success=True,
                message="SMS sent successfully",
                channel=self.channel,
                status=DeliveryStatus.sent,
                delivered_at=datetime.now(UTC),
            )

        except ImportError:
            return NotificationResult(
                success=False,
                message="SMS service not configured",
                channel=self.channel,
                status=DeliveryStatus.failed,
                error="SMS module not available",
            )
        except Exception as exc:
            logger.error("SMS send failed to %s: %s", request.recipient, exc)
            return NotificationResult(
                success=False,
                message=f"SMS send failed: {exc}",
                channel=self.channel,
                status=DeliveryStatus.failed,
                error=str(exc),
            )


# ---------------------------------------------------------------------------
# Webhook Provider
# ---------------------------------------------------------------------------


class WebhookProvider:
    """Webhook notification delivery (HTTP POST)."""

    @property
    def channel(self) -> NotificationChannel:
        return NotificationChannel.webhook

    def is_available(self) -> bool:
        return True  # Always available if there's a URL

    def send(self, request: NotificationRequest) -> NotificationResult:
        import httpx

        webhook_url = request.recipient
        if not webhook_url.startswith(("http://", "https://")):
            return NotificationResult(
                success=False,
                message="Invalid webhook URL",
                channel=self.channel,
                status=DeliveryStatus.failed,
                error="URL must start with http:// or https://",
            )

        payload = {
            "event": request.category.value,
            "title": request.title,
            "message": request.message,
            "priority": request.priority.value,
            "timestamp": datetime.now(UTC).isoformat(),
            "metadata": request.metadata,
            "context": request.context,
        }

        try:
            with httpx.Client(timeout=30) as client:
                response = client.post(
                    webhook_url,
                    json=payload,
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "DotMac-Notification/1.0",
                    },
                )
                response.raise_for_status()

            return NotificationResult(
                success=True,
                message=f"Webhook delivered (HTTP {response.status_code})",
                channel=self.channel,
                status=DeliveryStatus.delivered,
                delivered_at=datetime.now(UTC),
            )

        except httpx.HTTPStatusError as exc:
            logger.error(
                "Webhook failed to %s: HTTP %s", webhook_url, exc.response.status_code
            )
            return NotificationResult(
                success=False,
                message=f"Webhook failed: HTTP {exc.response.status_code}",
                channel=self.channel,
                status=DeliveryStatus.failed,
                error=str(exc),
            )
        except Exception as exc:
            logger.error("Webhook failed to %s: %s", webhook_url, exc)
            return NotificationResult(
                success=False,
                message=f"Webhook failed: {exc}",
                channel=self.channel,
                status=DeliveryStatus.failed,
                error=str(exc),
            )


# ---------------------------------------------------------------------------
# WebSocket Provider
# ---------------------------------------------------------------------------


class WebSocketProvider:
    """Real-time WebSocket notification delivery via Redis pub/sub."""

    @property
    def channel(self) -> NotificationChannel:
        return NotificationChannel.websocket

    def is_available(self) -> bool:
        try:
            from app.services.redis_client import get_redis

            client = get_redis()
            if client is None:
                return False
            client.ping()
            return True
        except Exception:
            return False

    def send(self, request: NotificationRequest) -> NotificationResult:
        try:
            from app.services.realtime_platform import (
                STAFF_AUDIENCE_TOPIC,
                principal_topic,
                publish_topic_event,
            )

            # Build WebSocket event payload
            payload = {
                "type": "notification",
                "category": request.category.value,
                "title": request.title or "Notification",
                "message": request.message,
                "priority": request.priority.value,
                "timestamp": datetime.now(UTC).isoformat(),
                "metadata": request.metadata,
            }

            # Determine channel - user-specific or broadcast
            if request.recipient == "*" or request.recipient == "broadcast":
                topic = STAFF_AUDIENCE_TOPIC
            else:
                topic = principal_topic(request.recipient)

            if not publish_topic_event(
                topic,
                event_type="notification.received",
                payload=payload,
            ):
                raise RuntimeError("Real-time broker is unavailable")

            return NotificationResult(
                success=True,
                message="WebSocket notification published",
                channel=self.channel,
                status=DeliveryStatus.sent,
                delivered_at=datetime.now(UTC),
            )

        except Exception as exc:
            logger.error("WebSocket publish failed: %s", exc)
            return NotificationResult(
                success=False,
                message=f"WebSocket publish failed: {exc}",
                channel=self.channel,
                status=DeliveryStatus.failed,
                error=str(exc),
            )


# ---------------------------------------------------------------------------
# Notification Adapter
# ---------------------------------------------------------------------------


class NotificationAdapter:
    """Unified notification adapter for multi-channel delivery.

    Provides a single interface for sending notifications across all channels
    with automatic provider selection, fallback, and retry support.
    """

    name = "notification"
    depends_on: tuple[str, ...] = ()  # Independent - uses external services directly

    def __init__(self):
        self._providers: dict[NotificationChannel, ChannelProvider] = {}
        self._register_providers()

    def health_check(self) -> tuple[bool, str]:
        """Check that at least one notification channel is available."""
        available = [ch.value for ch in self._providers.keys()]
        if not available:
            return False, "No notification channels registered"
        return True, f"Channels available: {', '.join(available)}"

    def _register_providers(self) -> None:
        """Register all available channel providers."""
        providers = [
            EmailProvider(),
            SmsProvider(),
            WebhookProvider(),
            WebSocketProvider(),
        ]
        for provider in providers:
            self._providers[provider.channel] = provider

    def get_provider(
        self, channel: NotificationChannel | str
    ) -> ChannelProvider | None:
        """Get the provider for a channel."""
        if isinstance(channel, str):
            try:
                channel = NotificationChannel(channel.lower())
            except ValueError:
                return None
        return self._providers.get(channel)

    def is_channel_available(self, channel: NotificationChannel | str) -> bool:
        """Check if a channel is available for sending."""
        provider = self.get_provider(channel)
        return provider is not None and provider.is_available()

    def available_channels(self) -> list[NotificationChannel]:
        """List all available channels."""
        return [ch for ch in self._providers if self.is_channel_available(ch)]

    # -----------------------------------------------------------------------
    # Send Methods
    # -----------------------------------------------------------------------

    def send(
        self,
        channel: NotificationChannel | str,
        recipient: str,
        message: str,
        *,
        subject: str | None = None,
        title: str | None = None,
        priority: NotificationPriority | str = NotificationPriority.normal,
        category: NotificationCategory | str = NotificationCategory.general,
        template_code: str | None = None,
        context: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> NotificationResult:
        """Send a notification on a single channel.

        Args:
            channel: Notification channel (email, sms, webhook, websocket)
            recipient: Recipient address (email, phone, URL, user_id)
            message: Notification message body
            subject: Email subject line
            title: Notification title
            priority: Priority level
            category: Notification category
            template_code: Optional template to render
            context: Template rendering context
            metadata: Additional metadata

        Returns:
            NotificationResult with delivery status
        """
        # Normalize enums
        if isinstance(channel, str):
            channel = NotificationChannel(channel.lower())
        if isinstance(priority, str):
            priority = NotificationPriority(priority.lower())
        if isinstance(category, str):
            category = NotificationCategory(category.lower())

        request = NotificationRequest(
            channel=channel,
            recipient=recipient,
            message=message,
            subject=subject,
            title=title,
            priority=priority,
            category=category,
            template_code=template_code,
            context=context or {},
            metadata=metadata or {},
            idempotency_key=idempotency_key,
        )

        provider = self.get_provider(channel)
        if provider is None:
            return NotificationResult(
                success=False,
                message=f"No provider for channel: {channel}",
                channel=channel,
                status=DeliveryStatus.failed,
                error="Channel not supported",
            )

        if not provider.is_available():
            return NotificationResult(
                success=False,
                message=f"Channel {channel} is not available",
                channel=channel,
                status=DeliveryStatus.failed,
                error="Provider not configured",
            )

        if request.idempotency_key:
            try:
                from app.services.session_store import get_session_redis

                redis = get_session_redis()
                if redis is not None:
                    key = f"notification:idempotency:{request.idempotency_key}"
                    reserved = redis.set(key, "1", nx=True, ex=86400)
                    if not reserved:
                        return NotificationResult(
                            success=True,
                            message="Duplicate notification skipped",
                            channel=channel,
                            status=DeliveryStatus.sent,
                        )
            except Exception:
                logger.warning("Notification idempotency check failed", exc_info=True)

        return provider.send(request)

    def send_multi(
        self,
        channels: list[NotificationChannel | str],
        recipient: str | dict[str, str],
        message: str,
        *,
        subject: str | None = None,
        title: str | None = None,
        priority: NotificationPriority | str = NotificationPriority.normal,
        category: NotificationCategory | str = NotificationCategory.general,
        template_code: str | None = None,
        context: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        stop_on_success: bool = False,
    ) -> MultiChannelResult:
        """Send notification to multiple channels.

        Args:
            channels: List of channels to send on
            recipient: Single recipient or dict mapping channel to recipient
            message: Notification message
            subject: Email subject
            title: Notification title
            priority: Priority level
            category: Category
            template_code: Template code
            context: Template context
            metadata: Metadata
            stop_on_success: Stop after first successful delivery

        Returns:
            MultiChannelResult with results per channel
        """
        result = MultiChannelResult()

        for channel in channels:
            if isinstance(channel, str):
                channel = NotificationChannel(channel.lower())

            # Resolve recipient for this channel
            if isinstance(recipient, dict):
                ch_recipient = recipient.get(channel.value) or recipient.get(
                    "default", ""
                )
            else:
                ch_recipient = recipient

            if not ch_recipient:
                result.results[channel] = NotificationResult(
                    success=False,
                    message="No recipient for channel",
                    channel=channel,
                    status=DeliveryStatus.failed,
                )
                continue

            ch_result = self.send(
                channel=channel,
                recipient=ch_recipient,
                message=message,
                subject=subject,
                title=title,
                priority=priority,
                category=category,
                template_code=template_code,
                context=context,
                metadata=metadata,
            )
            result.results[channel] = ch_result

            if stop_on_success and ch_result.success:
                break

        return result

    # -----------------------------------------------------------------------
    # Convenience Methods
    # -----------------------------------------------------------------------

    def alert_operators(
        self,
        title: str,
        message: str,
        *,
        severity: str = "warning",
        metadata: dict[str, Any] | None = None,
        include_email: bool = False,
        email_recipients: list[str] | None = None,
    ) -> MultiChannelResult:
        """Send alert to operators via WebSocket (and optionally email).

        This broadcasts to all connected admin users.

        Args:
            title: Alert title
            message: Alert message
            severity: Severity level (info, warning, error, critical)
            metadata: Additional alert data
            include_email: Also send email alerts
            email_recipients: Email addresses for email alerts

        Returns:
            MultiChannelResult
        """
        priority = {
            "info": NotificationPriority.low,
            "warning": NotificationPriority.normal,
            "error": NotificationPriority.high,
            "critical": NotificationPriority.critical,
        }.get(severity, NotificationPriority.normal)

        channels: list[NotificationChannel] = [NotificationChannel.websocket]
        recipients: dict[str, str] = {"websocket": "broadcast"}

        if include_email and email_recipients:
            channels.append(NotificationChannel.email)
            # For email, we'll need to send individually
            # For now, use first recipient
            recipients["email"] = email_recipients[0]

        return self.send_multi(
            channels=channels,
            recipient=recipients,
            message=message,
            title=title,
            priority=priority,
            category=NotificationCategory.network_alert,
            metadata={
                "severity": severity,
                **(metadata or {}),
            },
        )

    def notify_provisioning_complete(
        self,
        subscriber_id: str,
        ont_serial: str,
        *,
        channels: list[str] | None = None,
        recipient_email: str | None = None,
        recipient_phone: str | None = None,
    ) -> MultiChannelResult:
        """Send provisioning complete notification to customer.

        Args:
            subscriber_id: Subscriber ID
            ont_serial: ONT serial number
            channels: Channels to use (default: email)
            recipient_email: Customer email
            recipient_phone: Customer phone

        Returns:
            MultiChannelResult
        """
        channels = channels or ["email"]
        recipients = {}
        if recipient_email:
            recipients["email"] = recipient_email
        if recipient_phone:
            recipients["sms"] = recipient_phone

        return self.send_multi(
            channels=[NotificationChannel(ch) for ch in channels],
            recipient=recipients,
            message=f"Your internet service has been activated. ONT Serial: {ont_serial}",
            title="Service Activated",
            subject="Your Internet Service is Now Active",
            category=NotificationCategory.provisioning,
            template_code="provisioning_completed",
            context={
                "subscriber_id": subscriber_id,
                "ont_serial": ont_serial,
            },
        )

    def notify_ont_offline(
        self,
        ont_serial: str,
        ont_id: str,
        olt_name: str,
        *,
        subscriber_email: str | None = None,
        operator_alert: bool = True,
    ) -> MultiChannelResult:
        """Send ONT offline notification.

        Args:
            ont_serial: ONT serial number
            ont_id: ONT database ID
            olt_name: OLT name
            subscriber_email: Customer email (optional)
            operator_alert: Send operator WebSocket alert

        Returns:
            MultiChannelResult
        """
        result = MultiChannelResult()

        # Operator alert
        if operator_alert:
            op_result = self.alert_operators(
                title="ONT Offline",
                message=f"ONT {ont_serial} on {olt_name} is offline",
                severity="warning",
                metadata={
                    "ont_id": ont_id,
                    "ont_serial": ont_serial,
                    "olt_name": olt_name,
                },
            )
            result.results.update(op_result.results)

        # Customer notification
        if subscriber_email:
            email_result = self.send(
                channel=NotificationChannel.email,
                recipient=subscriber_email,
                message=f"We detected that your internet device ({ont_serial}) is offline. "
                "Our team is investigating. You will be notified when service is restored.",
                title="Internet Service Interruption",
                subject="Internet Service Alert",
                category=NotificationCategory.service,
                template_code="ont_offline",
                context={
                    "ont_serial": ont_serial,
                },
            )
            result.results[NotificationChannel.email] = email_result

        return result


# ---------------------------------------------------------------------------
# Singleton Instance
# ---------------------------------------------------------------------------


notify = NotificationAdapter()
adapter_registry.register(notify)


# ---------------------------------------------------------------------------
# Convenience Functions
# ---------------------------------------------------------------------------


def send_notification(
    channel: str,
    recipient: str,
    message: str,
    **kwargs,
) -> NotificationResult:
    """Send a notification (convenience wrapper)."""
    return notify.send(channel, recipient, message, **kwargs)


def alert_operators(
    title: str,
    message: str,
    severity: str = "warning",
    **kwargs,
) -> MultiChannelResult:
    """Alert operators (convenience wrapper)."""
    return notify.alert_operators(title, message, severity=severity, **kwargs)


def broadcast_websocket(
    event_type: str,
    title: str,
    message: str,
    metadata: dict | None = None,
) -> NotificationResult:
    """Broadcast a WebSocket message to all connected clients."""
    return notify.send(
        channel=NotificationChannel.websocket,
        recipient="broadcast",
        message=message,
        title=title,
        category=NotificationCategory.general,
        metadata={
            "event_type": event_type,
            **(metadata or {}),
        },
    )
