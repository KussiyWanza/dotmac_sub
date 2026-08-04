"""Tests for the balance/expiry-based prepaid enforcement sweep.

This sweep SUSPENDS customers, so every account-level safety transition is
covered here: arm+warn, configured grace, suspension, recovery, incomplete
opening sources, financial shields, idempotent re-runs, and the shared daily
time window.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.models.billing import (
    Invoice,
    InvoiceLine,
    InvoiceStatus,
)
from app.models.catalog import BillingMode, Subscription, SubscriptionStatus
from app.models.domain_settings import DomainSetting, SettingDomain, SettingValueType
from app.models.enforcement_lock import EnforcementLock, EnforcementReason
from app.models.notification import Notification
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services.account_lifecycle import suspend_subscription
from app.services.collections.prepaid_balance_sweep import run_prepaid_balance_sweep
from app.services.service_entitlements import (
    ensure_prepaid_entitlements_for_paid_invoice,
)
from tests.prepaid_funding_helpers import (
    TEST_PREPAID_POSITION_AT,
    ensure_test_prepaid_contract,
    materialize_test_prepaid_opening_balance,
)

# A fixed weekday noon (UTC). 2026-07-06 = Monday.
_MONDAY_NOON = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)
_SATURDAY_NOON = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)


def _record_retired_control_rows(db, **_ignored) -> None:
    """Keep legacy rows in fixtures to prove runtime behavior ignores them."""
    db.add_all(
        [
            DomainSetting(
                domain=SettingDomain.modules,
                key="collections_prepaid_balance_enforcement",
                value_type=SettingValueType.boolean,
                value_text="false",
                value_json=False,
                is_active=True,
            ),
            DomainSetting(
                domain=SettingDomain.modules,
                key="billing_prepaid_service_renewals",
                value_type=SettingValueType.boolean,
                value_text="false",
                value_json=False,
                is_active=True,
            ),
        ]
    )
    db.commit()


def _set_collections_setting(
    db, key: str, *, text=None, json_value=None, vtype
) -> None:
    db.add(
        DomainSetting(
            domain=SettingDomain.collections,
            key=key,
            value_type=vtype,
            value_text=text,
            value_json=json_value,
            is_active=True,
        )
    )
    db.commit()


def _set_billing_setting(db, key: str, *, text: str, vtype) -> None:
    db.add(
        DomainSetting(
            domain=SettingDomain.billing,
            key=key,
            value_type=vtype,
            value_text=text,
            is_active=True,
        )
    )
    db.commit()


def _make_prepaid(db, account, subscription, *, credit: Decimal, min_balance="100.00"):
    account.billing_mode = BillingMode.prepaid
    account.splynx_customer_id = None
    account.deposit = None
    account.min_balance = Decimal(min_balance)
    account.email = account.email or "prepaid@example.com"
    account.status = SubscriberStatus.active
    account.is_active = True
    account.billing_enabled = True
    subscription.status = SubscriptionStatus.active
    subscription.billing_mode = BillingMode.prepaid
    ensure_test_prepaid_contract(db, subscription)
    db.commit()
    materialize_test_prepaid_opening_balance(db, account.id, credit)


def _notices(db, account):
    return (
        db.query(Notification)
        .filter(Notification.event_type == "prepaid_balance_enforcement")
        .filter(Notification.recipient == account.email)
        .all()
    )


def _prepaid_locks(db, subscription):
    return (
        db.query(EnforcementLock)
        .filter(EnforcementLock.subscription_id == subscription.id)
        .filter(EnforcementLock.reason == EnforcementReason.prepaid)
        .filter(EnforcementLock.is_active.is_(True))
        .all()
    )


# ---------------------------------------------------------------------------
# Retired control rows cannot stop the owner
# ---------------------------------------------------------------------------


def test_missing_control_rows_do_not_disable_sweep(
    db_session, subscriber_account, subscription
):
    _make_prepaid(db_session, subscriber_account, subscription, credit=Decimal("0"))

    result = run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON)

    assert result["suspended"] == 1
    db_session.refresh(subscriber_account)
    db_session.refresh(subscription)
    assert subscriber_account.prepaid_low_balance_at is not None
    assert subscriber_account.prepaid_deactivation_at is not None
    assert subscription.status == SubscriptionStatus.suspended


def test_missing_contract_price_is_typed_and_never_suspends(
    db_session, subscriber_account, subscription
):
    _make_prepaid(db_session, subscriber_account, subscription, credit=Decimal("0"))
    subscription.unit_price = None
    db_session.commit()

    result = run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON)

    db_session.refresh(subscriber_account)
    db_session.refresh(subscription)
    assert result["renewal_terms_unresolved"] == 1
    assert result["suspended"] == 0
    assert subscription.status == SubscriptionStatus.active
    assert subscriber_account.prepaid_low_balance_at is None
    assert subscriber_account.prepaid_deactivation_at is None
    assert _prepaid_locks(db_session, subscription) == []
    assert _notices(db_session, subscriber_account) == []


def test_subscription_account_mode_mismatch_blocks_prepaid_enforcement(
    db_session, subscriber_account, subscription
):
    _make_prepaid(db_session, subscriber_account, subscription, credit=Decimal("0"))
    subscriber_account.billing_mode = BillingMode.postpaid
    db_session.commit()
    _record_retired_control_rows(db_session)

    result = run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON)

    db_session.refresh(subscriber_account)
    assert result["billing_profile_invalid"] == 1
    assert result["warned"] == 0
    assert subscriber_account.prepaid_low_balance_at is None


def test_prepaid_account_with_postpaid_subscription_is_reviewed_as_mismatch(
    db_session, subscriber_account, subscription
):
    _make_prepaid(db_session, subscriber_account, subscription, credit=Decimal("0"))
    subscription.billing_mode = BillingMode.postpaid
    db_session.commit()
    _record_retired_control_rows(db_session)

    result = run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON)

    assert result["accounts_scanned"] == 1
    assert result["billing_profile_invalid"] == 1
    db_session.refresh(subscription)
    assert subscription.status == SubscriptionStatus.active


def test_sweep_never_processes_incomplete_source_account_for_money_action(
    db_session, subscriber_account, subscription, monkeypatch
):
    from app.services.collections import prepaid_balance_sweep as sweep_module

    position_at = TEST_PREPAID_POSITION_AT
    subscriber_account.billing_mode = BillingMode.prepaid
    subscriber_account.status = SubscriberStatus.active
    subscriber_account.is_active = True
    subscriber_account.billing_enabled = True
    subscriber_account.min_balance = Decimal("100.00")
    subscriber_account.created_at = position_at - timedelta(days=1)
    subscriber_account.prepaid_low_balance_at = _MONDAY_NOON - timedelta(days=30)
    subscription.billing_mode = BillingMode.prepaid
    subscription.status = SubscriptionStatus.active

    verified_account = Subscriber(
        first_name="Verified",
        last_name="Funding",
        email="verified-prepaid-funding@example.com",
        billing_mode=BillingMode.prepaid,
        status=SubscriberStatus.active,
        is_active=True,
        billing_enabled=True,
        min_balance=Decimal("100.00"),
        reseller_id=subscriber_account.reseller_id,
        created_at=position_at - timedelta(days=1),
    )
    db_session.add(verified_account)
    db_session.flush()
    verified_subscription = Subscription(
        subscriber_id=verified_account.id,
        offer_id=subscription.offer_id,
        billing_mode=BillingMode.prepaid,
        status=SubscriptionStatus.active,
        unit_price=Decimal("100.00"),
        created_at=position_at - timedelta(days=1),
    )
    db_session.add(verified_subscription)
    db_session.commit()
    materialize_test_prepaid_opening_balance(
        db_session,
        verified_account.id,
        Decimal("500.00"),
        position_at=position_at,
    )
    _record_retired_control_rows(db_session)

    processed_ids = set()
    original_process_account = sweep_module._process_account

    def _track_processed_account(db, account, now, cfg, **kwargs):  # noqa: ANN001
        processed_ids.add(account.id)
        return original_process_account(db, account, now, cfg, **kwargs)

    monkeypatch.setattr(
        sweep_module,
        "_process_account",
        _track_processed_account,
    )

    result = run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON)

    assert result["accounts_scanned"] == 2
    assert result["funding_quarantined"] == 1
    assert processed_ids == {verified_account.id}
    db_session.refresh(subscriber_account)
    db_session.refresh(subscription)
    observed_low_at = subscriber_account.prepaid_low_balance_at
    assert observed_low_at is not None
    if observed_low_at.tzinfo is None:
        observed_low_at = observed_low_at.replace(tzinfo=UTC)
    assert observed_low_at == _MONDAY_NOON - timedelta(days=30)
    assert subscriber_account.prepaid_deactivation_at is None
    assert subscription.status == SubscriptionStatus.active
    assert _prepaid_locks(db_session, subscription) == []
    assert _notices(db_session, subscriber_account) == []


def test_existing_configured_grace_timer_is_not_reset(
    db_session, subscriber_account, subscription
):
    _record_retired_control_rows(db_session)
    _make_prepaid(db_session, subscriber_account, subscription, credit=Decimal("0"))
    subscriber_account.grace_period_days = 1
    subscriber_account.prepaid_low_balance_at = _MONDAY_NOON - timedelta(days=90)
    db_session.commit()

    first = run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON)

    assert first["suspended"] == 1
    db_session.refresh(subscription)
    assert subscription.status == SubscriptionStatus.suspended


def test_funded_restoration_does_not_require_legacy_readiness(
    db_session, subscriber_account, subscription
):
    _make_prepaid(
        db_session,
        subscriber_account,
        subscription,
        credit=Decimal("500.00"),
    )
    suspend_subscription(
        db_session,
        str(subscription.id),
        reason=EnforcementReason.prepaid,
        source="prepaid_balance_sweep",
    )
    subscriber_account.prepaid_low_balance_at = _MONDAY_NOON - timedelta(days=30)
    subscriber_account.prepaid_deactivation_at = _MONDAY_NOON - timedelta(days=29)
    db_session.commit()
    _record_retired_control_rows(db_session)

    result = run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON)

    assert result["restored"] == 1
    db_session.refresh(subscriber_account)
    db_session.refresh(subscription)
    assert subscriber_account.prepaid_low_balance_at is None
    assert subscriber_account.prepaid_deactivation_at is None
    assert subscription.status == SubscriptionStatus.active


# ---------------------------------------------------------------------------
# Low balance, configured zero grace → suspend on the first eligible sweep
# ---------------------------------------------------------------------------


def test_zero_grace_first_run_suspends_immediately(
    db_session, subscriber_account, subscription
):
    _record_retired_control_rows(db_session)
    _make_prepaid(db_session, subscriber_account, subscription, credit=Decimal("0"))

    result = run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON)

    assert result["warned"] == 0
    assert result["suspended"] == 1
    db_session.refresh(subscriber_account)
    db_session.refresh(subscription)
    assert subscriber_account.prepaid_low_balance_at is not None
    assert subscriber_account.prepaid_deactivation_at is not None
    assert subscription.status == SubscriptionStatus.suspended
    notices = _notices(db_session, subscriber_account)
    assert len(notices) == 1
    assert notices[0].subject == "Service Deactivated"


def test_nonzero_configured_grace_warns_without_first_sweep_suspension(
    db_session, subscriber_account, subscription
):
    _record_retired_control_rows(db_session)
    _set_billing_setting(
        db_session,
        "prepaid_default_grace_period_days",
        text="2",
        vtype=SettingValueType.integer,
    )
    _make_prepaid(db_session, subscriber_account, subscription, credit=Decimal("0"))

    result = run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON)

    assert result["warned"] == 1
    assert result["suspended"] == 0
    db_session.refresh(subscription)
    assert subscription.status == SubscriptionStatus.active


def test_positive_but_insufficient_wallet_suspends_when_current_period_unfunded(
    db_session, subscriber_account, subscription
):
    _record_retired_control_rows(db_session)
    _make_prepaid(
        db_session,
        subscriber_account,
        subscription,
        credit=Decimal("543.00"),
        min_balance="0.00",
    )
    subscription.unit_price = Decimal("17500.00")
    db_session.commit()

    result = run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON)

    db_session.refresh(subscriber_account)
    db_session.refresh(subscription)
    assert result["warned"] == 0
    assert result["suspended"] == 1
    assert subscriber_account.prepaid_low_balance_at is not None
    assert subscription.status == SubscriptionStatus.suspended


def test_low_wallet_does_not_warn_when_prepaid_period_is_already_funded(
    db_session, subscriber_account, subscription
):
    _record_retired_control_rows(db_session)
    _make_prepaid(
        db_session,
        subscriber_account,
        subscription,
        credit=Decimal("0.00"),
        min_balance="0.00",
    )
    subscription.unit_price = Decimal("17500.00")
    invoice = Invoice(
        account_id=subscriber_account.id,
        invoice_number="INV-FUNDED-PREPAID-PERIOD",
        status=InvoiceStatus.paid,
        total=Decimal("17500.00"),
        balance_due=Decimal("0.00"),
        billing_period_start=_MONDAY_NOON - timedelta(days=10),
        billing_period_end=_MONDAY_NOON + timedelta(days=20),
        issued_at=_MONDAY_NOON - timedelta(days=10),
        paid_at=_MONDAY_NOON - timedelta(days=10),
    )
    db_session.add(invoice)
    db_session.flush()
    db_session.add(
        InvoiceLine(
            invoice_id=invoice.id,
            subscription_id=subscription.id,
            description="Funded prepaid renewal",
            quantity=Decimal("1.000"),
            unit_price=Decimal("17500.00"),
            amount=Decimal("17500.00"),
            metadata_={"kind": "base_subscription"},
        )
    )
    db_session.commit()
    ensure_prepaid_entitlements_for_paid_invoice(db_session, invoice)
    db_session.commit()

    result = run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON)

    db_session.refresh(subscriber_account)
    db_session.refresh(subscription)
    assert result["ok"] == 1
    assert result["warned"] == 0
    assert subscriber_account.prepaid_low_balance_at is None
    assert subscription.status == SubscriptionStatus.active


def test_future_anchor_without_coverage_is_never_suspended(
    db_session, subscriber_account, subscription
):
    _record_retired_control_rows(db_session)
    _make_prepaid(
        db_session,
        subscriber_account,
        subscription,
        credit=Decimal("0.00"),
        min_balance="100.00",
    )
    subscription.unit_price = Decimal("17500.00")
    subscription.next_billing_at = _MONDAY_NOON + timedelta(days=20)
    subscriber_account.prepaid_low_balance_at = _MONDAY_NOON - timedelta(days=5)
    db_session.commit()

    result = run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON)

    db_session.refresh(subscription)
    assert result["coverage_unresolved"] == 1
    assert result["suspended"] == 0
    assert subscription.status == SubscriptionStatus.active
    assert _prepaid_locks(db_session, subscription) == []


def test_paid_invoice_without_entitlement_is_quarantined_from_suspension(
    db_session, subscriber_account, subscription
):
    _record_retired_control_rows(db_session)
    _make_prepaid(
        db_session,
        subscriber_account,
        subscription,
        credit=Decimal("0.00"),
        min_balance="100.00",
    )
    subscription.unit_price = Decimal("17500.00")
    subscription.next_billing_at = _MONDAY_NOON - timedelta(days=30)
    subscriber_account.prepaid_low_balance_at = _MONDAY_NOON - timedelta(days=5)
    invoice = Invoice(
        account_id=subscriber_account.id,
        invoice_number="INV-PAID-COVERAGE-PROJECTION-GAP",
        status=InvoiceStatus.paid,
        currency="NGN",
        subtotal=Decimal("17500.00"),
        tax_total=Decimal("0.00"),
        total=Decimal("17500.00"),
        balance_due=Decimal("0.00"),
        billing_period_start=_MONDAY_NOON - timedelta(days=3),
        billing_period_end=_MONDAY_NOON + timedelta(days=27),
        issued_at=_MONDAY_NOON - timedelta(days=3),
        paid_at=_MONDAY_NOON - timedelta(days=2),
    )
    db_session.add(invoice)
    db_session.flush()
    db_session.add(
        InvoiceLine(
            invoice_id=invoice.id,
            subscription_id=subscription.id,
            description="Paid prepaid period missing entitlement projection",
            quantity=Decimal("1.000"),
            unit_price=Decimal("17500.00"),
            amount=Decimal("17500.00"),
            metadata_={"kind": "base_subscription"},
        )
    )
    db_session.commit()

    result = run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON)

    db_session.refresh(subscription)
    assert result["coverage_unresolved"] == 1
    assert result["suspended"] == 0
    assert subscription.status == SubscriptionStatus.active
    assert _prepaid_locks(db_session, subscription) == []


def test_prepaid_legacy_invoice_ar_does_not_reduce_wallet_balance(
    db_session, subscriber_account, subscription
):
    _record_retired_control_rows(db_session)
    _make_prepaid(
        db_session, subscriber_account, subscription, credit=Decimal("100.00")
    )
    invoice = Invoice(
        account_id=subscriber_account.id,
        invoice_number="INV-PREPAID-PHANTOM-AR",
        status=InvoiceStatus.overdue,
        total=Decimal("80.00"),
        balance_due=Decimal("80.00"),
        due_at=_MONDAY_NOON - timedelta(days=10),
        metadata_={},
    )
    db_session.add(invoice)
    db_session.flush()
    db_session.add(
        InvoiceLine(
            invoice_id=invoice.id,
            subscription_id=subscription.id,
            description="Prepaid renewal",
            quantity=Decimal("1.000"),
            unit_price=Decimal("80.00"),
            amount=Decimal("80.00"),
            metadata_={"kind": "base_subscription"},
        )
    )
    db_session.commit()

    result = run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON)

    db_session.refresh(subscriber_account)
    db_session.refresh(subscription)
    assert result["ok"] == 1
    assert result["warned"] == 0
    assert subscriber_account.prepaid_low_balance_at is None
    assert subscription.status == SubscriptionStatus.active


def test_imported_line_less_prepaid_ar_does_not_reduce_wallet_balance(
    db_session, subscriber_account, subscription
):
    _record_retired_control_rows(db_session)
    _make_prepaid(
        db_session, subscriber_account, subscription, credit=Decimal("100.00")
    )
    invoice = Invoice(
        account_id=subscriber_account.id,
        invoice_number="INV-IMPORTED-PREPAID-PHANTOM-AR",
        status=InvoiceStatus.overdue,
        total=Decimal("80.00"),
        balance_due=Decimal("80.00"),
        due_at=_MONDAY_NOON - timedelta(days=10),
        metadata_={"imported_via": "system_import_wizard"},
    )
    db_session.add(invoice)
    db_session.commit()

    result = run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON)

    db_session.refresh(subscriber_account)
    db_session.refresh(subscription)
    assert result["ok"] == 1
    assert result["warned"] == 0
    assert subscriber_account.prepaid_low_balance_at is None
    assert subscription.status == SubscriptionStatus.active


@pytest.mark.parametrize("hold_value", [True, "true", "1", "yes", "on"])
def test_reconciliation_hold_invoice_does_not_reduce_wallet_balance(
    db_session, subscriber_account, subscription, hold_value
):
    _record_retired_control_rows(db_session)
    _make_prepaid(
        db_session, subscriber_account, subscription, credit=Decimal("100.00")
    )
    invoice = Invoice(
        account_id=subscriber_account.id,
        invoice_number="INV-HELD-PREPAID-AR",
        status=InvoiceStatus.overdue,
        total=Decimal("80.00"),
        balance_due=Decimal("80.00"),
        due_at=_MONDAY_NOON - timedelta(days=10),
        metadata_={"reconciliation_hold": hold_value},
    )
    db_session.add(invoice)
    db_session.commit()

    result = run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON)

    db_session.refresh(subscriber_account)
    db_session.refresh(subscription)
    assert result["ok"] == 1
    assert result["warned"] == 0
    assert subscriber_account.prepaid_low_balance_at is None
    assert subscription.status == SubscriptionStatus.active


# ---------------------------------------------------------------------------
# Deactivation window elapsed → suspend (EnforcementReason.prepaid)
# ---------------------------------------------------------------------------


def test_suspends_after_deactivation_days_elapsed(
    db_session, subscriber_account, subscription
):
    _record_retired_control_rows(db_session)
    _make_prepaid(db_session, subscriber_account, subscription, credit=Decimal("0"))
    # The default three-day warning window has elapsed. (Not "just armed" this
    # run, so the deactivation branch runs.)
    subscriber_account.prepaid_low_balance_at = _MONDAY_NOON - timedelta(days=4)
    db_session.commit()

    result = run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON)

    assert result["suspended"] == 1
    db_session.refresh(subscriber_account)
    db_session.refresh(subscription)
    assert subscriber_account.prepaid_deactivation_at is not None
    assert subscription.status == SubscriptionStatus.suspended
    locks = _prepaid_locks(db_session, subscription)
    assert len(locks) == 1
    assert locks[0].source == "prepaid_balance_sweep"
    notices = _notices(db_session, subscriber_account)
    assert any(n.subject == "Service Deactivated" for n in notices)


def test_respects_canonical_account_grace_override(
    db_session, subscriber_account, subscription
):
    _record_retired_control_rows(db_session)
    _make_prepaid(db_session, subscriber_account, subscription, credit=Decimal("0"))
    subscriber_account.grace_period_days = 5
    # Armed only 2 days ago; 5-day window has NOT elapsed → warn stays, no suspend.
    subscriber_account.prepaid_low_balance_at = _MONDAY_NOON - timedelta(days=2)
    db_session.commit()

    result = run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON)

    assert result["suspended"] == 0
    db_session.refresh(subscription)
    assert subscription.status == SubscriptionStatus.active
    assert subscriber_account.prepaid_deactivation_at is None


# ---------------------------------------------------------------------------
# Recovery → clear timers + restore
# ---------------------------------------------------------------------------


def test_funded_recovery_clears_timers_and_restores(
    db_session, subscriber_account, subscription
):
    _record_retired_control_rows(db_session)
    # Start suspended-for-prepaid with both timers set...
    _make_prepaid(
        db_session, subscriber_account, subscription, credit=Decimal("500.00")
    )
    suspend_subscription(
        db_session,
        str(subscription.id),
        reason=EnforcementReason.prepaid,
        source="prepaid_balance_sweep",
    )
    subscriber_account.prepaid_low_balance_at = _MONDAY_NOON - timedelta(days=3)
    subscriber_account.prepaid_deactivation_at = _MONDAY_NOON - timedelta(days=1)
    db_session.commit()
    assert subscription.status == SubscriptionStatus.suspended

    # ...now funded (credit 500 >= min 100).
    result = run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON)

    assert result["restored"] == 1
    db_session.refresh(subscriber_account)
    db_session.refresh(subscription)
    assert subscriber_account.prepaid_low_balance_at is None
    assert subscriber_account.prepaid_deactivation_at is None
    assert subscription.status == SubscriptionStatus.active
    assert _prepaid_locks(db_session, subscription) == []


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_rerun_does_not_rewarn_or_double_suspend(
    db_session, subscriber_account, subscription
):
    _record_retired_control_rows(db_session)
    _make_prepaid(db_session, subscriber_account, subscription, credit=Decimal("0"))
    subscriber_account.prepaid_low_balance_at = _MONDAY_NOON - timedelta(days=4)
    db_session.commit()

    first = run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON)
    assert first["suspended"] == 1
    deact_first = subscriber_account.prepaid_deactivation_at

    second = run_prepaid_balance_sweep(
        db_session, now=_MONDAY_NOON + timedelta(hours=1)
    )

    # Second pass: already suspended + deactivation armed → no new work.
    assert second["suspended"] == 0
    assert second["warned"] == 0
    db_session.refresh(subscriber_account)
    assert subscriber_account.prepaid_deactivation_at == deact_first
    assert len(_prepaid_locks(db_session, subscription)) == 1
    # Exactly one deactivation notice (not duplicated on re-run).
    deact_notices = [
        n
        for n in _notices(db_session, subscriber_account)
        if n.subject == "Service Deactivated"
    ]
    assert len(deact_notices) == 1


def test_low_balance_rerun_does_not_send_second_warning(
    db_session, subscriber_account, subscription
):
    _record_retired_control_rows(db_session)
    # A canonical account grace override keeps this focused on warning dedupe.
    subscriber_account.grace_period_days = 5
    _make_prepaid(db_session, subscriber_account, subscription, credit=Decimal("0"))

    run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON)
    run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON + timedelta(hours=6))

    warnings = [
        n
        for n in _notices(db_session, subscriber_account)
        if n.subject == "Low Balance Warning"
    ]
    assert len(warnings) == 1


# ---------------------------------------------------------------------------
# The shared daily time window defers the DEACTIVATION step (never the warning)
# ---------------------------------------------------------------------------


def test_weekend_is_an_ordinary_enforcement_day(
    db_session, subscriber_account, subscription
):
    _record_retired_control_rows(db_session)
    _make_prepaid(db_session, subscriber_account, subscription, credit=Decimal("0"))
    subscriber_account.prepaid_low_balance_at = _SATURDAY_NOON - timedelta(days=4)
    db_session.commit()

    result = run_prepaid_balance_sweep(db_session, now=_SATURDAY_NOON)

    assert result["suspended"] == 1
    db_session.refresh(subscription)
    assert subscription.status == SubscriptionStatus.suspended


def test_shared_time_window_defers_deactivation(
    db_session, subscriber_account, subscription
):
    _record_retired_control_rows(db_session)
    # Only act at/after 09:00 local (UTC in tests).
    _set_collections_setting(
        db_session,
        "enforcement_window_start",
        text="09:00",
        vtype=SettingValueType.string,
    )
    _make_prepaid(db_session, subscriber_account, subscription, credit=Decimal("0"))
    subscriber_account.prepaid_low_balance_at = _MONDAY_NOON - timedelta(days=4)
    db_session.commit()

    before_window = _MONDAY_NOON.replace(hour=7)
    result = run_prepaid_balance_sweep(db_session, now=before_window)

    assert result["deferred"] == 1
    db_session.refresh(subscription)
    assert subscription.status == SubscriptionStatus.active


def test_active_payment_arrangement_shields_prepaid_suspension(
    db_session, subscriber_account, subscription
):
    from datetime import date

    from app.models.payment_arrangement import ArrangementStatus, PaymentArrangement

    _record_retired_control_rows(db_session)
    _make_prepaid(db_session, subscriber_account, subscription, credit=Decimal("0"))
    subscriber_account.prepaid_low_balance_at = _MONDAY_NOON - timedelta(days=4)
    db_session.add(
        PaymentArrangement(
            subscriber_id=subscriber_account.id,
            status=ArrangementStatus.active,
            is_active=True,
            total_amount=Decimal("5000.00"),
            installment_amount=Decimal("2500.00"),
            installments_total=2,
            start_date=date(2026, 7, 1),
        )
    )
    db_session.commit()

    result = run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON)

    db_session.refresh(subscriber_account)
    db_session.refresh(subscription)
    assert result["suspended"] == 0
    assert subscriber_account.prepaid_deactivation_at is None
    assert subscription.status == SubscriptionStatus.active
    assert _prepaid_locks(db_session, subscription) == []


def test_submitted_payment_proof_shields_prepaid_suspension(
    db_session, subscriber_account, subscription
):
    from app.models.payment_proof import PaymentProof, PaymentProofStatus

    _record_retired_control_rows(db_session)
    _make_prepaid(db_session, subscriber_account, subscription, credit=Decimal("0"))
    subscriber_account.prepaid_low_balance_at = _MONDAY_NOON - timedelta(days=4)
    db_session.add(
        PaymentProof(
            account_id=subscriber_account.id,
            amount=Decimal("5000.00"),
            currency="NGN",
            file_path="tests/fixtures/payment-proof.pdf",
            status=PaymentProofStatus.submitted,
        )
    )
    db_session.commit()

    result = run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON)

    db_session.refresh(subscriber_account)
    db_session.refresh(subscription)
    assert result["suspended"] == 0
    assert subscriber_account.prepaid_deactivation_at is None
    assert subscription.status == SubscriptionStatus.active
    assert _prepaid_locks(db_session, subscription) == []


def test_billing_health_is_observed_but_does_not_block_prepaid_suspension(
    db_session, subscriber_account, subscription, monkeypatch
):
    from app.services import billing_enforcement_guards as guards

    _record_retired_control_rows(db_session)
    _make_prepaid(db_session, subscriber_account, subscription, credit=Decimal("0"))
    subscriber_account.prepaid_low_balance_at = _MONDAY_NOON - timedelta(days=4)
    db_session.commit()
    monkeypatch.setattr(
        guards,
        "billing_enforcement_health",
        lambda _db: guards.EnforcementHealth(
            ok=False,
            reasons=["payment_inbox_failures"],
            details={"dead_letters": 1},
        ),
    )

    result = run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON)

    db_session.refresh(subscriber_account)
    db_session.refresh(subscription)
    assert result["suspended"] == 1
    assert subscriber_account.prepaid_deactivation_at is not None
    assert subscription.status == SubscriptionStatus.suspended
    assert len(_prepaid_locks(db_session, subscription)) == 1


# ---------------------------------------------------------------------------
# Non-prepaid accounts are ignored
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", [BillingMode.postpaid])
def test_postpaid_accounts_untouched(
    db_session, subscriber_account, subscription, mode
):
    _record_retired_control_rows(db_session)
    subscriber_account.billing_mode = mode
    subscriber_account.min_balance = Decimal("100.00")
    subscription.status = SubscriptionStatus.active
    subscription.billing_mode = mode
    db_session.commit()

    result = run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON)

    assert result["accounts_scanned"] == 0
    db_session.refresh(subscription)
    assert subscription.status == SubscriptionStatus.active


def test_stale_prepaid_timer_is_repaired_after_account_leaves_cohort(
    db_session, subscriber_account, subscription
):
    _record_retired_control_rows(db_session)
    subscriber_account.status = SubscriberStatus.active
    subscriber_account.is_active = True
    subscriber_account.billing_enabled = False
    subscriber_account.billing_mode = BillingMode.postpaid
    subscriber_account.prepaid_low_balance_at = _MONDAY_NOON - timedelta(days=30)
    subscription.status = SubscriptionStatus.active
    subscription.billing_mode = BillingMode.postpaid
    db_session.commit()

    result = run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON)

    assert result["accounts_scanned"] == 1
    assert result["restored"] == 1
    db_session.refresh(subscriber_account)
    assert subscriber_account.prepaid_low_balance_at is None


def test_suspend_blocked_leaves_timer_unarmed_and_retries(
    db_session, subscriber_account, subscription, monkeypatch
):
    """If the suspend is blocked (shield / dedicated bundle → _suspend_account
    returns False), prepaid_deactivation_at must NOT be armed, so the next sweep
    re-attempts rather than short-circuiting on a set-but-not-suspended timer."""
    import app.services.collections.prepaid_balance_sweep as sweep

    _record_retired_control_rows(db_session)
    _make_prepaid(db_session, subscriber_account, subscription, credit=Decimal("0"))
    subscriber_account.prepaid_low_balance_at = _MONDAY_NOON - timedelta(days=4)
    db_session.commit()

    calls = {"n": 0}

    def _blocked(*args, **kwargs):
        calls["n"] += 1
        return False

    monkeypatch.setattr(sweep, "_suspend_account", _blocked)

    result = run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON)
    assert result["suspended"] == 0
    db_session.refresh(subscriber_account)
    db_session.refresh(subscription)
    assert subscriber_account.prepaid_deactivation_at is None
    assert subscription.status != SubscriptionStatus.suspended
    assert calls["n"] == 1

    # Next run re-attempts the suspension (timer still unarmed).
    run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON + timedelta(hours=1))
    assert calls["n"] == 2
