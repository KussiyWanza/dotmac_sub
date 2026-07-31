"""Tests for email service."""

import smtplib
from email import message_from_string

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.notification import (
    Notification,
    NotificationChannel,
    NotificationDelivery,
    NotificationStatus,
)
from app.models.subscription_engine import SettingValueType
from app.schemas.settings import DomainSettingUpdate
from app.services import email as email_service
from app.services.domain_settings import notification_settings
from tests.mocks import FakeSMTP


def test_send_email_success(db_session, monkeypatch):
    """Test sending email successfully."""
    fake_smtp = FakeSMTP()

    def mock_smtp(*args, **kwargs):
        return fake_smtp

    monkeypatch.setattr("smtplib.SMTP", mock_smtp)
    monkeypatch.setattr("smtplib.SMTP_SSL", mock_smtp)
    monkeypatch.setenv("SMTP_HOST", "smtp.test.local")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USER", "testuser")
    monkeypatch.setenv("SMTP_PASSWORD", "testpass")
    monkeypatch.setenv("SMTP_FROM", "noreply@test.local")

    result = email_service.send_email(
        db=db_session,
        to_email="recipient@example.com",
        subject="Test Subject",
        body_html="<p>Hello World</p>",
        body_text="Hello World",
        track=False,
    )

    assert result is True
    assert len(fake_smtp.messages) == 1
    from_addr, to_addrs, msg = fake_smtp.messages[0]
    assert "recipient@example.com" in to_addrs


def test_send_email_delivers_cc_and_hidden_bcc(db_session, monkeypatch):
    fake_smtp = FakeSMTP()
    monkeypatch.setattr("smtplib.SMTP", lambda *args, **kwargs: fake_smtp)
    monkeypatch.setattr("smtplib.SMTP_SSL", lambda *args, **kwargs: fake_smtp)
    monkeypatch.setenv("SMTP_HOST", "smtp.test.local")
    monkeypatch.setenv("SMTP_FROM", "noreply@test.local")

    assert email_service.send_email(
        db=db_session,
        to_email="primary@example.com",
        subject="Recipients",
        body_html="<p>Hello</p>",
        track=False,
        cc_addresses=["copy@example.com"],
        bcc_addresses=["hidden@example.com"],
    )

    _, recipients, message = fake_smtp.messages[0]
    assert recipients == [
        "primary@example.com",
        "copy@example.com",
        "hidden@example.com",
    ]
    assert "Cc: copy@example.com" in message
    assert "\nBcc:" not in message


def test_send_email_with_selected_config_delivers_cc_and_hidden_bcc(monkeypatch):
    fake_smtp = FakeSMTP()
    monkeypatch.setattr("smtplib.SMTP", lambda *args, **kwargs: fake_smtp)
    monkeypatch.setattr("smtplib.SMTP_SSL", lambda *args, **kwargs: fake_smtp)

    assert email_service.send_email_with_config(
        {
            "host": "smtp.selected.test",
            "port": 587,
            "from_email": "support@example.com",
            "from_name": "Support",
        },
        "primary@example.com",
        "Recipients",
        "<p>Hello</p>",
        cc_addresses=["copy@example.com"],
        bcc_addresses=["hidden@example.com"],
    )

    _, recipients, message = fake_smtp.messages[0]
    assert recipients == [
        "primary@example.com",
        "copy@example.com",
        "hidden@example.com",
    ]
    assert "Cc: copy@example.com" in message
    assert "\nBcc:" not in message


def test_send_email_html_and_text(db_session, monkeypatch):
    """Test sending email with both HTML and text content."""
    fake_smtp = FakeSMTP()

    def mock_smtp(*args, **kwargs):
        return fake_smtp

    monkeypatch.setattr("smtplib.SMTP", mock_smtp)
    monkeypatch.setattr("smtplib.SMTP_SSL", mock_smtp)
    monkeypatch.setenv("SMTP_HOST", "smtp.test.local")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_FROM", "noreply@test.local")

    result = email_service.send_email(
        db=db_session,
        to_email="user@example.com",
        subject="Multi-part Email",
        body_html="<h1>HTML Content</h1>",
        body_text="Text Content",
        track=False,
    )

    assert result is True
    assert len(fake_smtp.messages) == 1
    _, _, msg = fake_smtp.messages[0]
    assert "HTML Content" in msg or "Text Content" in msg


