"""EmailProvider/SmsProvider must invoke the real senders with their actual
signatures (they used to pass wrong kwargs and silently swallow the TypeError)."""

from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy.orm import Session

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.notification import NotificationChannel
from app.models.subscription_engine import SettingValueType
from app.services import sms as sms_service
from app.services.db_session_adapter import db_session_adapter
from app.services.notification_adapter import (
    EmailProvider,
    NotificationRequest,
    SmsProvider,
)


def test_email_provider_send_invokes_send_email_correctly(monkeypatch):
    captured: dict = {}

    def _fake_send_email(**kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr("app.services.email.send_email", _fake_send_email)

    result = EmailProvider().send(
        NotificationRequest(
            channel=NotificationChannel.email,
            recipient="ops@example.com",
            message="Provisioning complete",
            title="ONT provisioned",
        )
    )

    assert result.success is True
    assert captured["to_email"] == "ops@example.com"
    assert isinstance(captured["db"], Session)
    assert captured["body_text"] == "Provisioning complete"
    assert "<!DOCTYPE html>" in captured["body_html"]
    assert "ONT provisioned" in captured["body_html"]


def test_email_provider_send_reports_failure(monkeypatch):
    monkeypatch.setattr("app.services.email.send_email", lambda **_: False)

    result = EmailProvider().send(
        NotificationRequest(
            channel=NotificationChannel.email,
            recipient="ops@example.com",
            message="hello",
        )
    )

    assert result.success is False
    assert result.error == "send_failed"


def test_sms_provider_send_invokes_send_sms_correctly(monkeypatch):
    captured: dict = {}

    def _fake_send_sms(**kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr("app.services.sms.send_sms", _fake_send_sms)

    long_message = "x" * 200
    result = SmsProvider().send(
        NotificationRequest(
            channel=NotificationChannel.sms,
            recipient="+2348000000001",
            message=long_message,
        )
    )

    assert result.success is True
    assert captured["to_phone"] == "+2348000000001"
    assert isinstance(captured["db"], Session)
    assert captured["body"] == long_message


def _drive_availability(monkeypatch, db_session, rows: dict[str, str]):
    """Point `is_available()` at a real session with real setting rows.

    These tests used to configure SMS with environment variables. Env is a
    declared BOOTSTRAP input now, materialised into rows by the seed and never
    read at runtime, so an env-only test would assert nothing — worse, two of
    the three would still pass, because a test session has no database and the
    probe fails closed. Driving rows keeps them meaningful.
    """

    @contextmanager
    def _session():
        yield db_session

    monkeypatch.setattr(db_session_adapter, "session", _session)
    for key, value in rows.items():
        db_session.add(
            DomainSetting(
                domain=SettingDomain.notification,
                key=key,
                value_type=SettingValueType.string,
                value_text=value,
                is_active=True,
            )
        )
    db_session.commit()


def test_sms_provider_unavailable_without_provider_config(db_session, monkeypatch):
    monkeypatch.setattr(sms_service, "_sms_credentials", lambda: ("", ""))
    _drive_availability(
        monkeypatch, db_session, {"sms_enabled": "true", "sms_provider": "twilio"}
    )
    assert SmsProvider().is_available() is False


def test_sms_provider_available_with_webhook_config(db_session, monkeypatch):
    monkeypatch.setattr(sms_service, "_sms_credentials", lambda: ("", ""))
    _drive_availability(
        monkeypatch,
        db_session,
        {
            "sms_enabled": "true",
            "sms_provider": "webhook",
            "sms_webhook_url": "https://sms.example.test/send",
        },
    )
    assert SmsProvider().is_available() is True


def test_sms_provider_unavailable_when_disabled(db_session, monkeypatch):
    monkeypatch.setattr(sms_service, "_sms_credentials", lambda: ("", ""))
    _drive_availability(
        monkeypatch,
        db_session,
        {
            "sms_enabled": "false",
            "sms_provider": "webhook",
            "sms_webhook_url": "https://sms.example.test/send",
        },
    )
    assert SmsProvider().is_available() is False


def test_sms_provider_unavailable_without_a_database(monkeypatch):
    """The deliberate behaviour change, pinned.

    The old code read env directly so a DB-less probe could still report an
    env-configured channel. It cannot know any more, and an unconfigured
    customer channel must be off rather than optimistically on.
    """

    @contextmanager
    def _no_session():
        raise RuntimeError("no database")
        yield  # pragma: no cover

    monkeypatch.setattr(db_session_adapter, "session", _no_session)
    monkeypatch.setenv("SMS_ENABLED", "true")
    monkeypatch.setenv("SMS_PROVIDER", "webhook")
    monkeypatch.setenv("SMS_WEBHOOK_URL", "https://sms.example.test/send")

    assert SmsProvider().is_available() is False
