from __future__ import annotations

from types import SimpleNamespace

from app.models.notification import NotificationChannel
from app.services import web_notifications as web_notifications_service


def test_bulk_notification_setup_context_reports_channel_readiness(monkeypatch):
    templates = [
        SimpleNamespace(
            id="tmpl-email",
            name="Welcome Email",
            code="welcome_email",
            channel=NotificationChannel.email,
            subject="Hello",
            is_active=True,
        ),
        SimpleNamespace(
            id="tmpl-sms",
            name="Reminder SMS",
            code="reminder_sms",
            channel=NotificationChannel.sms,
            subject=None,
            is_active=True,
        ),
    ]

    monkeypatch.setattr(
        web_notifications_service.notification_service.templates,
        "list",
        lambda **_kwargs: templates,
    )
    monkeypatch.setattr(
        web_notifications_service,
        "sync_whatsapp_registry_templates",
        lambda _db: [],
    )
    monkeypatch.setattr(
        web_notifications_service.email_service,
        "list_smtp_senders",
        lambda _db: [{"sender_key": "default"}],
    )

    # These tests pass a stand-in for the session, so the resolvers are stubbed
    # rather than driven with rows. The credentials come from the ADR-0009 seam,
    # not from a setting.
    values = {
        "sms_provider": "twilio",
        "sms_from_number": "+15550000000",
    }
    monkeypatch.setattr(
        web_notifications_service.settings_spec,
        "resolve_boolean",
        lambda _db, _domain, key: key == "sms_enabled",
    )
    monkeypatch.setattr(
        web_notifications_service.settings_spec,
        "resolve_string",
        lambda _db, _domain, key: values.get(key, ""),
    )
    monkeypatch.setattr(
        web_notifications_service.sms_service,
        "_sms_credentials",
        lambda: ("sid", "token"),
    )
    monkeypatch.setattr(
        web_notifications_service.whatsapp_capability,
        "active_config",
        lambda _db: {
            "provider": "meta_cloud_api",
            "phone_number": "08012345678",
        },
    )

    context = web_notifications_service.bulk_notification_setup_context(object())

    channels = {item["id"]: item for item in context["bulk_notification_channels"]}
    assert channels["email"]["ready"] is True
    assert channels["email"]["template_count"] == 1
    assert channels["sms"]["ready"] is True
    assert channels["sms"]["message"] == "Twilio credentials configured"
    assert channels["sms"]["template_count"] == 1
    assert channels["whatsapp"]["ready"] is True
    assert channels["whatsapp"]["message"] == "Meta Cloud API is configured"

    templates_state = {
        item["id"]: item for item in context["bulk_notification_templates"]
    }
    assert templates_state["tmpl-email"]["channel"] == "email"
    assert templates_state["tmpl-sms"]["subject"] == ""


def test_bulk_notification_setup_context_reports_missing_channel_config(monkeypatch):
    monkeypatch.setattr(
        web_notifications_service.notification_service.templates,
        "list",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(
        web_notifications_service,
        "sync_whatsapp_registry_templates",
        lambda _db: [],
    )
    monkeypatch.setattr(
        web_notifications_service.email_service,
        "list_smtp_senders",
        lambda _db: [],
    )
    monkeypatch.setattr(
        web_notifications_service.settings_spec,
        "resolve_boolean",
        lambda _db, _domain, _key: False,
    )
    monkeypatch.setattr(
        web_notifications_service.whatsapp_capability,
        "active_config",
        lambda _db: (_ for _ in ()).throw(ValueError("not configured")),
    )

    context = web_notifications_service.bulk_notification_setup_context(object())

    channels = {item["id"]: item for item in context["bulk_notification_channels"]}
    assert channels["email"]["ready"] is False
    assert channels["email"]["message"] == "No SMTP sender profile configured"
    assert channels["sms"]["ready"] is False
    assert channels["sms"]["message"] == "SMS is disabled"
    assert channels["whatsapp"]["ready"] is False
    assert channels["whatsapp"]["message"] == "No enabled WhatsApp capability binding"
