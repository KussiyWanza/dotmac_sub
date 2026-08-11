"""The credit-invariant total must be decomposable into what actually failed.

`account_credit_invariant_violations{scope="all"}` sums seven unrelated
defects: unapplied credit while an invoice is payable is a customer-visible
billing error, an over-allocated payment or a duplicate provider reference is
money correctness, an unresolved deposit webhook is an integration gap. As a
single integer it says something is wrong and nothing about what, so the first
question an operator asks cannot be answered from the metric.
"""

from __future__ import annotations

from decimal import Decimal

from app.services.billing_health import (
    BillingHealthSnapshot,
    billing_health_observations,
)

_INVARIANTS = (
    "eligible_invoice_with_unused_credit",
    "payment_overallocated",
    "negative_payment_credit_source_availability",
    "paid_invoice_underfunded",
    "settled_deposit_without_exact_payment",
    "duplicate_provider_reference",
    "deposit_webhook_unresolved",
)


def _snapshot(**kwargs) -> BillingHealthSnapshot:
    base = dict(
        paid_with_balance_count=0,
        paid_with_balance_total=Decimal("0.00"),
        last_scanned=0,
        eligible_active_subs=0,
        scan_ratio=None,
        payments_24h=0,
        payments_7d_daily_avg=0.0,
        payment_volume_ratio=None,
        payment_volume_collapsed=False,
    )
    base.update(kwargs)
    return BillingHealthSnapshot(**base)


def _scoped(observations, signal):
    return {
        obs.scope: obs.value
        for obs in observations
        if getattr(obs, "signal", None) == signal
    }


def test_each_invariant_is_published_under_its_own_scope():
    breakdown = {name: index + 1 for index, name in enumerate(_INVARIANTS)}
    snapshot = _snapshot(
        account_credit_invariant_count=sum(breakdown.values()),
        account_credit_invariant_breakdown=breakdown,
    )

    scoped = _scoped(
        billing_health_observations(snapshot), "account_credit_invariant_violations"
    )

    for name, expected in breakdown.items():
        assert scoped.get(name) == expected, f"{name} not published under its own scope"


def test_the_breakdown_accounts_for_the_whole_total():
    """A scope that does not sum to `all` would send operators hunting a defect
    that is not in any of the named buckets."""
    breakdown = dict.fromkeys(_INVARIANTS, 3)
    snapshot = _snapshot(
        account_credit_invariant_count=sum(breakdown.values()),
        account_credit_invariant_breakdown=breakdown,
    )

    scoped = _scoped(
        billing_health_observations(snapshot), "account_credit_invariant_violations"
    )

    assert scoped["all"] == sum(breakdown[name] for name in _INVARIANTS)


def test_opening_balance_stays_a_separate_scope():
    """The Splynx handoff carry-in is not a live violation.

    No reconciler can allocate invoices carried in already settled, so it is
    observed for boundary visibility and must never be summed into `all` —
    in production it is 13,312 against 43 live violations.
    """
    snapshot = _snapshot(
        account_credit_invariant_count=43,
        account_credit_invariant_opening_balance_count=13312,
        account_credit_invariant_breakdown=dict.fromkeys(_INVARIANTS, 0),
    )

    scoped = _scoped(
        billing_health_observations(snapshot), "account_credit_invariant_violations"
    )

    assert scoped["all"] == 43
    assert scoped["opening_balance"] == 13312
