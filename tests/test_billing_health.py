"""Billing liveness/anomaly monitoring signals."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import event

from app.models.billing import (
    BillingRun,
    BillingRunStatus,
    Invoice,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentStatus,
)
from app.models.catalog import (
    AccessType,
    BillingMode,
    CatalogOffer,
    PriceBasis,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services import billing_health
from app.services.billing_health import BillingHealthSnapshot
from tests.prepaid_funding_helpers import (
    materialize_test_prepaid_opening_balance,
    materialize_test_prepaid_opening_balances,
)


def _subscriber(
    db, status=SubscriberStatus.active, email="hmon@example.com"
) -> Subscriber:
    s = Subscriber(
        first_name="Health",
        last_name="Mon",
        email=email,
        status=status,
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _offer(db) -> CatalogOffer:
    offer = CatalogOffer(
        name="Health Offer",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_mode=BillingMode.postpaid,
    )
    db.add(offer)
    db.flush()
    return offer


def _subscription(
    db,
    subscriber,
    offer,
    status=SubscriptionStatus.active,
    billing_mode=BillingMode.postpaid,
) -> Subscription:
    s = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=status,
        billing_mode=billing_mode,
    )
    db.add(s)
    db.flush()
    return s


def _add_payments(
    db, n: int, paid_at: datetime, status=PaymentStatus.succeeded
) -> None:
    for _ in range(n):
        db.add(Payment(amount=Decimal("100"), status=status, paid_at=paid_at))
    db.commit()


# ---- paid-with-balance ----------------------------------------------------


def test_paid_with_balance_counts_only_nonzero(db_session):
    sub = _subscriber(db_session)
    db_session.add(
        Invoice(account_id=sub.id, status=InvoiceStatus.paid, balance_due=Decimal("50"))
    )
    db_session.add(
        Invoice(account_id=sub.id, status=InvoiceStatus.paid, balance_due=Decimal("0"))
    )
    # an overdue-with-balance invoice must NOT count (only status=paid does)
    db_session.add(
        Invoice(
            account_id=sub.id, status=InvoiceStatus.overdue, balance_due=Decimal("99")
        )
    )
    db_session.commit()
    count, total = billing_health.paid_with_balance(db_session)
    assert count == 1
    assert total == Decimal("50")


# ---- payment volume (real 7-day baseline) ---------------------------------


def test_payment_volume_collapse_detected(db_session):
    now = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    _add_payments(db_session, 35, now - timedelta(days=3))  # baseline: 35/7 = 5.0/day
    _add_payments(db_session, 1, now - timedelta(hours=2))  # last 24h: 1
    c24, avg7, ratio, collapsed = billing_health.payment_volume(db_session, now=now)
    assert c24 == 1
    assert avg7 == 5.0
    assert ratio == 0.2
    assert collapsed is True


def test_payment_volume_healthy_not_flagged(db_session):
    now = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    _add_payments(db_session, 35, now - timedelta(days=3))  # avg 5.0/day
    _add_payments(db_session, 4, now - timedelta(hours=2))  # ratio 0.8
    _, _, ratio, collapsed = billing_health.payment_volume(db_session, now=now)
    assert ratio == 0.8
    assert collapsed is False


def test_payment_volume_low_baseline_never_collapses(db_session):
    now = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    _add_payments(db_session, 14, now - timedelta(days=3))  # avg 2.0/day (< floor 5)
    # zero in last 24h, but baseline too low to call it a collapse
    _, avg7, ratio, collapsed = billing_health.payment_volume(db_session, now=now)
    assert avg7 == 2.0
    assert ratio == 0.0
    assert collapsed is False


def test_money_path_controls_keep_forward_schedule_and_trailing_payments_separate(
    db_session,
):
    now = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    offer = _offer(db_session)
    first = _subscriber(db_session, email="renewal-first@example.com")
    second = _subscriber(db_session, email="renewal-second@example.com")
    first_subscription = _subscription(db_session, first, offer)
    second_subscription = _subscription(db_session, second, offer)
    first_subscription.next_billing_at = now + timedelta(hours=13)
    second_subscription.next_billing_at = now + timedelta(days=2)
    db_session.add_all(
        [
            Payment(
                account_id=first.id,
                amount=Decimal("100"),
                status=PaymentStatus.succeeded,
                paid_at=now - timedelta(days=1),
                created_at=now - timedelta(days=1),
            ),
            Payment(
                account_id=first.id,
                amount=Decimal("100"),
                status=PaymentStatus.succeeded,
                paid_at=now - timedelta(days=2),
                created_at=now - timedelta(days=2),
            ),
            Payment(
                account_id=second.id,
                amount=Decimal("100"),
                status=PaymentStatus.failed,
                created_at=now - timedelta(days=1),
            ),
        ]
    )
    db_session.commit()

    (
        active_null_anchors,
        unknown_due_basis,
        legacy_null_due_basis,
        expected_by_day,
        unique_payers,
        attempts,
        succeeded,
        success_ratio,
    ) = billing_health.money_path_control_snapshot(db_session, now=now)

    assert active_null_anchors == 0
    assert unknown_due_basis == 0
    assert legacy_null_due_basis == 0
    # Lagos local midnight puts +13h on the next business day.
    assert expected_by_day["day_1"] == 1
    assert expected_by_day["day_2"] == 1
    assert unique_payers == 1
    assert attempts == 3
    assert succeeded == 2
    assert success_ratio == 2 / 3


# ---- scan coverage None-guards --------------------------------------------


def test_invoice_scan_coverage_no_runs(db_session):
    last_scanned, eligible, ratio = billing_health.invoice_scan_coverage(db_session)
    assert last_scanned is None
    assert ratio is None  # no run -> no ratio (never a false 0.0 alert)


def test_invoice_scan_ratio_from_latest_run(db_session):
    db_session.add(
        BillingRun(
            run_at=datetime.now(UTC),
            subscriptions_scanned=3,
            status=BillingRunStatus.success,
        )
    )
    db_session.commit()
    last_scanned, eligible, ratio = billing_health.invoice_scan_coverage(db_session)
    assert last_scanned == 3
    # eligible active subs is 0 in this isolated test -> ratio guarded to None
    assert ratio is None


def test_invoice_scan_coverage_counts_blocked_subscriber_subs(db_session):
    """run_invoice_cycle still bills active subs of blocked/suspended/delinquent
    accounts, so the coverage denominator must count them too (else blocked subs
    are scanned but not eligible -> ratio inflated, real cohort drop hidden)."""
    offer = _offer(db_session)
    active_acct = _subscriber(db_session, SubscriberStatus.active, "a@example.com")
    blocked_acct = _subscriber(db_session, SubscriberStatus.blocked, "b@example.com")
    suspended_acct = _subscriber(
        db_session, SubscriberStatus.suspended, "s@example.com"
    )
    delinquent = _subscriber(db_session, SubscriberStatus.delinquent, "d@example.com")
    # disabled account is NOT billable -> must be excluded
    disabled_acct = _subscriber(db_session, SubscriberStatus.disabled, "x@example.com")
    _subscription(db_session, active_acct, offer)
    _subscription(db_session, blocked_acct, offer)
    _subscription(db_session, suspended_acct, offer)
    _subscription(db_session, delinquent, offer)
    _subscription(db_session, disabled_acct, offer)
    # prepaid active sub is skipped by the cycle -> must be excluded
    prepaid_acct = _subscriber(db_session, SubscriberStatus.active, "p@example.com")
    _subscription(db_session, prepaid_acct, offer, billing_mode=BillingMode.prepaid)
    db_session.add(
        BillingRun(
            run_at=datetime.now(UTC),
            subscriptions_scanned=4,
            status=BillingRunStatus.success,
        )
    )
    db_session.commit()

    last_scanned, eligible, ratio = billing_health.invoice_scan_coverage(db_session)
    assert last_scanned == 4
    # active + blocked + suspended + delinquent (non-prepaid) = 4; disabled and
    # prepaid excluded.
    assert eligible == 4
    assert ratio == 1.0


# ---- anomaly thresholds (pure) --------------------------------------------


def _snap(**kw) -> BillingHealthSnapshot:
    base = dict(
        paid_with_balance_count=0,
        paid_with_balance_total=Decimal("0"),
        last_scanned=100,
        eligible_active_subs=100,
        scan_ratio=1.0,
        payments_24h=10,
        payments_7d_daily_avg=10.0,
        payment_volume_ratio=1.0,
        payment_volume_collapsed=False,
    )
    base.update(kw)
    return BillingHealthSnapshot(**base)


def test_anomalies_none_when_healthy():
    assert _snap().anomalies == []


def test_anomalies_flag_each_signal():
    assert "paid_invoices_with_balance" in _snap(paid_with_balance_count=3).anomalies
    assert "invoice_scan_count_low" in _snap(scan_ratio=0.1).anomalies
    assert "payment_volume_collapse" in _snap(payment_volume_collapsed=True).anomalies
    assert (
        "negative_prepaid_balances" in _snap(negative_prepaid_balance_count=1).anomalies
    )
    assert (
        "billing_profile_mismatch" in _snap(billing_profile_mismatch_count=1).anomalies
    )
    assert (
        "billing_profile_mixed_modes" in _snap(billing_profile_mixed_count=1).anomalies
    )
    # scan_ratio just above the floor is fine
    assert "invoice_scan_count_low" not in _snap(scan_ratio=0.6).anomalies


# ---- negative prepaid exposure -------------------------------------------


def test_negative_prepaid_balance_exposure(db_session):
    offer = _offer(db_session)
    account = _subscriber(db_session)
    _subscription(db_session, account, offer, billing_mode=BillingMode.prepaid)
    db_session.add(
        LedgerEntry(
            account_id=account.id,
            entry_type=LedgerEntryType.debit,
            source=LedgerSource.adjustment,
            amount=Decimal("75.00"),
            currency="NGN",
            memo="prepaid drawdown",
        )
    )
    db_session.commit()
    materialize_test_prepaid_opening_balance(db_session, account.id, "0.00")

    count, total, quarantined = billing_health.negative_prepaid_balance_exposure(
        db_session
    )

    assert count == 1
    assert total == Decimal("75.00")
    assert quarantined == 0
    snap = billing_health.billing_health_snapshot(db_session)
    assert snap.negative_prepaid_balance_count == 1
    assert snap.negative_prepaid_balance_total == Decimal("75.00")
    assert "negative_prepaid_balances" in snap.anomalies
    assert "prepaid_funding_quarantined" not in snap.anomalies


def test_exposure_excludes_funding_quarantined_accounts_like_the_sweep(db_session):
    """Monitoring must measure the cohort enforcement actually acts on.

    ``collections.prepaid_balance_sweep`` computes
    ``enforceable_ids = account_ids - quarantined_ids`` BEFORE resolving any
    balance, so a legacy account with no reviewed opening baseline is never
    asked for one. This function queried the same cohort WITHOUT that filter,
    walked straight into the fail-closed resolver, and reported the whole
    signal unavailable -- in production, 80 quarantined accounts blanking the
    exposure figure for the other 3,842 that enforcement was handling fine.

    One cohort definition, not two: quarantined accounts come back as their own
    count, and exposure is measured over the remainder.
    """
    offer = _offer(db_session)

    enforceable = _subscriber(db_session, email="enforceable@example.com")
    enforceable.billing_mode = BillingMode.prepaid
    _subscription(db_session, enforceable, offer, billing_mode=BillingMode.prepaid)
    db_session.add(
        LedgerEntry(
            account_id=enforceable.id,
            entry_type=LedgerEntryType.debit,
            source=LedgerSource.adjustment,
            amount=Decimal("40.00"),
            currency="NGN",
            memo="prepaid drawdown",
        )
    )

    # Legacy: created before the authority cutover and never given a reviewed
    # opening baseline, so the sweep quarantines it. Asking the resolver for
    # its balance is exactly what used to raise.
    quarantined_account = _subscriber(db_session, email="quarantined@example.com")
    quarantined_account.billing_mode = BillingMode.prepaid
    quarantined_account.created_at = datetime(1999, 1, 1, tzinfo=UTC)
    _subscription(
        db_session, quarantined_account, offer, billing_mode=BillingMode.prepaid
    )
    db_session.commit()
    materialize_test_prepaid_opening_balance(db_session, enforceable.id, "0.00")

    count, total, quarantined = billing_health.negative_prepaid_balance_exposure(
        db_session
    )

    # The un-baselined account is excluded, not fatal, and not counted as zero.
    assert quarantined == 1
    assert count == 1
    assert total == Decimal("40.00")

    snap = billing_health.billing_health_snapshot(db_session)
    assert snap.negative_prepaid_funding_quarantined_count == 1
    assert snap.negative_prepaid_balance_count == 1
    assert "prepaid_funding_quarantined" in snap.anomalies
    # The exposure signal stays measurable and keeps publishing.
    assert "negative_prepaid_exposure_unmeasurable" not in snap.anomalies
    observed = {
        (o.signal, o.scope): o.value
        for o in billing_health.billing_health_observations(snap)
    }
    assert observed[("prepaid_funding_quarantined", "all")] == 1
    assert observed[("negative_prepaid_balance_accounts", "all")] == 1


def test_missing_funding_baseline_degrades_one_signal_not_the_snapshot(
    db_session, monkeypatch
):
    """A single un-baselined account must not blank the whole dashboard.

    Regression: the balance resolver is fail-closed by design (enforcement must
    never act on a balance it cannot verify), but this monitoring-only path
    shared it. One account missing a prepaid funding baseline therefore raised
    out of billing_health_snapshot, so the scheduled task published nothing and
    EVERY billing gauge vanished -- including the ones that would reveal an
    invoicing or payment-intake failure. Prod ran blind from 2026-07-18.
    """
    from app.services.prepaid_funding_reconstruction import (
        PrepaidFundingBaselineMissingError,
    )

    def _raise(*_args, **_kwargs):
        raise PrepaidFundingBaselineMissingError(
            "verified prepaid funding baseline missing for: 03dbf141"
        )

    monkeypatch.setattr(billing_health, "prepaid_available_balances", _raise)

    count, total, _quarantined = billing_health.negative_prepaid_balance_exposure(
        db_session
    )
    assert count is None
    assert total is None

    # The snapshot must still be produced, not raise.
    snap = billing_health.billing_health_snapshot(db_session)
    assert snap.negative_prepaid_balance_count is None
    assert "negative_prepaid_exposure_unmeasurable" in snap.anomalies
    assert "negative_prepaid_balances" not in snap.anomalies

    signals = {
        signal
        for signal, _scope, *_ in (
            (o.signal, o.scope)
            for o in billing_health.billing_health_observations(snap)
        )
    }
    # The unmeasurable signal is omitted rather than reported as a false zero...
    assert "negative_prepaid_balance_accounts" not in signals
    assert "negative_prepaid_balance_total" not in signals
    # ...while every other billing gauge still publishes.
    assert "paid_invoices_with_balance" in signals
    assert "active_subscriptions" in signals


def test_negative_prepaid_balance_exposure_has_bounded_query_count(db_session):
    offer = _offer(db_session)
    accounts = []
    for index in range(20):
        account = _subscriber(
            db_session,
            email=f"bounded-prepaid-{index}@example.com",
        )
        _subscription(db_session, account, offer, billing_mode=BillingMode.prepaid)
        db_session.add(
            LedgerEntry(
                account_id=account.id,
                entry_type=LedgerEntryType.debit,
                source=LedgerSource.adjustment,
                amount=Decimal("10.00"),
                currency="NGN",
                memo="prepaid drawdown",
            )
        )
        accounts.append(account)
    db_session.commit()
    materialize_test_prepaid_opening_balances(
        db_session,
        {account.id: "0.00" for account in accounts},
    )

    statements: list[str] = []

    def capture(_conn, _cursor, statement, _params, _context, _executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(db_session.bind, "before_cursor_execute", capture)
    try:
        count, total, _quarantined = billing_health.negative_prepaid_balance_exposure(
            db_session
        )
    finally:
        event.remove(db_session.bind, "before_cursor_execute", capture)

    assert count == 20
    assert total == Decimal("200.00")
    # The financial cohort is loaded in a fixed set of bulk queries.
    assert len(statements) <= 25


def test_billing_health_snapshot_publishes_bounded_observations(monkeypatch):
    from app.services import observability

    captured = {}

    def publish(domain, observations, **kwargs):
        captured.update(
            domain=domain,
            observations=list(observations),
            status=kwargs["status"],
        )
        return True

    monkeypatch.setattr(observability, "publish_state_snapshot", publish)

    snapshot = _snap(
        negative_prepaid_balance_count=2,
        negative_prepaid_balance_total=Decimal("125.00"),
        prepaid_coverage_repairable_count=3,
        prepaid_coverage_quarantined_count=4,
    )
    assert billing_health.publish_billing_health_snapshot(snapshot) is True
    assert captured["domain"] == "billing_health"
    assert captured["status"] == "degraded"
    labels = {
        (item.signal, item.scope): item.value for item in captured["observations"]
    }
    assert labels[("negative_prepaid_balance_accounts", "all")] == 2.0
    assert labels[("negative_prepaid_balance_total", "all")] == 125.0
    assert labels[("prepaid_coverage_repair_required", "all")] == 3.0
    assert labels[("prepaid_coverage_quarantined", "all")] == 4.0


def test_billing_profile_integrity_counts_mismatch_and_mixed_modes(db_session):
    offer = _offer(db_session)
    mismatch = _subscriber(db_session, email="profile-mismatch@example.com")
    mismatch.billing_mode = BillingMode.postpaid
    _subscription(db_session, mismatch, offer, billing_mode=BillingMode.prepaid)

    mixed = _subscriber(db_session, email="profile-mixed@example.com")
    mixed.billing_mode = BillingMode.prepaid
    _subscription(db_session, mixed, offer, billing_mode=BillingMode.prepaid)
    _subscription(db_session, mixed, offer, billing_mode=BillingMode.postpaid)
    db_session.commit()
    materialize_test_prepaid_opening_balances(
        db_session,
        {mismatch.id: "0.00", mixed.id: "0.00"},
    )

    mismatch_count, mixed_count = billing_health.billing_profile_integrity(db_session)

    assert mismatch_count == 1
    assert mixed_count == 1
    snap = billing_health.billing_health_snapshot(db_session)
    assert snap.billing_profile_mismatch_count == 1
    assert snap.billing_profile_mixed_count == 1
    assert "billing_profile_mismatch" in snap.anomalies
    assert "billing_profile_mixed_modes" in snap.anomalies