def test_send_email_attaches_pdf_with_alternative_body(db_session, monkeypatch):
    fake_smtp = FakeSMTP()
    monkeypatch.setattr("smtplib.SMTP", lambda *args, **kwargs: fake_smtp)
    monkeypatch.setattr("smtplib.SMTP_SSL", lambda *args, **kwargs: fake_smtp)
    monkeypatch.setenv("SMTP_HOST", "smtp.test.local")
    monkeypatch.setenv("SMTP_FROM", "noreply@test.local")

    result = email_service.send_email(
        db=db_session,
        to_email="customer@example.com",
        subject="Invoice INV-1001",
        body_html="<p>Your invoice is attached.</p>",
        body_text="Your invoice is attached.",
        track=False,
        attachments=(
            email_service.EmailAttachment(
                filename="invoice-INV-1001.pdf",
                content_type="application/pdf",
                content=b"%PDF-1.4 invoice",
            ),
        ),
    )

    assert result is True
    parsed = message_from_string(fake_smtp.messages[0][2])
    assert parsed.get_content_type() == "multipart/mixed"
    parts = parsed.get_payload()
    assert parts[0].get_content_type() == "multipart/alternative"
    assert [part.get_content_type() for part in parts[0].get_payload()] == [
        "text/plain",
        "text/html",
    ]
    assert parts[1].get_content_type() == "application/pdf"
    assert parts[1].get_filename() == "invoice-INV-1001.pdf"
    assert parts[1].get_payload(decode=True) == b"%PDF-1.4 invoice"


def test_send_email_preserves_operational_headers(db_session, monkeypatch):
    fake_smtp = FakeSMTP()
    monkeypatch.setattr("smtplib.SMTP", lambda *args, **kwargs: fake_smtp)
    monkeypatch.setattr("smtplib.SMTP_SSL", lambda *args, **kwargs: fake_smtp)
    monkeypatch.setenv("SMTP_HOST", "smtp.test.local")
    monkeypatch.setenv("SMTP_FROM", "noreply@test.local")

    result = email_service.send_email(
        db=db_session,
        to_email="probe@example.com",
        subject="Probe",
        body_html="<p>Probe</p>",
        body_text="Probe",
        track=False,
        activity="observability_smtp_probe",
        headers={
            "Message-ID": "<probe-1@example.com>",
            "X-Dotmac-Probe": "team_inbox_smtp_e2e",
        },
    )

    assert result is True
    message = fake_smtp.messages[0][2]
    assert "Message-ID: <probe-1@example.com>" in message
    assert "X-Dotmac-Probe: team_inbox_smtp_e2e" in message


def test_send_email_with_tracking(db_session, monkeypatch):
    """Test sending email with notification tracking."""
    fake_smtp = FakeSMTP()

    def mock_smtp(*args, **kwargs):
        return fake_smtp

    monkeypatch.setattr("smtplib.SMTP", mock_smtp)
    monkeypatch.setattr("smtplib.SMTP_SSL", mock_smtp)
    monkeypatch.setenv("SMTP_HOST", "smtp.test.local")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_FROM", "noreply@test.local")

    # When track=True, a notification record should be created
    result = email_service.send_email(
        db=db_session,
        to_email="tracked@example.com",
        subject="Tracked Email",
        body_html="<p>Tracked content</p>",
        body_text="Tracked content",
        track=True,
    )

    assert result is True


