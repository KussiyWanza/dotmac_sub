from __future__ import annotations

from uuid import uuid4

from fastapi import Request
from fastapi.responses import HTMLResponse

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.subscription_engine import SettingValueType
from app.services import domain_settings, settings_spec, web_system_settings_forms
from app.services.owner_commands import CommandContext
from app.web.admin import system as admin_system


def _context() -> CommandContext:
    return CommandContext.system(
        actor="user:pytest-settings-admin",
        scope=domain_settings.ADMIN_SETTINGS_FORM_WRITE_SCOPE,
        reason="test admin settings form update",
    )


def _spec(key: str) -> settings_spec.SettingSpec:
    spec = settings_spec.get_spec(SettingDomain.billing, key)
    assert spec is not None
    return spec


def test_blank_optional_notice_days_and_two_million_topup_are_accepted(db_session):
    topup = _spec("topup_max_amount")
    notice_days = _spec("renewal_invoice_notice_days")

    errors = web_system_settings_forms.upsert_settings_from_specs(
        db=db_session,
        form={topup.key: "2000000", notice_days.key: ""},
        specs=[topup, notice_days],
        service=domain_settings.billing_settings,
        context=_context(),
    )

    assert errors == []
    saved_topup = domain_settings.billing_settings.get_by_key(db_session, topup.key)
    assert saved_topup.value_text == "2000000"
    assert (
        domain_settings.billing_settings.get_optional_by_key(
            db_session,
            notice_days.key,
        )
        is None
    )


def test_invalid_later_field_does_not_save_an_earlier_setting(db_session):
    topup = _spec("topup_max_amount")
    notice_days = _spec("renewal_invoice_notice_days")
    db_session.add(
        DomainSetting(
            domain=SettingDomain.billing,
            key=topup.key,
            value_type=SettingValueType.integer,
            value_text="500000",
            is_active=True,
        )
    )
    db_session.commit()

    errors = web_system_settings_forms.upsert_settings_from_specs(
        db=db_session,
        form={topup.key: "2000000", notice_days.key: "not-a-number"},
        specs=[topup, notice_days],
        service=domain_settings.billing_settings,
        context=_context(),
    )

    assert errors == ["renewal_invoice_notice_days: Value must be an integer"]
    saved_topup = domain_settings.billing_settings.get_by_key(db_session, topup.key)
    assert saved_topup.value_text == "500000"


def test_existing_blank_secret_remains_unchanged(db_session):
    secret = settings_spec.get_spec(SettingDomain.notification, "smtp_password")
    assert secret is not None
    db_session.add(
        DomainSetting(
            domain=SettingDomain.notification,
            key=secret.key,
            value_type=SettingValueType.string,
            value_text="enc:test:preserved",
            is_secret=True,
            is_active=True,
        )
    )
    db_session.commit()

    errors = web_system_settings_forms.upsert_settings_from_specs(
        db=db_session,
        form={secret.key: ""},
        specs=[secret],
        service=domain_settings.notification_settings,
        context=_context(),
    )

    assert errors == []
    saved = domain_settings.notification_settings.get_by_key(db_session, secret.key)
    assert saved.value_text == "enc:test:preserved"


def test_boolean_setting_keeps_checkbox_handling(db_session):
    enabled = _spec("renewal_invoice_notice_enabled")

    errors = web_system_settings_forms.upsert_settings_from_specs(
        db=db_session,
        form={enabled.key: "on"},
        specs=[enabled],
        service=domain_settings.billing_settings,
        context=_context(),
    )

    assert errors == []
    saved = domain_settings.billing_settings.get_by_key(db_session, enabled.key)
    assert saved.value_text == "true"
    assert saved.value_json is True


def test_validation_error_returns_form_response_instead_of_500(
    db_session,
    monkeypatch,
):
    topup = _spec("topup_max_amount")
    notice_days = _spec("renewal_invoice_notice_days")
    monkeypatch.setattr(
        web_system_settings_forms.settings_spec,
        "list_specs",
        lambda _domain: [topup, notice_days],
    )
    monkeypatch.setattr(
        web_system_settings_forms.web_system_settings_views_service,
        "build_settings_context",
        lambda _db, domain: {"selected_domain": domain},
    )
    monkeypatch.setattr(
        admin_system.web_system_settings_views_service,
        "build_settings_page_context",
        lambda request, _db, *, settings_context, extra=None: {
            "request": request,
            **settings_context,
            **(extra or {}),
        },
    )
    monkeypatch.setattr(
        admin_system.templates,
        "TemplateResponse",
        lambda _name, context, status_code=200: HTMLResponse(
            str(context.get("errors", ())),
            status_code=status_code,
        ),
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/admin/system/settings",
            "headers": [],
            "query_string": b"",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
        }
    )
    request.state.actor_id = str(uuid4())
    request.state.auth = {"principal_type": "system_user"}

    response = admin_system.settings_update(
        request,
        domain="billing",
        form={topup.key: "2000000", notice_days.key: "not-a-number"},
        db=db_session,
    )

    assert response.status_code == 400
    assert b"Value must be an integer" in response.body
