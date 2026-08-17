"""Autopay engine — mandate + auto-charge of due invoices.

Paystack is mocked (no live keys); the charge path runs through the real billing
adapter so a recorded payment actually reduces the invoice balance.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.models.billing import (
    InvoiceDueDateBasis,
    InvoiceStatus,
    Payment,
    PaymentStatus,
)
from app.models.catalog import SubscriptionStatus
from app.schemas.billing import InvoiceCreate, PaymentMethodCreate
from app.services import autopay
from app.services import billing as billing_service
from app.services.integrations import payment_capability


@pytest.fixture(autouse=True)
def _live_service(subscription):
    """Autopay skips accounts with no live service (``account_has_live_service``).
    Every autopay test charges the ``subscriber`` account, so give it a live
    subscription (the conftest fixture creates a ``pending`` one — live but not
    auto-billed). Without this, the live-service gate would skip every charge."""
    return subscription


def _card(db_session, account_id, *, default=True):
    return billing_service.payment_methods.create(
        db_session,
        PaymentMethodCreate(
            account_id=account_id,
            label="Visa •••• 4081",
            token="AUTH_test",
            last4="4081",
            brand="visa",
            is_default=default,
        ),
    )


def _open_invoice(
    db_session,
    account_id,
    amount: Decimal,
    *,
    status=InvoiceStatus.issued,
    due_at=...,
):
    # Default to an already-due invoice so the (default-on) due-date gating
    # does not hide the invoice from the charge engine.
    if due_at is ...:
        due_at = datetime.now(UTC) - timedelta(days=1)
    issued_at = (
        datetime.now(UTC)
        if due_at is None
        else min(datetime.now(UTC), due_at - timedelta(days=1))
    )
    payload = InvoiceCreate(
        account_id=account_id,
        status=status,
        currency="NGN",
        subtotal=amount,
        total=amount,
        balance_due=amount,
        issued_at=issued_at,
        due_at=due_at,
        due_date_basis=(
            InvoiceDueDateBasis.unknown_unverified
            if due_at is None
            else InvoiceDueDateBasis.contract_terms
        ),
        due_date_basis_ref=(None if due_at is None else "test:autopay-contract"),
        due_date_policy_version=(None if due_at is None else "test-v1"),
    )
    creator = (
        billing_service.invoices.create_imported_observation
        if due_at is None
        else billing_service.invoices.create
    )
    return creator(
        db_session,
        payload,
    )


def _mock_charge(monkeypatch, *, status="success", calls=None):
    def fake(db, **kwargs):
        if calls is not None:
            calls.append(kwargs)
        return {"status": status, "reference": kwargs.get("reference")}

    monkeypatch.setattr(payment_capability, "charge_authorization", fake)


def _mock_no_recovery(monkeypatch):
    """verify_transaction finds nothing recoverable (prior attempts declined)."""
    monkeypatch.setattr(
        payment_capability,
        "verify_transaction",
        lambda db, *, provider_type, reference: {
            "status": "failed",
            "reference": reference,
        },
    )


# --- mandate management ----------------------------------------------------


def test_enable_requires_a_saved_card(db_session, subscriber):
    with pytest.raises(ValueError, match="saved card"):
        autopay.enable(db_session, str(subscriber.id))


def test_enable_then_status_then_disable(db_session, subscriber):
    card = _card(db_session, subscriber.id)
    autopay.enable(db_session, str(subscriber.id))

    status = autopay.get_status(db_session, str(subscriber.id))
    assert status["enabled"] is True
    assert status["payment_method_id"] == str(card.id)

    assert autopay.disable(db_session, str(subscriber.id)) is True
    assert autopay.get_status(db_session, str(subscriber.id))["enabled"] is False


# --- the charge engine -----------------------------------------------------


def test_run_charges_open_invoice_and_records_payment(
    db_session, subscriber, monkeypatch
):
    _card(db_session, subscriber.id)
    invoice = _open_invoice(db_session, subscriber.id, Decimal("5000.00"))
    autopay.enable(db_session, str(subscriber.id))
    _mock_charge(monkeypatch, status="success")

    result = autopay.run_account_autopay(db_session, str(subscriber.id))
    assert result["charged"] == 1
    assert result["failed"] == 0

    db_session.refresh(invoice)
    assert Decimal(str(invoice.balance_due)) == Decimal("0.00")
    payments = (
        db_session.query(Payment).filter(Payment.account_id == subscriber.id).all()
    )
    assert len(payments) == 1
    assert Decimal(str(payments[0].amount)) == Decimal("5000.00")


def test_run_does_not_record_when_charge_fails(db_session, subscriber, monkeypatch):
    _card(db_session, subscriber.id)
    invoice = _open_invoice(db_session, subscriber.id, Decimal("5000.00"))
    autopay.enable(db_session, str(subscriber.id))
    _mock_charge(monkeypatch, status="failed")

    result = autopay.run_account_autopay(db_session, str(subscriber.id))
    assert result["charged"] == 0
    assert result["failed"] == 1

    db_session.refresh(invoice)
    assert Decimal(str(invoice.balance_due)) == Decimal("5000.00")
    assert (
        db_session.query(Payment).filter(Payment.account_id == subscriber.id).count()
        == 0
    )


def test_run_skips_account_with_no_live_service(
    db_session, subscriber, subscription, monkeypatch
):
    """A terminated service must not keep capturing the saved card, even with
    an active mandate and an open balance."""
    subscription.status = SubscriptionStatus.disabled  # service terminated
    db_session.add(subscription)
    _card(db_session, subscriber.id)
    _open_invoice(db_session, subscriber.id, Decimal("5000.00"))
    autopay.enable(db_session, str(subscriber.id))
    db_session.commit()
    charge_calls: list[dict] = []
    _mock_charge(monkeypatch, calls=charge_calls)

    result = autopay.run_account_autopay(db_session, str(subscriber.id))

    assert result["skipped"] == "no_live_service"
    assert result["charged"] == 0
    assert charge_calls == []  # card was never touched
    assert (
        db_session.query(Payment).filter(Payment.account_id == subscriber.id).count()
        == 0
    )


def test_run_charges_blocked_account(db_session, subscriber, subscription, monkeypatch):
    """``blocked`` is a recoverable non-payment hold, not a dead service —
    autopay must still try to recover it (collectible), unlike truly-terminal
    statuses. Regression guard for the 2026-06-26 collections leak where the
    live-service gate excluded ``blocked`` and stopped auto-charging walled
    non-payers entirely."""
    subscription.status = SubscriptionStatus.blocked
    db_session.add(subscription)
    _card(db_session, subscriber.id)
    _open_invoice(db_session, subscriber.id, Decimal("5000.00"))
    autopay.enable(db_session, str(subscriber.id))
    db_session.commit()
    _mock_charge(monkeypatch, status="success")

    result = autopay.run_account_autopay(db_session, str(subscriber.id))

    assert result.get("skipped") != "no_live_service"
    assert result["charged"] == 1
    assert result["failed"] == 0


def test_run_noop_when_not_enabled(db_session, subscriber, monkeypatch):
    _card(db_session, subscriber.id)
    _open_invoice(db_session, subscriber.id, Decimal("1000.00"))
    _mock_charge(monkeypatch)
    result = autopay.run_account_autopay(db_session, str(subscriber.id))
    assert result.get("skipped") == "not_enabled"


def test_removing_the_card_disables_autopay(db_session, subscriber):
    from app.services import customer_portal_flow_payment_methods as cards

    card = _card(db_session, subscriber.id)
    autopay.enable(db_session, str(subscriber.id))
    assert autopay.get_status(db_session, str(subscriber.id))["enabled"] is True

    cards.remove(db_session, str(subscriber.id), str(card.id))
    assert autopay.get_status(db_session, str(subscriber.id))["enabled"] is False


def test_run_all_due_iterates_active_mandates(db_session, subscriber, monkeypatch):
    _card(db_session, subscriber.id)
    _open_invoice(db_session, subscriber.id, Decimal("2500.00"))
    autopay.enable(db_session, str(subscriber.id))
    _mock_charge(monkeypatch, status="success")

    summary = autopay.run_all_due(db_session)
    assert summary["accounts"] == 1
    assert summary["charged"] == 1


def test_rerun_does_not_double_charge(db_session, subscriber, monkeypatch):
    _card(db_session, subscriber.id)
    _open_invoice(db_session, subscriber.id, Decimal("5000.00"))
    autopay.enable(db_session, str(subscriber.id))
    _mock_charge(monkeypatch, status="success")

    first = autopay.run_account_autopay(db_session, str(subscriber.id))
    second = autopay.run_account_autopay(db_session, str(subscriber.id))
    assert first["charged"] == 1
    assert second["charged"] == 0  # invoice already settled — not re-charged
    assert (
        db_session.query(Payment).filter(Payment.account_id == subscriber.id).count()
        == 1
    )


def test_recovers_capture_when_charge_errors(db_session, subscriber, monkeypatch):
    # The charge attempt errors (e.g. a duplicate-reference error after the card
    # was already captured); recovery via verify_transaction records it instead
    # of re-charging.
    _card(db_session, subscriber.id)
    invoice = _open_invoice(db_session, subscriber.id, Decimal("5000.00"))
    autopay.enable(db_session, str(subscriber.id))

    def boom(db, **kwargs):
        raise RuntimeError("Duplicate Transaction Reference")

    monkeypatch.setattr(payment_capability, "charge_authorization", boom)
    monkeypatch.setattr(
        payment_capability,
        "verify_transaction",
        lambda db, *, provider_type, reference: {
            "status": "success",
            "reference": reference,
        },
    )

    result = autopay.run_account_autopay(db_session, str(subscriber.id))
    assert result["charged"] == 1
    db_session.refresh(invoice)
    assert Decimal(str(invoice.balance_due)) == Decimal("0.00")


# --- due-date gating ---------------------------------------------------------


def test_setting_spec_registered():
    from app.models.domain_settings import SettingDomain
    from app.services.settings_spec import get_spec

    spec = get_spec(SettingDomain.billing, "autopay_charge_only_due")
    assert spec is not None
    assert spec.env_var == "BILLING_AUTOPAY_CHARGE_ONLY_DUE"
    assert spec.default is True

    failure_spec = get_spec(SettingDomain.billing, "autopay_max_consecutive_failures")
    assert failure_spec is not None
    assert failure_spec.env_var == "BILLING_AUTOPAY_MAX_CONSECUTIVE_FAILURES"
    assert failure_spec.default == 3


def test_gating_skips_invoice_not_yet_due(db_session, subscriber, monkeypatch):
    _card(db_session, subscriber.id)
    _open_invoice(
        db_session,
        subscriber.id,
        Decimal("5000.00"),
        due_at=datetime.now(UTC) + timedelta(days=7),
    )
    autopay.enable(db_session, str(subscriber.id))
    calls: list[dict] = []
    _mock_charge(monkeypatch, status="success", calls=calls)

    result = autopay.run_account_autopay(db_session, str(subscriber.id))
    assert result["charged"] == 0
    assert result["failed"] == 0
    assert calls == []


def test_gating_skips_issued_invoice_without_due_date(
    db_session, subscriber, monkeypatch
):
    _card(db_session, subscriber.id)
    _open_invoice(db_session, subscriber.id, Decimal("5000.00"), due_at=None)
    autopay.enable(db_session, str(subscriber.id))
    calls: list[dict] = []
    _mock_charge(monkeypatch, status="success", calls=calls)

    result = autopay.run_account_autopay(db_session, str(subscriber.id))
    assert result["charged"] == 0
    assert calls == []


def test_gating_skips_unverified_overdue_invoice_without_due_date(
    db_session, subscriber, monkeypatch
):
    _card(db_session, subscriber.id)
    _open_invoice(
        db_session,
        subscriber.id,
        Decimal("5000.00"),
        status=InvoiceStatus.overdue,
        due_at=None,
    )
    autopay.enable(db_session, str(subscriber.id))
    calls: list[dict] = []
    _mock_charge(monkeypatch, status="success", calls=calls)

    result = autopay.run_account_autopay(db_session, str(subscriber.id))
    assert result["charged"] == 0
    assert calls == []


def test_gating_off_charges_at_issuance(db_session, subscriber, monkeypatch):
    _card(db_session, subscriber.id)
    _open_invoice(
        db_session,
        subscriber.id,
        Decimal("5000.00"),
        due_at=datetime.now(UTC) + timedelta(days=7),
    )
    autopay.enable(db_session, str(subscriber.id))
    monkeypatch.setattr(autopay, "_charge_only_due", lambda db: False)
    _mock_charge(monkeypatch, status="success")

    result = autopay.run_account_autopay(db_session, str(subscriber.id))
    assert result["charged"] == 1


# --- decline tracking + attempt-aware references -----------------------------


def test_decline_increments_failure_count_and_retries_with_fresh_reference(
    db_session, subscriber, monkeypatch
):
    _card(db_session, subscriber.id)
    invoice = _open_invoice(db_session, subscriber.id, Decimal("5000.00"))
    autopay.enable(db_session, str(subscriber.id))
    _mock_no_recovery(monkeypatch)

    calls: list[dict] = []
    _mock_charge(monkeypatch, status="failed", calls=calls)

    first = autopay.run_account_autopay(db_session, str(subscriber.id))
    assert first["failed"] == 1
    status = autopay.get_status(db_session, str(subscriber.id))
    assert status["failure_count"] == 1
    assert status["last_failure_at"] is not None
    assert status["last_failure_reason"]
    assert status["suspended"] is False

    second = autopay.run_account_autopay(db_session, str(subscriber.id))
    assert second["failed"] == 1
    assert autopay.get_status(db_session, str(subscriber.id))["failure_count"] == 2

    # A decline burns the reference at Paystack: each retry must use a fresh,
    # attempt-suffixed reference (attempt 0 keeps the legacy format).
    refs = [c["reference"] for c in calls]
    base = (
        f"AUTOPAY-{invoice.id}-{payment_capability.amount_to_kobo(Decimal('5000.00'))}"
    )
    assert refs == [base, f"{base}-A1"]
    assert len(set(refs)) == len(refs)


def test_mandate_skipped_after_three_consecutive_failures(
    db_session, subscriber, monkeypatch
):
    _card(db_session, subscriber.id)
    _open_invoice(db_session, subscriber.id, Decimal("5000.00"))
    autopay.enable(db_session, str(subscriber.id))
    _mock_no_recovery(monkeypatch)

    calls: list[dict] = []
    _mock_charge(monkeypatch, status="failed", calls=calls)

    for _ in range(autopay.MAX_CONSECUTIVE_FAILURES):
        autopay.run_account_autopay(db_session, str(subscriber.id))
    assert len(calls) == autopay.MAX_CONSECUTIVE_FAILURES

    status = autopay.get_status(db_session, str(subscriber.id))
    assert status["failure_count"] == autopay.MAX_CONSECUTIVE_FAILURES
    assert status["suspended"] is True

    result = autopay.run_account_autopay(db_session, str(subscriber.id))
    assert result.get("skipped") == "too_many_failures"
    assert len(calls) == autopay.MAX_CONSECUTIVE_FAILURES  # no further charges


def test_mandate_failure_cap_can_be_configured(db_session, subscriber, monkeypatch):
    from app.models.domain_settings import DomainSetting, SettingDomain
    from app.models.subscription_engine import SettingValueType

    db_session.add(
        DomainSetting(
            domain=SettingDomain.billing,
            key="autopay_max_consecutive_failures",
            value_type=SettingValueType.integer,
            value_text="2",
            is_active=True,
        )
    )
    db_session.commit()
    _card(db_session, subscriber.id)
    _open_invoice(db_session, subscriber.id, Decimal("5000.00"))
    autopay.enable(db_session, str(subscriber.id))
    _mock_no_recovery(monkeypatch)

    calls: list[dict] = []
    _mock_charge(monkeypatch, status="failed", calls=calls)

    for _ in range(2):
        autopay.run_account_autopay(db_session, str(subscriber.id))

    status = autopay.get_status(db_session, str(subscriber.id))
    assert status["failure_count"] == 2
    assert status["suspended"] is True

    result = autopay.run_account_autopay(db_session, str(subscriber.id))
    assert result.get("skipped") == "too_many_failures"
    assert len(calls) == 2


def test_success_resets_failure_count(db_session, subscriber, monkeypatch):
    _card(db_session, subscriber.id)
    _open_invoice(db_session, subscriber.id, Decimal("5000.00"))
    autopay.enable(db_session, str(subscriber.id))
    _mock_no_recovery(monkeypatch)

    _mock_charge(monkeypatch, status="failed")
    autopay.run_account_autopay(db_session, str(subscriber.id))
    assert autopay.get_status(db_session, str(subscriber.id))["failure_count"] == 1

    _mock_charge(monkeypatch, status="success")
    result = autopay.run_account_autopay(db_session, str(subscriber.id))
    assert result["charged"] == 1
    status = autopay.get_status(db_session, str(subscriber.id))
    assert status["failure_count"] == 0
    assert status["last_failure_at"] is None
    assert status["last_failure_reason"] is None


def test_reenable_resets_failure_count(db_session, subscriber, monkeypatch):
    _card(db_session, subscriber.id)
    _open_invoice(db_session, subscriber.id, Decimal("5000.00"))
    autopay.enable(db_session, str(subscriber.id))
    _mock_no_recovery(monkeypatch)
    _mock_charge(monkeypatch, status="failed")

    for _ in range(autopay.MAX_CONSECUTIVE_FAILURES):
        autopay.run_account_autopay(db_session, str(subscriber.id))
    assert autopay.get_status(db_session, str(subscriber.id))["suspended"] is True

    autopay.enable(db_session, str(subscriber.id))
    status = autopay.get_status(db_session, str(subscriber.id))
    assert status["failure_count"] == 0
    assert status["suspended"] is False


def test_new_default_card_resets_failure_count(db_session, subscriber, monkeypatch):
    from app.services import customer_portal_flow_payment_methods as cards

    _card(db_session, subscriber.id)
    other = _card(db_session, subscriber.id, default=False)
    _open_invoice(db_session, subscriber.id, Decimal("5000.00"))
    autopay.enable(db_session, str(subscriber.id))
    _mock_no_recovery(monkeypatch)
    _mock_charge(monkeypatch, status="failed")
    autopay.run_account_autopay(db_session, str(subscriber.id))
    assert autopay.get_status(db_session, str(subscriber.id))["failure_count"] == 1

    cards.set_default(db_session, str(subscriber.id), str(other.id))
    assert autopay.get_status(db_session, str(subscriber.id))["failure_count"] == 0


def test_failed_charge_notifies_customer(db_session, subscriber, monkeypatch):
    _card(db_session, subscriber.id)
    invoice = _open_invoice(db_session, subscriber.id, Decimal("5000.00"))
    autopay.enable(db_session, str(subscriber.id))
    _mock_no_recovery(monkeypatch)
    _mock_charge(monkeypatch, status="failed")

    emitted: list[tuple] = []

    def fake_emit(db, event_type, payload, **kwargs):
        emitted.append((event_type, payload, kwargs))

    monkeypatch.setattr(autopay, "emit_event", fake_emit)
    autopay.run_account_autopay(db_session, str(subscriber.id))

    assert len(emitted) == 1
    event_type, payload, kwargs = emitted[0]
    from app.services.events.types import EventType

    assert event_type == EventType.payment_failed
    assert payload["source"] == "autopay"
    assert payload["invoice_id"] == str(invoice.id)
    assert kwargs["account_id"] == invoice.account_id


# --- idempotency across attempts ---------------------------------------------


def test_no_double_charge_when_succeeded_autopay_payment_exists(
    db_session, subscriber, monkeypatch
):
    """A succeeded autopay payment for this (invoice, amount) — at any attempt —
    blocks re-charging even if the invoice balance was never reduced."""
    _card(db_session, subscriber.id)
    invoice = _open_invoice(db_session, subscriber.id, Decimal("5000.00"))
    autopay.enable(db_session, str(subscriber.id))

    kobo = payment_capability.amount_to_kobo(Decimal("5000.00"))
    db_session.add(
        Payment(
            account_id=subscriber.id,
            amount=Decimal("5000.00"),
            currency="NGN",
            status=PaymentStatus.succeeded,
            external_id=f"AUTOPAY-{invoice.id}-{kobo}",  # legacy attempt-0 ref
        )
    )
    db_session.commit()

    calls: list[dict] = []
    _mock_charge(monkeypatch, status="success", calls=calls)

    result = autopay.run_account_autopay(db_session, str(subscriber.id))
    assert result["charged"] == 0
    assert result["failed"] == 0
    assert calls == []


def test_recovers_prior_attempt_capture_before_recharging(
    db_session, subscriber, monkeypatch
):
    """If an earlier attempt actually captured at the provider (but was never
    recorded), the next run records THAT transaction instead of charging again."""
    _card(db_session, subscriber.id)
    invoice = _open_invoice(db_session, subscriber.id, Decimal("5000.00"))
    autopay.enable(db_session, str(subscriber.id))
    _mock_no_recovery(monkeypatch)

    calls: list[dict] = []
    _mock_charge(monkeypatch, status="failed", calls=calls)
    autopay.run_account_autopay(db_session, str(subscriber.id))
    assert autopay.get_status(db_session, str(subscriber.id))["failure_count"] == 1

    # The "failed" attempt 0 turns out to have captured at Paystack.
    monkeypatch.setattr(
        payment_capability,
        "verify_transaction",
        lambda db, *, provider_type, reference: {
            "status": "success",
            "reference": reference,
        },
    )
    result = autopay.run_account_autopay(db_session, str(subscriber.id))
    assert result["charged"] == 1
    assert len(calls) == 1  # no second capture
    db_session.refresh(invoice)
    assert Decimal(str(invoice.balance_due)) == Decimal("0.00")

    kobo = payment_capability.amount_to_kobo(Decimal("5000.00"))
    payment = (
        db_session.query(Payment)
        .filter(Payment.external_id == f"AUTOPAY-{invoice.id}-{kobo}")
        .one()
    )
    assert payment.status == PaymentStatus.succeeded