def test_send_email_tracking_stores_text_body(db_session, monkeypatch):
    """Tracked notifications should not expose email HTML in customer feeds."""
    fake_smtp = FakeSMTP()

    def mock_smtp(*args, **kwargs):
        return fake_smtp

    monkeypatch.setattr("smtplib.SMTP", mock_smtp)
    monkeypatch.setattr("smtplib.SMTP_SSL", mock_smtp)
    monkeypatch.setenv("SMTP_HOST", "smtp.test.local")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_FROM", "noreply@test.local")

    result = email_service.send_email(
        db=db_session,
        to_email="tracked@example.com",
        subject="Tracked Email",
        body_html="<p>Your <strong>invoice</strong> is ready.</p>",
        track=True,
    )

    assert result is True
    notification = db_session.query(Notification).one()
    assert notification.body == "Your invoice is ready."


def test_sensitive_transport_tracks_outcome_without_persisting_content(
    db_session, monkeypatch
):
    fake_smtp = FakeSMTP()

    def mock_smtp(*args, **kwargs):
        return fake_smtp

    monkeypatch.setattr("smtplib.SMTP", mock_smtp)
    monkeypatch.setattr("smtplib.SMTP_SSL", mock_smtp)
    monkeypatch.setenv("SMTP_HOST", "smtp.test.local")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_FROM", "noreply@test.local")
    notification = Notification(
        channel=NotificationChannel.email,
        recipient="capability@example.com",
        subject="Complete access",
        body=None,
        status=NotificationStatus.sending,
    )
    db_session.add(notification)
    db_session.commit()
    capability = "header.sensitive-capability.signature"

    result = email_service.send_email(
        db=db_session,
        to_email=notification.recipient,
        subject="Complete access",
        body_html=f"<p>Use #{capability}</p>",
        body_text=f"Use #{capability}",
        track=False,
        notification_id=str(notification.id),
        sensitive_content=True,
    )

    assert result is True
    assert capability in fake_smtp.messages[0][2]
    db_session.refresh(notification)
    assert notification.status == NotificationStatus.delivered
    assert notification.body is None
    delivery = (
        db_session.query(NotificationDelivery)
        .filter(NotificationDelivery.notification_id == notification.id)
        .one()
    )
    assert capability not in str(delivery.response_body)


def test_sensitive_transport_redacts_provider_exception_from_state_and_logs(
    db_session, monkeypatch, caplog
):
    capability = "header.secret-from-provider.signature"

    def mock_smtp_error(*args, **kwargs):
        raise RuntimeError(f"provider echoed {capability}")

    monkeypatch.setattr("smtplib.SMTP", mock_smtp_error)
    monkeypatch.setattr("smtplib.SMTP_SSL", mock_smtp_error)
    monkeypatch.setenv("SMTP_HOST", "smtp.test.local")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_FROM", "noreply@test.local")
    notification = Notification(
        channel=NotificationChannel.email,
        recipient="capability-error@example.com",
        subject="Complete access",
        body=None,
        status=NotificationStatus.sending,
    )
    db_session.add(notification)
    db_session.commit()

    with caplog.at_level("ERROR"):
        result = email_service.send_email(
            db=db_session,
            to_email=notification.recipient,
            subject="Complete access",
            body_html=f"<p>Use #{capability}</p>",
            body_text=f"Use #{capability}",
            track=False,
            notification_id=str(notification.id),
            sensitive_content=True,
        )

    assert result is False
    db_session.refresh(notification)
    assert notification.body is None
    assert notification.last_error == "Sensitive email transport failed"
    assert capability not in caplog.text
    delivery = (
        db_session.query(NotificationDelivery)
        .filter(NotificationDelivery.notification_id == notification.id)
        .one()
    )
    assert capability not in str(delivery.response_body)


def test_get_smtp_config_from_env(monkeypatch):
    """Test getting SMTP config from environment variables."""
    monkeypatch.setenv("SMTP_HOST", "mail.example.com")
    monkeypatch.setenv("SMTP_PORT", "465")
    # Some environments may set SMTP_USERNAME; ensure this test is deterministic.
    monkeypatch.setenv("SMTP_USERNAME", "admin")
    monkeypatch.setenv("SMTP_USER", "admin")
    monkeypatch.setenv("SMTP_PASSWORD", "secret123")
    monkeypatch.setenv("SMTP_FROM_EMAIL", "sender@example.com")
    monkeypatch.setenv("SMTP_FROM", "sender@example.com")
    monkeypatch.setenv("SMTP_TLS", "true")

    config = email_service._get_smtp_config(db=None)

    assert config["host"] == "mail.example.com"
    assert config["port"] == 465
    assert config["user"] == "admin"
    assert config["password"] == "secret123"
    assert config["from_addr"] == "sender@example.com"


