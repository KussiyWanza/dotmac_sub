from pathlib import Path

import pytest

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.subscription_engine import SettingValueType
from app.services.billing_settings import resolve_payment_due_days
from app.services.settings_spec import get_spec
from app.services.web_system_config import (
    get_billing_config_context,
    save_billing_config,
)


def _setting(key: str, value: str) -> DomainSetting:
    return DomainSetting(
        domain=SettingDomain.billing,
        key=key,
        value_type=SettingValueType.integer,
        value_text=value,
        is_active=True,
    )


def test_resolve_payment_due_days_uses_canonical_key(db_session):
    db_session.add(_setting("payment_due_days", "21"))
    db_session.commit()

    assert resolve_payment_due_days(db_session) == 21


def test_resolve_payment_due_days_falls_back_to_legacy_invoice_key(db_session):
    db_session.add(_setting("invoice_due_days", "17"))
    db_session.commit()

    assert resolve_payment_due_days(db_session) == 17


def test_resolve_payment_due_days_falls_back_to_legacy_default_terms_key(db_session):
    db_session.add(_setting("default_payment_terms_days", "30"))
    db_session.commit()

    assert resolve_payment_due_days(db_session) == 30


def test_billing_config_context_backfills_payment_due_days_from_legacy_key(db_session):
    db_session.add(_setting("invoice_due_days", "9"))
    db_session.commit()

    context = get_billing_config_context(db_session)

    assert context["billing"]["payment_due_days"] == "9"


def test_billing_config_context_backfills_notification_defaults(db_session):
    context = get_billing_config_context(db_session)

    assert context["billing"]["expiry_reminder_days"] == "7"
    assert context["billing"]["invoice_reminder_days"] == "7,1"
    assert context["billing"]["minimum_balance"] == "0"
    assert context["billing"]["invoice_pdf_payment_presentment"] == "paystack"


def test_billing_policy_settings_have_specs():
    expected = {
        "minimum_balance": "0",
        "invoice_pdf_payment_presentment": "paystack",
    }

    for key, default in expected.items():
        spec = get_spec(SettingDomain.billing, key)
        assert spec is not None
        assert spec.default == default


def test_retired_access_controls_have_no_billing_specs():
    for key in (
        "auto_suspend_on_overdue",
        "suspension_grace_hours",
        "dunning_escalation_days",
        "blocking_period_days",
        "deactivation_period_days",
    ):
        assert get_spec(SettingDomain.billing, key) is None


def test_save_billing_config_normalizes_valid_policy_values(db_session):
    save_billing_config(
        db_session,
        {
            "payment_period": "Monthly",
            "billing_day": "05",
            "use_creation_date": "false",
            "payment_due_days": "14",
            "expiry_reminder_days": "7",
            "invoice_reminder_days": "7, 1",
            "minimum_balance": "10.50",
            "proforma_enabled": "false",
            "zero_total_invoices": "false",
            "invoice_caching": "true",
            "invoice_pdf_payment_presentment": "Both",
            "upcoming_charges_prepaid_amount_bands": "0-10000,10000-",
        },
    )

    context = get_billing_config_context(db_session)["billing"]

    assert context["payment_period"] == "monthly"
    assert context["billing_day"] == "5"
    assert context["invoice_reminder_days"] == "7,1"
    assert context["minimum_balance"] == "10.50"
    assert context["invoice_pdf_payment_presentment"] == "both"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("billing_day", "29", "Billing Day must be between 1 and 28."),
        ("minimum_balance", "-1", "Minimum Balance must be at least 0."),
        (
            "invoice_reminder_days",
            "7,today",
            "Invoice Reminder Days must be a comma-separated list of day numbers.",
        ),
        (
            "invoice_pdf_payment_presentment",
            "cash",
            "Invoice PDF Payment Fallback must be one of: bank_account, both, paystack.",
        ),
    ],
)
def test_save_billing_config_rejects_invalid_policy_values(
    db_session, field, value, message
):
    payload = {
        "payment_period": "monthly",
        "billing_day": "1",
        "minimum_balance": "0",
        "invoice_reminder_days": "7,1",
    }
    payload[field] = value

    with pytest.raises(ValueError, match=message):
        save_billing_config(db_session, payload)


def test_billing_settings_template_confirms_and_bounds_policy_save():
    template = Path("templates/admin/system/config/billing.html").read_text()

    assert "Save fleet-wide billing settings?" in template
    assert "button[type=submit]').disabled = true" in template
    assert "{% if error %}" in template
    assert 'name="payment_due_days"' in template
    assert 'name="minimum_balance"' in template
    assert 'name="invoice_pdf_payment_presentment"' in template
    assert "Dunning owns overdue actions" in template
    assert 'name="auto_suspend_on_overdue"' not in template
    assert 'name="suspension_grace_hours"' not in template
    assert 'name="dunning_escalation_days"' not in template
    assert 'name="blocking_period_days"' not in template
    assert 'name="deactivation_period_days"' not in template
    assert 'name="billing_enabled"' not in template
    assert 'name="send_billing_notifications"' not in template
    assert 'min="0"' in template
    assert 'max="3650"' in template
