import pytest

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.notification import (
    Notification,
    NotificationChannel,
    NotificationStatus,
)
from app.models.subscriber import Subscriber, SubscriberStatus
from app.models.subscription_engine import SettingValueType
from app.schemas.notification import (
    NotificationCreate,
    NotificationDeliveryLatency,
    NotificationTemplateCreate,
    NotificationTemplateUpdate,
)
from app.services import email as email_service
from app.services import notification as notification_service
from app.tasks import notifications as notification_tasks


def test_notification_template_and_delivery(db_session):
    template = notification_service.templates.create(
        db_session,
        NotificationTemplateCreate(
            name="Welcome",
            code="welcome_email",
            channel=NotificationChannel.email,
            subject="Hello",
            body="Welcome!",
        ),
    )
    notification = notification_service.notifications.create(
        db_session,
        NotificationCreate(
            template_id=template.id,
            channel=NotificationChannel.email,
            recipient="user@example.com",
            status=NotificationStatus.queued,
            payload={"name": "User"},
        ),
    )
    items = notification_service.notifications.list(
        db_session,
        channel=NotificationChannel.email.value,
        status=NotificationStatus.queued.value,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=10,
        offset=0,
    )
    assert len(items) == 1
    assert items[0].id == notification.id


def test_immediate_latency_schedules_targeted_delivery_after_commit(
    db_session, monkeypatch
):
    delivery_wakeups: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        notification_tasks.deliver_notification,
        "apply_async",
        lambda *args, **kwargs: delivery_wakeups.append((args, kwargs)),
    )

    notification = notification_service.notifications.queue_internal_notification(
        db_session,
        NotificationCreate(
            channel=NotificationChannel.email,
            recipient="now@example.com",
            subject="Action required",
            body="Confirm now.",
            delivery_latency=NotificationDeliveryLatency.immediate,
        ),
    )

    assert delivery_wakeups == []
    db_session.commit()

    assert notification.metadata_["delivery_latency"] == "immediate"
    assert delivery_wakeups == [((), {"args": [str(notification.id)], "retry": False})]


def test_normal_latency_does_not_schedule_targeted_delivery(db_session, monkeypatch):
    delivery_wakeups: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        notification_tasks.deliver_notification,
        "apply_async",
        lambda *args, **kwargs: delivery_wakeups.append((args, kwargs)),
    )

    notification = notification_service.notifications.queue_internal_notification(
        db_session,
        NotificationCreate(
            channel=NotificationChannel.email,
            recipient="later@example.com",
            subject="General update",
            body="This can use the normal sweep.",
        ),
    )
    db_session.commit()

    assert notification.metadata_["delivery_latency"] == "normal"
    assert delivery_wakeups == []


def test_auth_verification_email_is_immediate_latency(db_session, monkeypatch):
    delivery_wakeups: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        notification_tasks.deliver_notification,
        "apply_async",
        lambda *args, **kwargs: delivery_wakeups.append((args, kwargs)),
    )

    queued = email_service.send_email(
        db_session,
        "verify@example.com",
        "Verify your email address",
        "<p>Verify now.</p>",
        "Verify now.",
        activity="auth_email_verification",
    )

    notification = db_session.query(Notification).one()
    assert queued is True
    assert notification.event_type == "auth_email_verification"
    assert notification.metadata_["delivery_latency"] == "immediate"
    assert delivery_wakeups == [((), {"args": [str(notification.id)], "retry": False})]


def test_notification_owner_blocks_incomplete_active_payment_receipt(db_session):
    with pytest.raises(ValueError, match="require these body fields"):
        notification_service.templates.create(
            db_session,
            NotificationTemplateCreate(
                name="Incomplete receipt",
                code="payment_received",
                channel=NotificationChannel.email,
                subject="Payment received",
                body="Thank you for paying {amount}.",
            ),
        )


def test_notification_owner_validates_inactive_receipt_before_activation(db_session):
    template = notification_service.templates.create(
        db_session,
        NotificationTemplateCreate(
            name="Receipt draft",
            code="payment_received",
            channel=NotificationChannel.email,
            subject="Payment received",
            body="Thank you for paying {amount}.",
            is_active=False,
        ),
    )

    with pytest.raises(ValueError, match="require these body fields"):
        notification_service.templates.update(
            db_session,
            str(template.id),
            NotificationTemplateUpdate(is_active=True),
        )

    db_session.refresh(template)
    assert template.is_active is False

    activated = notification_service.templates.update(
        db_session,
        str(template.id),
        NotificationTemplateUpdate(
            subject="Payment receipt {receipt_number}",
            body="Receipt {receipt_number}: {receipt_url}",
            is_active=True,
        ),
    )
    assert activated.is_active is True


def _subscriber(db_session, *, status=SubscriberStatus.active, suffix="manual"):
    sub = Subscriber(
        first_name="Manual",
        last_name="Notify",
        email=f"manual-notify-{suffix}@example.com",
        phone="+2348012345678",
        status=status,
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)
    return sub


def _setting(db_session, key: str, value: str, value_type=SettingValueType.string):
    row = (
        db_session.query(DomainSetting)
        .filter_by(domain=SettingDomain.notification, key=key)
        .one_or_none()
    )
    if row is None:
        row = DomainSetting(
            domain=SettingDomain.notification,
            key=key,
            value_type=value_type,
            value_text=value,
            is_active=True,
        )
        db_session.add(row)
    else:
        row.value_type = value_type
        row.value_text = value
        row.value_json = None
        row.is_active = True
    db_session.commit()