def test_send_email_connection_error(db_session, monkeypatch):
    """Test handling SMTP connection error."""

    def mock_smtp_error(*args, **kwargs):
        raise ConnectionRefusedError("Connection refused")

    monkeypatch.setattr("smtplib.SMTP", mock_smtp_error)
    monkeypatch.setattr("smtplib.SMTP_SSL", mock_smtp_error)
    monkeypatch.setenv("SMTP_HOST", "invalid.host")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_FROM", "noreply@test.local")

    result = email_service.send_email(
        db=db_session,
        to_email="user@example.com",
        subject="Test",
        body_html="<p>Test</p>",
        body_text="Test",
        track=False,
    )
    assert result is False


def test_send_email_auth_failure_logs(db_session, monkeypatch, caplog):
    """Test SMTP authentication failure is surfaced in logs."""

    def mock_smtp_auth_error(*args, **kwargs):
        raise smtplib.SMTPAuthenticationError(535, b"Authentication failed")

    monkeypatch.setattr("smtplib.SMTP", mock_smtp_auth_error)
    monkeypatch.setattr("smtplib.SMTP_SSL", mock_smtp_auth_error)
    monkeypatch.setenv("SMTP_HOST", "smtp.test.local")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_FROM", "noreply@test.local")

    with caplog.at_level("ERROR"):
        result = email_service.send_email(
            db=db_session,
            to_email="user@example.com",
            subject="Test",
            body_html="<p>Test</p>",
            body_text="Test",
            track=False,
        )

    assert result is False
    assert "SMTP authentication failed" in caplog.text


def test_smtp_connection_auth_failure_logs(monkeypatch, caplog):
    """Test SMTP auth failure during connection test is surfaced."""

    def mock_smtp_auth_error(*args, **kwargs):
        raise smtplib.SMTPAuthenticationError(535, b"Authentication failed")

    monkeypatch.setattr("smtplib.SMTP", mock_smtp_auth_error)
    monkeypatch.setattr("smtplib.SMTP_SSL", mock_smtp_auth_error)
    config = {
        "host": "smtp.test.local",
        "port": 587,
        "use_ssl": False,
        "use_tls": False,
        "username": "user",
        "password": "pass",
    }

    with caplog.at_level("ERROR"):
        ok, error = email_service.test_smtp_connection(config)

    assert ok is False
    assert error == "SMTP authentication failed"
    assert "SMTP authentication failed during connection test" in caplog.text


def test_get_smtp_config_uses_activity_mapped_sender(db_session):
    """Sender config should be selected from activity mapping when present."""
    notification_settings.upsert_by_key(
        db_session,
        "smtp_sender.billing.host",
        DomainSettingUpdate(
            value_type=SettingValueType.string, value_text="smtp.billing.local"
        ),
    )
    notification_settings.upsert_by_key(
        db_session,
        "smtp_sender.billing.port",
        DomainSettingUpdate(value_type=SettingValueType.integer, value_text="2525"),
    )
    notification_settings.upsert_by_key(
        db_session,
        "smtp_sender.billing.username",
        DomainSettingUpdate(
            value_type=SettingValueType.string, value_text="billing-user"
        ),
    )
    notification_settings.upsert_by_key(
        db_session,
        "smtp_sender.billing.password",
        DomainSettingUpdate(
            value_type=SettingValueType.string,
            value_text="billing-pass",
            is_secret=True,
        ),
    )
    notification_settings.upsert_by_key(
        db_session,
        "smtp_sender.billing.from_email",
        DomainSettingUpdate(
            value_type=SettingValueType.string, value_text="billing@example.com"
        ),
    )
    notification_settings.upsert_by_key(
        db_session,
        "smtp_sender.billing.use_tls",
        DomainSettingUpdate(
            value_type=SettingValueType.boolean, value_text="true", value_json=True
        ),
    )
    notification_settings.upsert_by_key(
        db_session,
        "smtp_default_sender_key",
        DomainSettingUpdate(value_type=SettingValueType.string, value_text="default"),
    )
    notification_settings.upsert_by_key(
        db_session,
        "smtp_activity_sender.billing_invoice",
        DomainSettingUpdate(value_type=SettingValueType.string, value_text="billing"),
    )

    config = email_service._get_smtp_config(db_session, activity="billing_invoice")

    assert config["sender_key"] == "billing"
    assert config["host"] == "smtp.billing.local"
    assert config["port"] == 2525
    assert config["username"] == "billing-user"
    assert config["password"] == "billing-pass"
    assert config["from_email"] == "billing@example.com"


