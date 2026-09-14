"""Payment-form balance derivation.

Regression: a fully-paid invoice (balance_due == 0) was treated as falsy and
fell back to invoice.total, so the record-payment form showed the full total as
"balance due" and pre-filled it — inviting a duplicate payment / overpayment.
"""

from decimal import Decimal
from types import SimpleNamespace

from app.services.web_billing_payment_forms import (
    build_new_form_state,
    invoice_balance_info,
)


def _invoice(balance_due, total, currency="NGN"):
    return SimpleNamespace(balance_due=balance_due, total=total, currency=currency)


def test_paid_invoice_shows_zero_balance_not_total():
    value, display = invoice_balance_info(
        _invoice(Decimal("0.00"), Decimal("15000.00"))
    )
    assert value == "0.00"
    assert display == "NGN 0.00"


def test_partial_balance_is_shown():
    value, _ = invoice_balance_info(_invoice(Decimal("5000.00"), Decimal("15000.00")))
    assert value == "5000.00"


def test_unset_balance_falls_back_to_total():
    # Legacy/never-computed balance_due (None) should still fall back to total.
    value, display = invoice_balance_info(_invoice(None, Decimal("15000.00")))
    assert value == "15000.00"
    assert "15,000.00" in display


def test_account_payment_can_prefill_sales_order_balance(db_session, monkeypatch):
    account_id = "46ca495f-654e-4cef-88ac-528773a72b29"
    monkeypatch.setattr(
        "app.services.web_billing_payment_forms.subscriber_service.accounts.get",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.web_billing_payment_forms.billing_service.collection_accounts.list",
        lambda **kwargs: [],
    )

    state = build_new_form_state(
        db_session,
        invoice_id=None,
        invoice_alias=None,
        account_id=account_id,
        account_alias=None,
        amount="233812.50",
    )

    assert state["prefill"]["account_id"] == account_id
    assert state["prefill"]["amount"] == Decimal("233812.50")


def test_load_invoice_currency_state_honors_default_currency_setting(db_session):
    # C-4 currency cleanup: the manual-payment currency default must come from
    # the billing.default_currency setting, not a hardcoded NGN literal.
    from app.models.domain_settings import DomainSetting, SettingDomain
    from app.models.subscription_engine import SettingValueType
    from app.services.web_billing_payment_forms import load_invoice_currency_state

    db_session.add(
        DomainSetting(
            domain=SettingDomain.billing,
            key="default_currency",
            value_type=SettingValueType.string,
            value_text="USD",
            is_active=True,
        )
    )
    db_session.commit()

    state = load_invoice_currency_state(db_session, invoice_id=None, currency=None)
    assert state["currency_value"] == "USD"
