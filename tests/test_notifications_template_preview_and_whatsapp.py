from starlette.requests import Request

from app.models.notification import NotificationChannel
from app.schemas.notification import NotificationTemplateCreate
from app.services import notification as notification_service
from app.web.admin import notifications as notifications_web


def _request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/", "headers": []})


def test_notification_template_test_uses_whatsapp_and_substitution(
    db_session, monkeypatch
):
    template = notification_service.templates.create(
        db_session,
        NotificationTemplateCreate(
            name="WA Invoice",
            code="wa_invoice",
            channel=NotificationChannel.whatsapp,
            subject=None,
            body="Hello {customer_name}, account {account_number}",
        ),
    )
    captured = {}

    def _fake_send_template_message(
        *, db, recipient, template_name, language=None, dry_run
    ):
        captured["recipient"] = recipient
        captured["template_name"] = template_name
        captured["language"] = language
        captured["dry_run"] = dry_run
        return {"ok": True, "response": "ok"}

    monkeypatch.setattr(
        "app.services.integrations.whatsapp_capability.send_template_message",
        _fake_send_template_message,
    )

    response = notifications_web.notification_template_test(
        request=_request(),
        template_id=template.id,
        test_recipient="+2348000000000",
        test_variables_json='{"customer_name":"Ada","account_number":"ACC-1"}',
        db=db_session,
    )

    assert response.status_code == 303
    # WhatsApp business sends go through approved Meta templates (by code), not
    # free text, so the test-send now invokes send_template_message.
    assert captured["recipient"] == "+2348000000000"
    assert captured["dry_run"] is False
    assert captured["template_name"] == "wa_invoice"
    assert captured["language"] is None


def test_notification_template_preview_renders_variables(db_session):
    template = notification_service.templates.create(
        db_session,
        NotificationTemplateCreate(
            name="Email Invoice",
            code="email_invoice",
            channel=NotificationChannel.email,
            subject="Account {account_number}",
            body="Hi {customer_name}",
        ),
    )

    response = notifications_web.notification_template_preview(
        request=_request(),
        template_id=template.id,
        test_variables_json='{"customer_name":"Jane","account_number":"ACC-9"}',
        db=db_session,
    )

    assert response.context["rendered_subject"] == "Account ACC-9"
    assert response.context["rendered_body"] == "Hi Jane"


def test_notification_template_unsaved_preview_renders_variables():
    response = notifications_web.notification_template_unsaved_preview(
        request=_request(),
        channel="sms",
        subject="",
        body="Hi {subscriber_name}, invoice {invoice_number}",
        test_variables_json='{"subscriber_name":"Jane","invoice_number":"INV-9"}',
    )

    assert response.context["rendered_subject"] == ""
    assert response.context["rendered_body"] == "Hi Jane, invoice INV-9"
    assert response.context["channel"] == "sms"
