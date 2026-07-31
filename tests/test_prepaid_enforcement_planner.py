"""Prepaid enforcement planning is exact, read-only, and shared by the sweep."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.models.catalog import BillingMode, SubscriptionStatus
from app.models.enforcement_lock import EnforcementReason
from app.models.notification import Notification
from app.models.subscriber import SubscriberStatus
from app.services.account_lifecycle import get_active_locks, suspend_subscription
from app.services.collections.prepaid_balance_sweep import run_prepaid_balance_sweep
from app.services.prepaid_enforcement_planner import (
    PrepaidEnforcementAction,
    PrepaidEnforcementError,
    PrepaidEnforcementReasonSource,
    candidate_prepaid_account_ids,
    candidate_prepaid_funding_account_ids,
    plan_prepaid_enforcement,
)
from tests.prepaid_funding_helpers import (
    ensure_test_prepaid_contract,
    materialize_test_prepaid_opening_balance,
)

_MONDAY_NOON = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)


def _prepare(db, account, subscription) -> None:
    account.billing_mode = BillingMode.prepaid
    account.min_balance = Decimal("100.00")
    account.splynx_customer_id = None
    account.deposit = None
    account.status = SubscriberStatus.active
    account.is_active = True
    account.billing_enabled = True
    subscription.billing_mode = BillingMode.prepaid
    subscription.status = SubscriptionStatus.active
    subscription.next_billing_at = None
    ensure_test_prepaid_contract(db, subscription)
    db.commit()
    materialize_test_prepaid_opening_balance(db, account.id, Decimal("0.00"))


def test_planner_is_permanent_and_read_only_without_control_rows(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    notice_count = (
        db_session.query(Notification)
        .filter(Notification.event_type == "prepaid_balance_enforcement")
        .count()
    )

    plan = plan_prepaid_enforcement(
        db_session,
        now=_MONDAY_NOON,
        account_ids=[str(subscriber_account.id)],
    )

    assert plan.action_counts == {"suspend": 1}
    assert plan.items[0].available_balance == Decimal("0.00")
    assert plan.items[0].required_balance >= Decimal("100.00")
    db_session.refresh(subscriber_account)
    assert subscriber_account.prepaid_low_balance_at is None
    assert (
        db_session.query(Notification)
        .filter(Notification.event_type == "prepaid_balance_enforcement")
        .count()
        == notice_count
    )


def test_funding_cohort_excludes_postpaid_timer_repair_input(
    db_session, subscriber_account, subscription
):
    subscriber_account.billing_mode = BillingMode.postpaid
    subscriber_account.status = SubscriberStatus.active
    subscriber_account.is_active = True
    subscriber_account.billing_enabled = True
    subscriber_account.prepaid_low_balance_at = _MONDAY_NOON - timedelta(days=1)
    subscription.billing_mode = BillingMode.postpaid
    subscription.status = SubscriptionStatus.active
    db_session.commit()

    assert subscriber_account.id in candidate_prepaid_account_ids(db_session)
    assert subscriber_account.id not in candidate_prepaid_funding_account_ids(
        db_session
    )
    item = plan_prepaid_enforcement(
        db_session,
        now=_MONDAY_NOON,
        account_ids=[subscriber_account.id],
    ).items[0]

    assert item.action == PrepaidEnforcementAction.clear_stale_timers
    assert item.reason == "non_prepaid_account_has_prepaid_timers"
    assert item.available_balance == Decimal("0.00")
    assert item.required_balance == Decimal("0.00")


def test_mode_change_repairs_only_the_obsolete_prepaid_lock(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    suspend_subscription(
        db_session,
        str(subscription.id),
        reason=EnforcementReason.prepaid,
        source="pytest:prepaid",
        emit=False,
    )
    suspend_subscription(
        db_session,
        str(subscription.id),
        reason=EnforcementReason.fraud,
        source="pytest:fraud",
        emit=False,
    )
    subscriber_account.billing_mode = BillingMode.postpaid
    subscription.billing_mode = BillingMode.postpaid
    db_session.commit()

    item = plan_prepaid_enforcement(
        db_session,
        now=_MONDAY_NOON,
        account_ids=[subscriber_account.id],
    ).items[0]
    assert item.action == PrepaidEnforcementAction.repair_stale_locks

    result = run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON)

    db_session.refresh(subscription)
    assert result["restored"] == 1
    assert subscription.status == SubscriptionStatus.suspended
    assert {
        lock.reason
        for lock in get_active_locks(db_session, subscription_id=str(subscription.id))
    } == {EnforcementReason.fraud}


def test_terminal_service_stale_lock_is_resolved_without_reactivation(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    suspend_subscription(
        db_session,
        str(subscription.id),
        reason=EnforcementReason.prepaid,
        source="pytest:prepaid",
        emit=False,
    )
    subscription.status = SubscriptionStatus.canceled
    db_session.commit()

    result = run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON)

    db_session.refresh(subscription)
    assert result["restored"] == 1
    assert subscription.status == SubscriptionStatus.canceled
    assert get_active_locks(db_session, subscription_id=str(subscription.id)) == []


def test_funding_cohort_excludes_service_less_prepaid_timer_repair_input(
    db_session, subscriber_account, subscription
):
    subscriber_account.billing_mode = BillingMode.prepaid
    subscriber_account.status = SubscriberStatus.active
    subscriber_account.is_active = True
    subscriber_account.billing_enabled = True
    subscriber_account.prepaid_low_balance_at = _MONDAY_NOON - timedelta(days=1)
    subscription.billing_mode = BillingMode.prepaid
    subscription.status = SubscriptionStatus.canceled
    db_session.commit()

    assert subscriber_account.id in candidate_prepaid_account_ids(db_session)
    assert subscriber_account.id not in candidate_prepaid_funding_account_ids(
        db_session
    )
    item = plan_prepaid_enforcement(
        db_session,
        now=_MONDAY_NOON,
        account_ids=[subscriber_account.id],
    ).items[0]

    assert item.action == PrepaidEnforcementAction.clear_stale_timers
    assert item.reason == "account_without_collectible_service_has_prepaid_timers"


def test_plan_reports_parent_status_drift_and_distinct_suspend_action(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    subscriber_account.status = SubscriberStatus.new
    subscriber_account.prepaid_low_balance_at = _MONDAY_NOON - timedelta(days=4)
    db_session.commit()

    item = plan_prepaid_enforcement(
        db_session,
        now=_MONDAY_NOON,
        account_ids=[subscriber_account.id],
    ).items[0]

    assert item.action == PrepaidEnforcementAction.suspend
    assert item.account_status is SubscriberStatus.new
    assert item.derived_account_status is SubscriberStatus.active
    assert item.account_status_drift is True


def test_future_anchor_without_coverage_blocks_adverse_action(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    subscription.next_billing_at = _MONDAY_NOON + timedelta(days=20)
    subscriber_account.prepaid_low_balance_at = _MONDAY_NOON - timedelta(days=4)
    db_session.commit()

    item = plan_prepaid_enforcement(
        db_session,
        now=_MONDAY_NOON,
        account_ids=[subscriber_account.id],
    ).items[0]

    assert item.action == PrepaidEnforcementAction.coverage_unresolved
    assert item.reason_source is PrepaidEnforcementReasonSource.COVERAGE
    assert item.unresolved_projection_subscription_ids == (subscription.id,)


def test_plan_classifies_financial_shield_without_mutation(
    db_session, subscriber_account, subscription, monkeypatch
):
    _prepare(db_session, subscriber_account, subscription)
    subscriber_account.prepaid_low_balance_at = _MONDAY_NOON - timedelta(days=4)
    db_session.commit()
    monkeypatch.setattr(
        "app.services.prepaid_enforcement_planner._bulk_dunning_shield_reasons",
        lambda db, ids: {subscriber_account.id: "payment proof pending review"},
    )

    item = plan_prepaid_enforcement(
        db_session,
        now=_MONDAY_NOON,
        account_ids=[subscriber_account.id],
    ).items[0]

    assert item.action == PrepaidEnforcementAction.shielded
    assert item.reason == "payment proof pending review"
    assert item.reason_source is PrepaidEnforcementReasonSource.SHIELD
    db_session.refresh(subscription)
    assert subscription.status == SubscriptionStatus.active


def test_missing_selected_account_is_a_stable_domain_failure(db_session):
    import uuid

    missing_id = uuid.uuid4()

    with pytest.raises(PrepaidEnforcementError) as captured:
        plan_prepaid_enforcement(db_session, account_ids=[missing_id])

    assert captured.value.code == "financial.prepaid_enforcement.account_not_found"
    assert captured.value.details == {"account_ids": [str(missing_id)]}


def test_plan_always_uses_materialized_funding_owner(
    db_session, subscriber_account, subscription, monkeypatch
):
    _prepare(db_session, subscriber_account, subscription)
    calls: list[str] = []

    def _funding(db, account, *, now):  # noqa: ANN001
        from app.services.access_resolution import PrepaidFundingDecision

        calls.append(str(account.id))
        return PrepaidFundingDecision(
            account_id=str(account.id),
            available_balance=Decimal("500.00"),
            required_balance=Decimal("100.00"),
            currency="NGN",
        )

    monkeypatch.setattr(
        "app.services.prepaid_enforcement_planner.resolve_prepaid_funding",
        _funding,
    )

    plan = plan_prepaid_enforcement(
        db_session,
        now=_MONDAY_NOON,
        account_ids=[subscriber_account.id],
    )

    assert plan.generated_at == _MONDAY_NOON
    assert plan.funding_owner == "financial.prepaid_funding_reconstruction"
    assert plan.funding_observed_at == _MONDAY_NOON
    assert plan.items[0].available_balance == Decimal("500.00")
    assert plan.items[0].action == PrepaidEnforcementAction.ok
    assert calls == [str(subscriber_account.id)]


def test_plan_reports_deactivation_marker_without_prepaid_lock_as_drift(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    subscriber_account.prepaid_low_balance_at = _MONDAY_NOON - timedelta(days=2)
    subscriber_account.prepaid_deactivation_at = _MONDAY_NOON - timedelta(days=1)
    db_session.commit()

    item = plan_prepaid_enforcement(
        db_session,
        now=_MONDAY_NOON,
        account_ids=[subscriber_account.id],
    ).items[0]

    assert item.action == PrepaidEnforcementAction.state_drift
    assert item.reason == "deactivation_marker_missing_prepaid_lock"


def test_sweep_does_not_mutate_enforcement_state_drift(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    subscriber_account.prepaid_low_balance_at = _MONDAY_NOON - timedelta(days=2)
    subscriber_account.prepaid_deactivation_at = _MONDAY_NOON - timedelta(days=1)
    db_session.commit()

    result = run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON)

    assert result["state_drift"] == 1
    db_session.refresh(subscriber_account)
    db_session.refresh(subscription)
    assert subscriber_account.prepaid_deactivation_at is not None
    assert subscription.status == SubscriptionStatus.active


def test_zero_grace_suspends_even_when_notice_is_fault_suppressed(
    db_session, subscriber_account, subscription, monkeypatch
):
    _prepare(db_session, subscriber_account, subscription)

    def _decisions(db, subscriptions):
        return {
            sub.id: SimpleNamespace(
                suppress_suspension_notice=True,
                reason="open_infrastructure_down_ticket",
            )
            for sub in subscriptions
        }

    monkeypatch.setattr(
        "app.services.prepaid_enforcement_planner.billing_communication_decisions",
        _decisions,
    )

    result = run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON)

    assert result["suspended"] == 1
    db_session.refresh(subscriber_account)
    assert subscriber_account.prepaid_low_balance_at.replace(tzinfo=UTC) == _MONDAY_NOON
    assert (
        subscriber_account.prepaid_deactivation_at.replace(tzinfo=UTC) == _MONDAY_NOON
    )
    assert (
        db_session.query(Notification)
        .filter(Notification.event_type == "prepaid_balance_enforcement")
        .count()
        == 0
    )


def test_nonzero_grace_does_not_start_until_warning_is_queued(
    db_session, subscriber_account, subscription, monkeypatch
):
    _prepare(db_session, subscriber_account, subscription)
    subscriber_account.grace_period_days = 1
    db_session.commit()

    def _decisions(db, subscriptions):
        return {
            sub.id: SimpleNamespace(
                suppress_suspension_notice=True,
                reason="open_infrastructure_down_ticket",
            )
            for sub in subscriptions
        }

    monkeypatch.setattr(
        "app.services.prepaid_enforcement_planner.billing_communication_decisions",
        _decisions,
    )

    result = run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON)

    assert result["notice_suppressed"] == 1
    assert result["warned"] == 0
    db_session.refresh(subscriber_account)
    assert subscriber_account.prepaid_low_balance_at is None
    assert (
        db_session.query(Notification)
        .filter(Notification.event_type == "prepaid_balance_enforcement")
        .count()
        == 0
    )
