"""End-to-end usage accuracy: RADIUS accounting sessions -> metering ->
quota bucket -> customer-facing usage-summary (period=cycle).

The cycle number customers see must equal the summed session octets, byte for
byte, once metering has run — this is the billing-authoritative chain.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models.catalog import (
    AccessType,
    BillingCycle,
    BillingMode,
    CatalogOffer,
    PriceBasis,
    ServiceType,
    Subscription,
    SubscriptionStatus,
    UsageAllowance,
)
from app.models.usage import AccountingStatus, QuotaBucket, RadiusAccountingSession
from app.services import usage_summary as svc
from app.services.usage import meter_usage_into_quota

_GB = 1024**3

# Metering buckets sessions into the calendar month of `now`. Pin the clock
# to mid-month so sessions created a few hours in the past cannot cross a
# month boundary — on wall clock these tests fail in the first hours of
# every month (sessions land in the previous month's window).
_MID_MONTH_NOON = datetime.now(UTC).replace(
    day=15, hour=12, minute=0, second=0, microsecond=0
)


def _run_async(coro):
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _setup_metered_subscription(db_session, subscriber):
    allowance = UsageAllowance(name="100GB", included_gb=100, is_active=True)
    db_session.add(allowance)
    db_session.flush()
    offer = CatalogOffer(
        name="Capped 100",
        code="capped-100",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_cycle=BillingCycle.monthly,
        billing_mode=BillingMode.prepaid,
        is_active=True,
        usage_allowance_id=allowance.id,
    )
    db_session.add(offer)
    db_session.flush()
    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=SubscriptionStatus.active,
        billing_mode=offer.billing_mode,
        next_billing_at=datetime.now(UTC),
    )
    db_session.add(subscription)
    db_session.commit()
    return subscription


def test_sessions_meter_into_quota_and_match_cycle_summary(db_session, subscriber):
    subscription = _setup_metered_subscription(db_session, subscriber)
    now = _MID_MONTH_NOON

    # Two sessions with known traffic: 2 GB + 1 GB, both directions counted.
    db_session.add_all(
        [
            RadiusAccountingSession(
                subscription_id=subscription.id,
                session_id="e2e-1",
                status_type=AccountingStatus.stop,
                session_start=now - timedelta(hours=3),
                session_end=now - timedelta(hours=2),
                input_octets=1 * _GB,
                output_octets=1 * _GB,
            ),
            RadiusAccountingSession(
                subscription_id=subscription.id,
                session_id="e2e-2",
                status_type=AccountingStatus.interim,
                session_start=now - timedelta(hours=1),
                input_octets=_GB // 2,
                output_octets=_GB // 2,
            ),
        ]
    )
    db_session.commit()

    meter_usage_into_quota(db_session, now)

    bucket = (
        db_session.query(QuotaBucket)
        .filter(QuotaBucket.subscription_id == subscription.id)
        .one()
    )
    assert Decimal(str(bucket.used_gb)) == Decimal("3.00")

    out = _run_async(
        svc.get_usage_summary(db_session, str(subscriber.id), "cycle", now=now)
    )
    # Cycle total is the RADIUS session-octet sum over the window (the same
    # 2 GB + 1 GB metered above), not the rated quota bucket.
    assert out["total_source"] == "sessions"
    assert out["is_authoritative"] is True
    assert out["total_bytes"] == 3 * _GB


def test_remetering_is_idempotent(db_session, subscriber):
    subscription = _setup_metered_subscription(db_session, subscriber)
    now = _MID_MONTH_NOON
    db_session.add(
        RadiusAccountingSession(
            subscription_id=subscription.id,
            session_id="e2e-3",
            status_type=AccountingStatus.stop,
            session_start=now - timedelta(hours=1),
            session_end=now,
            input_octets=2 * _GB,
            output_octets=0,
        )
    )
    db_session.commit()

    meter_usage_into_quota(db_session, now)
    meter_usage_into_quota(db_session, now)  # absolute recompute, not increment

    bucket = (
        db_session.query(QuotaBucket)
        .filter(QuotaBucket.subscription_id == subscription.id)
        .one()
    )
    assert Decimal(str(bucket.used_gb)) == Decimal("2.00")