def test_upsert_smtp_sender_updates_existing_sender_in_place(db_session):
    """Upserting the same sender key should update the existing sender profile."""
    sender_key = email_service.upsert_smtp_sender(
        db_session,
        sender_key="billing",
        host="smtp.old.local",
        port=587,
        username="mailer-old",
        password="secret-old",
        from_email="old@example.com",
        from_name="Old Sender",
        use_tls=True,
        use_ssl=False,
        is_active=True,
    )

    assert sender_key == "billing"

    sender_key = email_service.upsert_smtp_sender(
        db_session,
        sender_key="billing",
        host="smtp.new.local",
        port=2525,
        username="mailer-new",
        password="",
        from_email="new@example.com",
        from_name="New Sender",
        use_tls=False,
        use_ssl=True,
        is_active=True,
    )

    senders = email_service.list_smtp_senders(db_session)

    assert sender_key == "billing"
    assert len(senders) == 1
    assert senders[0]["sender_key"] == "billing"
    assert senders[0]["host"] == "smtp.new.local"
    assert senders[0]["port"] == 2525
    assert senders[0]["username"] == "mailer-new"
    assert senders[0]["from_email"] == "new@example.com"
    assert senders[0]["from_name"] == "New Sender"
    assert senders[0]["use_tls"] is False
    assert senders[0]["use_ssl"] is True
    assert senders[0]["has_password"] is True

    config = email_service.get_smtp_config(db_session, sender_key="billing")

    assert config["password"] == "secret-old"


def test_deactivate_smtp_sender_removes_sender_from_active_list(db_session):
    """Deactivating a sender should hide it from active sender listings."""
    email_service.upsert_smtp_sender(
        db_session,
        sender_key="billing",
        host="smtp.billing.local",
        port=587,
        username="mailer",
        password="secret",
        from_email="billing@example.com",
        from_name="Billing",
        use_tls=True,
        use_ssl=False,
        is_active=True,
    )

    assert [
        sender["sender_key"] for sender in email_service.list_smtp_senders(db_session)
    ] == ["billing"]

    email_service.deactivate_smtp_sender(db_session, "billing")

    assert email_service.list_smtp_senders(db_session) == []


def test_get_smtp_config_falls_back_to_legacy_env(monkeypatch):
    """Legacy env config should still work when no sender profiles exist."""
    monkeypatch.setenv("SMTP_HOST", "legacy.smtp.local")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_FROM_EMAIL", "legacy@example.com")

    config = email_service._get_smtp_config(db=None, activity="billing_invoice")

    assert config["host"] == "legacy.smtp.local"
    assert config["port"] == 587
    assert config["from_email"] == "legacy@example.com"


