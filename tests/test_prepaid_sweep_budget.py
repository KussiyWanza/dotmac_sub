"""The prepaid sweep always ends cleanly inside its wall-clock budget.

Production incident 2026-07-31: the first post-release sweep run exceeded the
Celery soft time limit mid-query; the interrupt left the connection
mid-command, the per-account rollback then failed, and the whole run died
without publishing its snapshot. Work that does not fit the budget must be
deferred to the next run instead — the run itself must always finish and
report.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models.catalog import BillingMode, SubscriptionStatus
from app.models.subscriber import SubscriberStatus
from app.services.collections.prepaid_balance_sweep import run_prepaid_balance_sweep
from app.services.collections.scheduled import repair_prepaid_coverage_evidence
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
    account.grace_period_days = 1
    subscription.billing_mode = BillingMode.prepaid
    subscription.status = SubscriptionStatus.active
    subscription.next_billing_at = None
    ensure_test_prepaid_contract(db, subscription)
    db.commit()
    materialize_test_prepaid_opening_balance(db, account.id, Decimal("0.00"))


def test_exhausted_budget_defers_accounts_and_still_completes(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)

    expired = _MONDAY_NOON - timedelta(seconds=1)
    result = run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON, deadline=expired)

    # The run returned normally: nothing processed, everything deferred.
    assert result["budget_deferred"] == result["accounts_scanned"] > 0
    assert result["errors"] == 0
    assert result["warned"] == 0
    db_session.refresh(subscriber_account)
    assert subscriber_account.prepaid_low_balance_at is None


def test_open_deadline_processes_the_full_cohort(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)

    deadline = datetime.now(UTC) + timedelta(hours=1)
    result = run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON, deadline=deadline)

    assert result["budget_deferred"] == 0
    db_session.refresh(subscriber_account)
    # The low-balance account was actually processed: its timer armed.
    assert subscriber_account.prepaid_low_balance_at is not None


def test_repair_defers_chunks_past_deadline(db_session):
    expired = datetime.now(UTC) - timedelta(seconds=1)

    outcome = repair_prepaid_coverage_evidence(db_session, deadline=expired)

    # Whatever the cohort contains, nothing may be attempted past the
    # deadline; the outcome still reports normally instead of raising.
    assert outcome.entitlements_created == 0
    assert outcome.deferred_subscriptions == outcome.repairable + (
        outcome.quarantined_blocking
    )
    assert outcome.status.value in {"ok", "stale_preview"}


def test_budget_resolution_uses_registered_setting(db_session):
    # Exercises the real wrapper helper (the Celery-boundary path): a broken
    # import or unregistered key here crashed the first v7.82.1 production
    # run before any account was processed.
    from app.services.collections.scheduled import (
        _DEFAULT_SWEEP_BUDGET_SECONDS,
        _sweep_budget_seconds,
    )

    assert _sweep_budget_seconds(db_session) == _DEFAULT_SWEEP_BUDGET_SECONDS


def test_keyset_cursor_resumes_and_completes_cycle(
    db_session, subscriber_account, subscription
):
    from app.models.collections import PrepaidSweepCycleState

    _prepare(db_session, subscriber_account, subscription)

    # Run 1: budget exhausted before any account — nothing processed, the
    # cycle is open, the whole cohort remains.
    expired = _MONDAY_NOON - timedelta(seconds=1)
    r1 = run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON, deadline=expired)
    assert r1["cycle_remaining"] == r1["cycle_total"] > 0
    state = (
        db_session.query(PrepaidSweepCycleState)
        .filter_by(runner="prepaid_balance_sweep")
        .one()
    )
    assert state.cursor_key is None
    completed_before = state.cycles_completed

    # Run 2: open deadline — the cycle finishes and the cursor resets so the
    # next run starts a fresh cycle from the beginning.
    open_deadline = datetime.now(UTC) + timedelta(hours=1)
    r2 = run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON, deadline=open_deadline)
    assert r2["cycle_remaining"] == 0
    assert r2["budget_deferred"] == 0
    db_session.refresh(state)
    assert state.cursor_key is None
    assert state.cycles_completed == completed_before + 1
    assert int(r2["cycle_age_seconds"]) >= 0


def test_sweep_plans_from_batched_funding_not_per_account(
    db_session, subscriber_account, subscription, monkeypatch
):
    _prepare(db_session, subscriber_account, subscription)

    def _boom(db, account, *, now=None):  # noqa: ANN001
        raise AssertionError(
            "per-account resolve_prepaid_funding must not run when the "
            "sweep prefetch is active"
        )

    monkeypatch.setattr(
        "app.services.prepaid_enforcement_planner.resolve_prepaid_funding",
        _boom,
    )

    result = run_prepaid_balance_sweep(
        db_session,
        now=_MONDAY_NOON,
        deadline=datetime.now(UTC) + timedelta(hours=1),
    )

    assert result["errors"] == 0
    db_session.refresh(subscriber_account)
    # The account was genuinely planned (low balance -> timer armed).
    assert subscriber_account.prepaid_low_balance_at is not None
