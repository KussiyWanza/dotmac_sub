"""Shared policy helpers for customer-facing notifications."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.notification import (
    Notification,
    NotificationChannel,
    NotificationStatus,
)
from app.models.subscriber import Subscriber, SubscriberContact
from app.services.customer_identity_normalization import (
    normalize_email_identifier,
    normalize_phone_identifier,
)
from app.services.customer_identity_resolution import resolve_customer_identity
from app.services.settings_spec import resolve_value

NotificationCategory = str
_DISABLED_VALUES = {"false", "0", "no", "off", "disabled"}
_ENABLED_VALUES = {"true", "1", "yes", "on", "enabled"}

_BILLING_PREFIXES = ("invoice_", "payment_")
_SERVICE_PREFIXES = (
    "subscription_",
    "service_order_",
    "provisioning_",
    "appointment_",
    "ont_",
)
_ACCOUNT_PREFIXES = ("subscriber_", "profile_")
_USAGE_PREFIXES = ("usage_",)


@dataclass(frozen=True, slots=True)
class CustomerNotificationPolicyCandidate:
    subscriber_id: UUID
    recipient: str

    @property
    def key(self) -> tuple[UUID, str]:
        return self.subscriber_id, self.recipient


@dataclass(frozen=True, slots=True)
class CustomerNotificationPolicyDecision:
    candidate: CustomerNotificationPolicyCandidate
    allowed: bool
    reason_code: str | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class CustomerNotificationPolicyCohortQuery:
    candidates: tuple[CustomerNotificationPolicyCandidate, ...]
    channel: NotificationChannel
    category: NotificationCategory
    event_type: str | None
    evaluated_at: datetime


@dataclass(frozen=True, slots=True)
class CustomerNotificationPolicyCohortResult:
    decisions: tuple[CustomerNotificationPolicyDecision, ...]

    def decision_for(
        self,
        *,
        subscriber_id: UUID,
        recipient: str,
    ) -> CustomerNotificationPolicyDecision:
        key = (subscriber_id, recipient)
        for decision in self.decisions:
            if decision.candidate.key == key:
                return decision
        raise KeyError(key)


def _metadata_flag(value: object, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def get_subscriber_notification_preferences(
    subscriber: Subscriber | None,
) -> dict[str, bool]:
    metadata = dict(subscriber.metadata_ or {}) if subscriber else {}
    categories = ("billing", "service", "account", "usage", "general")
    return {
        "billing_notifications": _metadata_flag(
            metadata.get("billing_notifications"),
            True,
        ),
        "sms_updates": _metadata_flag(metadata.get("sms_updates"), True),
        "push_notifications": _metadata_flag(metadata.get("push_notifications"), True),
        **{
            f"{category}_notifications": _metadata_flag(
                metadata.get(f"{category}_notifications"),
                True,
            )
            for category in categories
        },
    }


def resolve_notification_category(identifier: str | None) -> NotificationCategory:
    normalized = (identifier or "").strip().lower()
    try:
        from app.services.events.handlers.notification import EVENT_NOTIFICATION_SPECS

        for event_type, spec in EVENT_NOTIFICATION_SPECS.items():
            if normalized in {event_type.value, spec.template_code}:
                return spec.category
    except Exception:
        pass
    for prefix in _BILLING_PREFIXES:
        if normalized.startswith(prefix):
            return "billing"
    for prefix in _SERVICE_PREFIXES:
        if normalized.startswith(prefix):
            return "service"
    for prefix in _ACCOUNT_PREFIXES:
        if normalized.startswith(prefix):
            return "account"
    for prefix in _USAGE_PREFIXES:
        if normalized.startswith(prefix):
            return "usage"
    return "general"


def resolve_subscriber_id_for_recipient(
    db: Session,
    recipient: str | None,
) -> UUID | None:
    resolution = resolve_customer_identity(db, recipient)
    return resolution.subscriber_id if resolution.matched else None


def _contact_preference_rows(
    db: Session,
    *,
    subscriber_id: UUID,
    recipient: str | None,
) -> list[SubscriberContact]:
    normalized_email = normalize_email_identifier(recipient)
    normalized_phone = normalize_phone_identifier(recipient)
    if not normalized_email and not normalized_phone:
        return []

    rows = db.scalars(
        select(SubscriberContact).where(
            SubscriberContact.subscriber_id == subscriber_id
        )
    ).all()
    return [
        contact
        for contact in rows
        if (
            normalized_email
            and normalize_email_identifier(contact.email) == normalized_email
        )
        or (
            normalized_phone
            and (
                normalize_phone_identifier(contact.phone) == normalized_phone
                or normalize_phone_identifier(contact.whatsapp) == normalized_phone
            )
        )
    ]


def _notification_preferences_allow(
    subscriber: Subscriber,
    *,
    contacts: Iterable[SubscriberContact],
    channel: NotificationChannel | str,
    category: NotificationCategory,
    recipient: str | None,
) -> bool:
    normalized_channel = (
        (channel.value if isinstance(channel, NotificationChannel) else str(channel))
        .strip()
        .lower()
    )
    preferences = get_subscriber_notification_preferences(subscriber)
    category_key = f"{str(category or 'general').strip().lower()}_notifications"
    if category_key in preferences and not preferences[category_key]:
        return False
    if category == "billing" and not preferences["billing_notifications"]:
        return False
    if normalized_channel in {"sms", "whatsapp"} and not preferences["sms_updates"]:
        return False
    if normalized_channel == "push" and not preferences["push_notifications"]:
        return False

    normalized_email = normalize_email_identifier(recipient)
    normalized_phone = normalize_phone_identifier(recipient)
    matching_contacts = [
        contact
        for contact in contacts
        if (
            normalized_email
            and normalize_email_identifier(contact.email) == normalized_email
        )
        or (
            normalized_phone
            and (
                normalize_phone_identifier(contact.phone) == normalized_phone
                or normalize_phone_identifier(contact.whatsapp) == normalized_phone
            )
        )
    ]
    return not matching_contacts or any(
        contact.receives_notifications for contact in matching_contacts
    )


def is_notification_enabled_for_subscriber(
    db: Session,
    *,
    subscriber_id: UUID | None,
    channel: NotificationChannel | str,
    category: NotificationCategory,
    recipient: str | None = None,
) -> bool:
    if subscriber_id is None:
        return True

    subscriber = db.get(Subscriber, subscriber_id)
    if not subscriber:
        return True

    return _notification_preferences_allow(
        subscriber,
        contacts=_contact_preference_rows(
            db,
            subscriber_id=subscriber.id,
            recipient=recipient,
        ),
        channel=channel,
        category=category,
        recipient=recipient,
    )


def evaluate_bulk_customer_notification_policy(
    db: Session,
    query: CustomerNotificationPolicyCohortQuery,
) -> CustomerNotificationPolicyCohortResult:
    """Evaluate customer queue policy for a cohort with bounded query count."""

    items = tuple(dict.fromkeys(candidate.key for candidate in query.candidates))
    if not items:
        return CustomerNotificationPolicyCohortResult(decisions=())
    normalized_channel = query.channel
    candidate_rows = tuple(
        CustomerNotificationPolicyCandidate(
            subscriber_id=subscriber_id,
            recipient=recipient,
        )
        for subscriber_id, recipient in items
    )
    subscriber_ids = {candidate.subscriber_id for candidate in candidate_rows}
    subscribers = {
        subscriber.id: subscriber
        for subscriber in db.scalars(
            select(Subscriber).where(Subscriber.id.in_(subscriber_ids))
        ).all()
    }
    contacts_by_subscriber: dict[UUID, list[SubscriberContact]] = {
        subscriber_id: [] for subscriber_id in subscriber_ids
    }
    for contact in db.scalars(
        select(SubscriberContact).where(
            SubscriberContact.subscriber_id.in_(subscriber_ids)
        )
    ).all():
        contacts_by_subscriber.setdefault(contact.subscriber_id, []).append(contact)

    from app.services.communication_eligibility import (
        normalize_address,
        suppression_reasons_for_addresses,
    )
    from app.services.notification_status_policy import status_allows_notification

    durable_suppressions = suppression_reasons_for_addresses(
        db,
        channel=normalized_channel,
        addresses=(candidate.recipient for candidate in candidate_rows),
        category=query.category,
    )
    recent_keys: set[tuple[UUID, str]] = set()
    window_minutes = _setting_int(db, "notification_dedupe_window_minutes", 0)
    if window_minutes > 0:
        cutoff = query.evaluated_at - timedelta(minutes=window_minutes)
        recent_query = (
            select(Notification.subscriber_id, Notification.recipient)
            .where(Notification.subscriber_id.in_(subscriber_ids))
            .where(Notification.channel == normalized_channel)
            .where(Notification.created_at >= cutoff)
            .where(Notification.status != NotificationStatus.canceled)
            .where(
                Notification.recipient.in_(
                    {candidate.recipient for candidate in candidate_rows}
                )
            )
        )
        if query.event_type:
            recent_query = recent_query.where(
                Notification.event_type == query.event_type
            )
        if query.category:
            recent_query = recent_query.where(Notification.category == query.category)
        recent_keys = {
            (subscriber_id, recipient)
            for subscriber_id, recipient in db.execute(recent_query).all()
            if subscriber_id is not None
        }

    channel_disabled = channel_disabled_in_config(db, normalized_channel)
    decisions: dict[tuple[UUID, str], CustomerNotificationPolicyDecision] = {}
    for candidate in candidate_rows:
        subscriber = subscribers.get(candidate.subscriber_id)
        reason_code: str | None = None
        reason: str | None = None
        if channel_disabled:
            reason_code = "channel_configuration"
            reason = "Suppressed by notification channel configuration"
        elif subscriber and not status_allows_notification(
            subscriber.status,
            query.category,
        ):
            reason_code = "account_status"
            reason = "Suppressed by account notification status policy"
        elif subscriber and not _notification_preferences_allow(
            subscriber,
            contacts=contacts_by_subscriber.get(candidate.subscriber_id, ()),
            channel=normalized_channel,
            category=query.category,
            recipient=candidate.recipient,
        ):
            reason_code = "preferences"
            reason = "Suppressed by customer notification preferences"
        else:
            normalized_recipient = normalize_address(
                normalized_channel,
                candidate.recipient,
            )
            durable_reason = durable_suppressions.get(normalized_recipient)
            if durable_reason:
                reason_code = "communication_suppression"
                reason = "Suppressed by communication ledger: " + durable_reason
            elif candidate.key in recent_keys:
                reason_code = "dedupe"
                reason = "Suppressed by notification dedupe window"

        decisions[candidate.key] = CustomerNotificationPolicyDecision(
            candidate=candidate,
            allowed=reason_code is None,
            reason_code=reason_code,
            reason=reason,
        )
    return CustomerNotificationPolicyCohortResult(decisions=tuple(decisions.values()))


def status_allows_notification_for_subscriber(
    db: Session,
    *,
    subscriber_id: UUID | None,
    category: NotificationCategory | None,
) -> bool:
    """Apply the account-status notification gate for subscriber notifications."""
    if subscriber_id is None:
        return True

    from app.services.notification_status_policy import status_allows_notification

    subscriber = db.get(Subscriber, subscriber_id)
    status = subscriber.status if subscriber else None
    return status_allows_notification(status, category)


def channel_disabled_in_config(db: Session, channel: NotificationChannel | str) -> bool:
    """Return whether a notification channel is explicitly disabled.

    Channels without an enable flag are fail-open and are never suppressed here.
    """
    normalized_channel = (
        channel
        if isinstance(channel, NotificationChannel)
        else NotificationChannel(str(channel))
    )
    if normalized_channel != NotificationChannel.sms:
        return False
    try:
        from app.services.sms import _get_setting

        # Fail closed, matching sms.send_sms and the readiness probe: an
        # unconfigured SMS channel is disabled, so its notifications are
        # cancelled cleanly at queue time rather than created and left to fail.
        # SMS is retired by default; a future SMS plugin flips sms_enabled on.
        value = _get_setting(db, "sms_enabled", "SMS_ENABLED", "false")
    except Exception:
        return True
    return str(value or "false").strip().lower() not in _ENABLED_VALUES


def _setting_int(db: Session, key: str, default: int = 0) -> int:
    value = resolve_value(db, SettingDomain.notification, key)
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _parse_time(value: object, fallback: time) -> time:
    try:
        hour, minute = str(value or "").strip().split(":", 1)
        return time(hour=int(hour), minute=int(minute), tzinfo=UTC)
    except Exception:
        return fallback


def quiet_hours_send_at(db: Session, now: datetime | None = None) -> datetime | None:
    now = now or datetime.now(UTC)
    start = _parse_time(
        resolve_value(db, SettingDomain.notification, "notification_quiet_hours_start"),
        time(22, 0, tzinfo=UTC),
    )
    end = _parse_time(
        resolve_value(db, SettingDomain.notification, "notification_quiet_hours_end"),
        time(7, 0, tzinfo=UTC),
    )
    current = now.timetz()
    if start <= end:
        in_quiet_hours = start <= current < end
    else:
        in_quiet_hours = current >= start or current < end
    if not in_quiet_hours:
        return None
    send_date = now.date()
    if current >= start and start > end:
        send_date = send_date + timedelta(days=1)
    return datetime.combine(send_date, end, tzinfo=UTC)


def has_recent_notification(
    db: Session,
    *,
    subscriber_id: UUID | None,
    channel: NotificationChannel | str,
    event_type: str | None,
    category: str | None,
    recipient: str | None,
    now: datetime | None = None,
) -> bool:
    window_minutes = _setting_int(db, "notification_dedupe_window_minutes", 0)
    if window_minutes <= 0:
        return False
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(minutes=window_minutes)
    normalized_channel = (
        channel
        if isinstance(channel, NotificationChannel)
        else NotificationChannel(str(channel))
    )
    query = (
        db.query(Notification.id)
        .filter(Notification.channel == normalized_channel)
        .filter(Notification.created_at >= cutoff)
        .filter(Notification.status != NotificationStatus.canceled)
    )
    if subscriber_id is not None:
        query = query.filter(Notification.subscriber_id == subscriber_id)
    if event_type:
        query = query.filter(Notification.event_type == event_type)
    if category:
        query = query.filter(Notification.category == category)
    if recipient:
        query = query.filter(Notification.recipient == recipient)
    return query.first() is not None


def visible_notification_count(items: Iterable[object]) -> int:
    return sum(1 for _ in items)