def test_send_user_invite_email_uses_company_name_and_branding_logo(
    db_session, monkeypatch
):
    """Invite email should use configured company name and branded logo."""
    captured: dict[str, str] = {}

    def fake_send_email(
        db, to_email, subject, body_html, body_text, activity, **kwargs
    ):
        captured["to_email"] = to_email
        captured["subject"] = subject
        captured["body_html"] = body_html
        captured["body_text"] = body_text
        captured["activity"] = activity
        return True

    monkeypatch.setattr(email_service, "send_email", fake_send_email)
    monkeypatch.setenv("APP_URL", "https://selfcare.dotmac.ng")

    db_session.add_all(
        [
            DomainSetting(
                domain=SettingDomain.billing,
                key="company_name",
                value_text="Dotmac Selfcare",
                value_type=SettingValueType.string,
            ),
            DomainSetting(
                domain=SettingDomain.comms,
                key="sidebar_logo_url",
                value_text="/branding/assets/logo-main.png",
                value_type=SettingValueType.string,
            ),
        ]
    )
    db_session.commit()

    result = email_service.send_user_invite_email(
        db_session,
        "invitee@example.com",
        "token-123",
        person_name="John Doe",
    )

    assert result is True
    assert captured["subject"] == "You're invited to Dotmac Selfcare"
    assert "Welcome to Dotmac Selfcare" in captured["body_html"]
    assert (
        "https://selfcare.dotmac.ng/branding/assets/logo-main.png"
        in captured["body_html"]
    )
    assert "color: #FF0000" in captured["body_html"]
    assert "color: #008000" in captured["body_html"]
    assert "<img" in captured["body_html"]
    assert "Welcome to Dotmac Selfcare." in captured["body_text"]
    assert captured["activity"] == "auth_user_invite"


def test_send_user_invite_email_can_keep_bearer_out_of_http_query(
    db_session, monkeypatch
):
    captured: dict[str, object] = {}

    def fake_send_email(
        db, to_email, subject, body_html, body_text, activity, **kwargs
    ):
        captured["body_html"] = body_html
        captured["body_text"] = body_text
        captured.update(kwargs)
        return True

    monkeypatch.setattr(email_service, "send_email", fake_send_email)
    monkeypatch.setenv("APP_URL", "https://selfcare.dotmac.io")

    result = email_service.send_user_invite_email(
        db_session,
        "invitee@example.com",
        "sensitive-token",
        action_path="/portal/auth/credential-enrollment",
        track=False,
        token_in_fragment=True,
    )

    assert result is True
    expected = (
        "https://selfcare.dotmac.io/portal/auth/credential-enrollment"
        "#token=sensitive-token"
    )
    assert expected in str(captured["body_html"])
    assert expected in str(captured["body_text"])
    assert "?token=sensitive-token" not in str(captured["body_text"])
    assert captured["track"] is False


def test_send_password_reset_email_uses_branding_logo(db_session, monkeypatch):
    """Password reset email should use branded HTML and app logo."""
    captured: dict[str, str] = {}

    def fake_send_email(
        db, to_email, subject, body_html, body_text, activity, **kwargs
    ):
        captured["subject"] = subject
        captured["body_html"] = body_html
        captured["body_text"] = body_text
        captured["activity"] = activity
        return True

    monkeypatch.setattr(email_service, "send_email", fake_send_email)
    monkeypatch.setenv("APP_URL", "https://selfcare.dotmac.ng")

    db_session.add_all(
        [
            DomainSetting(
                domain=SettingDomain.billing,
                key="company_name",
                value_text="Dotmac Selfcare",
                value_type=SettingValueType.string,
            ),
            DomainSetting(
                domain=SettingDomain.comms,
                key="sidebar_logo_url",
                value_text="/branding/assets/logo-main.png",
                value_type=SettingValueType.string,
            ),
        ]
    )
    db_session.commit()

    result = email_service.send_password_reset_email(
        db_session,
        "user@example.com",
        "reset-456",
        person_name="Jane Doe",
    )

    assert result is True
    assert captured["subject"] == "Password Reset Request"
    assert "Password Reset Request" in captured["body_html"]
    assert (
        "https://selfcare.dotmac.ng/branding/assets/logo-main.png"
        in captured["body_html"]
    )
    assert "background-color: #FF0000" in captured["body_html"]
    assert "color: #008000" in captured["body_html"]
    assert "background-color: #F4F4F9" in captured["body_html"]
    assert "email-highlight-box" in captured["body_html"]
    assert "background-color: #f8fafc" in captured["body_html"]
    assert "border: 1px solid #008000" in captured["body_html"]
    assert "border-left: 5px solid #FF0000" in captured["body_html"]
    assert 'name="color-scheme" content="light dark"' in captured["body_html"]
    assert "@media (prefers-color-scheme: dark)" in captured["body_html"]
    assert "email-muted" in captured["body_html"]
    assert "#111827" in captured["body_html"]
    assert "#d1d5db" in captured["body_html"]
    assert "border: 1px solid #ccc" not in captured["body_html"]
    assert "box-shadow:" not in captured["body_html"]
    assert "border: 2px solid #e2e2e2" not in captured["body_html"]
    assert (
        "We received a request to reset your password for Dotmac Selfcare."
        in captured["body_text"]
    )
    assert captured["activity"] == "auth_password_reset"