def test_manual_notification_create_uses_status_gate(db_session):
    subscriber = _subscriber(
        db_session, status=SubscriberStatus.canceled, suffix="canceled"
    )

    notification = notification_service.notifications.create(
        db_session,
        NotificationCreate(
            subscriber_id=subscriber.id,
            channel=NotificationChannel.email,
            recipient=subscriber.email,
            subject="Usage update",
            body="Usage update",
            category="usage",
            event_type="manual_usage_notice",
        ),
    )

    assert notification.status == NotificationStatus.canceled
    assert notification.last_error == "Suppressed by account notification status policy"


def test_manual_notification_create_uses_channel_gate(db_session):
    subscriber = _subscriber(db_session, suffix="sms-disabled")
    _setting(
        db_session,
        "sms_enabled",
        "false",
        value_type=SettingValueType.boolean,
    )

    notification = notification_service.notifications.create(
        db_session,
        NotificationCreate(
            subscriber_id=subscriber.id,
            channel=NotificationChannel.sms,
            recipient=subscriber.phone,
            subject="Service update",
            body="Service update",
            category="service",
            event_type="manual_service_notice",
        ),
    )

    assert notification.status == NotificationStatus.canceled
    assert notification.last_error == "Suppressed by notification channel configuration"


def test_manual_notification_create_applies_quiet_hours_and_dedupe(db_session):
    subscriber = _subscriber(db_session, suffix="dedupe")
    _setting(db_session, "notification_quiet_hours_start", "00:00")
    _setting(db_session, "notification_quiet_hours_end", "23:59")
    _setting(
        db_session,
        "notification_dedupe_window_minutes",
        "60",
        value_type=SettingValueType.integer,
    )

    first = notification_service.notifications.create(
        db_session,
        NotificationCreate(
            subscriber_id=subscriber.id,
            channel=NotificationChannel.email,
            recipient=subscriber.email,
            subject="Service update",
            body="Service update",
            category="service",
            event_type="manual_service_notice",
        ),
    )
    second = notification_service.notifications.create(
        db_session,
        NotificationCreate(
            subscriber_id=subscriber.id,
            channel=NotificationChannel.email,
            recipient=subscriber.email,
            subject="Service update",
            body="Service update",
            category="service",
            event_type="manual_service_notice",
        ),
    )

    assert first.status == NotificationStatus.queued
    assert first.send_at is not None
    assert second.status == NotificationStatus.canceled
    assert second.last_error == "Suppressed duplicate customer notification"


def test_event_notification_drops_policy_suppression(db_session):
    subscriber = _subscriber(
        db_session, status=SubscriberStatus.canceled, suffix="event-drop"
    )
    before = db_session.query(Notification).count()

    notification = notification_service.notifications.queue_event_notification(
        db_session,
        NotificationCreate(
            subscriber_id=subscriber.id,
            channel=NotificationChannel.email,
            recipient=subscriber.email,
            subject="Usage update",
            body="Usage update",
            category="usage",
            event_type="usage_warning",
        ),
    )

    assert notification is None
    assert db_session.query(Notification).count() == before


def test_queue_customer_notifications_for_policy_resolves_channels_and_recipients(
    db_session,
):
    subscriber = _subscriber(db_session, suffix="policy-helper")

    created = (
        notification_service.notifications.queue_customer_notifications_for_policy(
            db_session,
            subscriber=subscriber,
            template_code="fup_throttled",
            event_type="fup_throttled",
            category="fup",
            default_channels=(NotificationChannel.push, NotificationChannel.email),
            subject="Usage threshold reached",
            body="Your usage crossed the configured threshold.",
        )
    )

    rows = (
        db_session.query(Notification)
        .filter(Notification.subscriber_id == subscriber.id)
        .order_by(Notification.channel)
        .all()
    )

    assert created == 2
    assert {row.channel for row in rows} == {
        NotificationChannel.email,
        NotificationChannel.push,
    }
    assert {row.recipient for row in rows} == {subscriber.email, str(subscriber.id)}
    assert {row.event_type for row in rows} == {"fup_throttled"}
    assert {row.category for row in rows} == {"fup"}


def test_internal_notification_bypasses_customer_identity_and_gates(db_session):
    subscriber = _subscriber(
        db_session, status=SubscriberStatus.canceled, suffix="internal-bypass"
    )
    _setting(
        db_session,
        "sms_enabled",
        "false",
        value_type=SettingValueType.boolean,
    )

    notification = notification_service.notifications.queue_internal_notification(
        db_session,
        NotificationCreate(
            channel=NotificationChannel.sms,
            recipient=subscriber.phone,
            subject="Internal note",
            body="Internal note",
        ),
    )

    assert notification.subscriber_id is None
    assert notification.status == NotificationStatus.queued
    assert notification.last_error is None


def test_record_transport_attempt_creates_sending_row(db_session):
    notification = notification_service.notifications.record_transport_attempt(
        db_session,
        channel=NotificationChannel.email,
        recipient="transport@example.com",
        subject="Sending",
        body="Body",
    )

    assert notification.status == NotificationStatus.sending
    assert notification.recipient == "transport@example.com"

    updated = notification_service.notifications.record_transport_attempt(
        db_session,
        notification_id=notification.id,
        channel=NotificationChannel.sms,
        recipient="+2348011112222",
        body="SMS",
    )

    assert updated.id == notification.id
    assert updated.channel == NotificationChannel.sms
    assert updated.status == NotificationStatus.sending
    assert updated.last_error is None