def test_send_password_reset_email_prefers_selfcare_domain_setting(
    db_session, monkeypatch
):
    """Customer-facing reset links should use configured selfcare domain."""
    captured: dict[str, str] = {}

    def fake_send_email(
        db, to_email, subject, body_html, body_text, activity, **kwargs
    ):
        captured["body_html"] = body_html
        captured["body_text"] = body_text
        return True

    monkeypatch.setattr(email_service, "send_email", fake_send_email)
    monkeypatch.delenv("APP_URL", raising=False)

    db_session.add(
        DomainSetting(
            domain=SettingDomain.auth,
            key="selfcare_domain",
            value_text="selfcare.dotmac.io",
            value_type=SettingValueType.string,
        )
    )
    db_session.commit()

    result = email_service.send_password_reset_email(
        db_session,
        "user@example.com",
        "reset-456",
        person_name="Jane Doe",
    )

    assert result is True
    assert (
        "https://selfcare.dotmac.io/auth/reset-password?token=reset-456"
        in captured["body_html"]
    )
    assert (
        "https://selfcare.dotmac.io/auth/reset-password?token=reset-456"
        in captured["body_text"]
    )


def test_send_user_invite_email_prefers_selfcare_domain_for_admin_login(
    db_session, monkeypatch
):
    """Admin invites should use the public selfcare host when configured."""
    captured: dict[str, str] = {}

    def fake_send_email(
        db, to_email, subject, body_html, body_text, activity, **kwargs
    ):
        captured["body_html"] = body_html
        captured["body_text"] = body_text
        return True

    monkeypatch.setattr(email_service, "send_email", fake_send_email)
    monkeypatch.setenv("APP_URL", "http://localhost:8000")

    db_session.add_all(
        [
            DomainSetting(
                domain=SettingDomain.auth,
                key="selfcare_domain",
                value_text="selfcare.dotmac.io",
                value_type=SettingValueType.string,
            ),
            DomainSetting(
                domain=SettingDomain.auth,
                key="admin_domain",
                value_text="oss.dotmac.io",
                value_type=SettingValueType.string,
            ),
        ]
    )
    db_session.commit()

    result = email_service.send_user_invite_email(
        db_session,
        "invitee@example.com",
        "token-123",
        person_name="John Doe",
        next_login_path="/auth/login?next=/admin/dashboard",
    )

    assert result is True
    assert (
        "https://selfcare.dotmac.io/auth/reset-password?token=token-123"
        in captured["body_html"]
    )
    assert (
        "next_login=%2Fauth%2Flogin%3Fnext%3D%2Fadmin%2Fdashboard"
        in captured["body_html"]
    )
    assert (
        "https://selfcare.dotmac.io/auth/reset-password?token=token-123"
        in captured["body_text"]
    )


def test_render_email_bodies_leaves_full_html_document_unwrapped():
    from app.services.email_template import render_email_bodies

    document = (
        '<!DOCTYPE html><html lang="en"><head><title>Notice</title></head>'
        "<body><p>Hi {{ customer_name }}</p></body></html>"
    )

    body_html, body_text = render_email_bodies(document, subject="Notice")

    assert body_html == document
    assert body_html.count("<html") == 1
    assert "{{ customer_name }}" in body_html
    assert "Hi {{ customer_name }}" in body_text
