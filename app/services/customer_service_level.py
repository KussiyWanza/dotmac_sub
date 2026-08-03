"""Per-subscription SLA policy resolution and period scoring (shadow phase).

Owner: ``customer.service_level`` (OUTAGE_SLA_SPINE §4). Read-time only in
this slice: it resolves the effective policy (offer-version precedence today;
subscription/account contract tables arrive with cutover), merges the accrual
ledger's qualifying intervals, and scores one Africa/Lagos calendar-month
period. It never invents a contractual SLA — no effective policy renders
measured availability with the ``no_contractual_sla`` verdict, not a 99.5%
default. Overlapping intervals are unioned, never summed; excluded intervals
are reported in their own bucket, never silently dropped; unknown-coverage
tracking and persisted effective-dated policy versions are later slices, so
every score carries ``policy`` + ``evidence_digest`` for lineage and the
existing read-time ``topology.customer_availability`` stays authoritative for
display until the shadow-comparison gate cuts over.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.models.catalog import CatalogOffer, SlaProfile, Subscription
from app.services.network.customer_outage_accrual import intervals_for_subscription
from app.services.service_impact_contracts import (
    SLA_CALENDAR_TIMEZONE,
    ImpactState,
    SlaPolicySource,
    SlaPolicyVersion,
    SlaScore,
    SlaVerdict,
)

logger = logging.getLogger(__name__)

# Downtime consuming more than this share of the allowed budget flags the
# period at-risk before it breaches.
_AT_RISK_BUDGET_SHARE = 0.8


@dataclass(frozen=True, slots=True)
class SlaShadowComparison:
    """Ledger-based score next to the legacy read-time availability.

    Approximate by design (calendar month-to-date vs trailing window); the
    comparison exists to find discrepancies before cutover, not to display
    two numbers to customers.
    """

    score: SlaScore
    legacy_availability_percent: float | None
    delta_percent: float | None


def resolve_effective_policy(
    db: Session, subscription: Subscription
) -> SlaPolicyVersion | None:
    """The effective contractual policy, or None — never an invented default.

    Precedence today reaches the offer's SLA profile; subscription- and
    account-contract policies land with the persisted policy-version slice
    and take precedence then.
    """

    offer = (
        db.get(CatalogOffer, subscription.offer_id) if subscription.offer_id else None
    )
    profile = (
        db.get(SlaProfile, offer.sla_profile_id)
        if offer is not None and offer.sla_profile_id
        else None
    )
    if profile is None or profile.uptime_percent is None:
        return None
    effective_from = profile.created_at or datetime.now(UTC)
    if effective_from.tzinfo is None:
        effective_from = effective_from.replace(tzinfo=UTC)
    return SlaPolicyVersion(
        policy_id=profile.id,
        version=1,
        source=SlaPolicySource.offer_version,
        effective_from=effective_from,
        effective_to=None,
        availability_target_percent=float(profile.uptime_percent),
        credit_percent_per_breach=(
            float(profile.credit_percent)
            if profile.credit_percent is not None
            else None
        ),
    )


def period_bounds(now: datetime) -> tuple[datetime, datetime]:
    """The Africa/Lagos calendar month containing ``now``, as UTC instants."""

    zone = ZoneInfo(SLA_CALENDAR_TIMEZONE)
    local = now.astimezone(zone)
    start_local = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start_local.month == 12:
        end_local = start_local.replace(year=start_local.year + 1, month=1)
    else:
        end_local = start_local.replace(month=start_local.month + 1)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _merge_windows(
    windows: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    """Union overlapping windows so concurrent incidents never double-count."""

    merged: list[tuple[datetime, datetime]] = []
    for start, end in sorted(windows):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def score_subscription_period(
    db: Session,
    subscription: Subscription,
    *,
    now: datetime | None = None,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
) -> SlaScore:
    """Score one subscription for one reporting period from the ledger."""

    evaluated_at = now or datetime.now(UTC)
    if period_start is None or period_end is None:
        period_start, period_end = period_bounds(evaluated_at)
    policy = resolve_effective_policy(db, subscription)

    # Eligibility approximation for the shadow phase: an active subscription
    # is entitled from its creation; non-active subscriptions score
    # unavailable rather than pretending entitlement intervals exist.
    status_value = getattr(subscription.status, "value", subscription.status)
    created_at = _utc(subscription.created_at) or period_start
    eligible_start = max(period_start, created_at)
    eligible_end = min(period_end, evaluated_at)
    active = str(status_value) == "active"
    eligible_seconds = (
        max(int((eligible_end - eligible_start).total_seconds()), 0) if active else 0
    )

    qualifying: list[tuple[datetime, datetime]] = []
    excluded: list[tuple[datetime, datetime]] = []
    interval_ids: list[str] = []
    if eligible_seconds:
        for interval in intervals_for_subscription(
            db, subscription.id, since=eligible_start
        ):
            if interval.state != ImpactState.confirmed_unavailable.value:
                continue
            started = _utc(interval.started_at) or eligible_start
            ended = _utc(interval.ended_at) or eligible_end
            start = max(started, eligible_start)
            end = min(ended, eligible_end)
            if end <= start:
                continue
            interval_ids.append(str(interval.id))
            if interval.exclusion_candidate is not None:
                excluded.append((start, end))
            elif interval.quality == "exact":
                qualifying.append((start, end))
            else:
                # Estimated/unavailable evidence never silently becomes
                # contractual downtime; report it with the exclusions until
                # a reviewed adjustment upgrades it.
                excluded.append((start, end))

    unavailable_seconds = sum(
        int((end - start).total_seconds()) for start, end in _merge_windows(qualifying)
    )
    excluded_seconds = sum(
        int((end - start).total_seconds()) for start, end in _merge_windows(excluded)
    )
    unavailable_seconds = min(unavailable_seconds, eligible_seconds)

    digest_material = "\0".join(
        [
            str(subscription.id),
            period_start.isoformat(),
            period_end.isoformat(),
            str(policy.policy_id) if policy else "no-policy",
            *sorted(interval_ids),
        ]
    ).encode()
    evidence_digest = f"sha256:{hashlib.sha256(digest_material).hexdigest()}"

    verdict = _verdict(
        policy=policy,
        eligible_seconds=eligible_seconds,
        unavailable_seconds=unavailable_seconds,
    )
    return SlaScore(
        subscription_id=subscription.id,
        period_start=period_start,
        period_end=period_end,
        eligible_seconds=eligible_seconds,
        unavailable_seconds=unavailable_seconds,
        excluded_seconds=min(excluded_seconds, eligible_seconds),
        unknown_seconds=0,
        verdict=verdict,
        policy=policy,
        evidence_digest=evidence_digest,
        interval_ids=tuple(sorted(interval_ids)),
    )


def _verdict(
    *,
    policy: SlaPolicyVersion | None,
    eligible_seconds: int,
    unavailable_seconds: int,
) -> SlaVerdict:
    if eligible_seconds <= 0:
        return SlaVerdict.unavailable
    if policy is None or policy.availability_target_percent is None:
        return SlaVerdict.no_contractual_sla
    availability = 100.0 * (eligible_seconds - unavailable_seconds) / eligible_seconds
    target = policy.availability_target_percent
    if availability < target:
        return SlaVerdict.breach
    allowed_downtime = eligible_seconds * (100.0 - target) / 100.0
    if allowed_downtime > 0 and unavailable_seconds >= (
        _AT_RISK_BUDGET_SHARE * allowed_downtime
    ):
        return SlaVerdict.at_risk
    return SlaVerdict.passing


def shadow_compare(
    db: Session,
    subscription: Subscription,
    *,
    now: datetime | None = None,
) -> SlaShadowComparison:
    """Ledger score vs the legacy read-time availability, for the cutover gate."""

    evaluated_at = now or datetime.now(UTC)
    score = score_subscription_period(db, subscription, now=evaluated_at)
    legacy_percent: float | None = None
    try:
        from app.services.topology.customer_availability import (
            customer_availability,
        )

        elapsed_days = max(
            int(
                (evaluated_at - score.period_start).total_seconds()
                // int(timedelta(days=1).total_seconds())
            ),
            1,
        )
        legacy = customer_availability(
            db, subscription, days=elapsed_days, now=evaluated_at
        )
        legacy_percent = getattr(legacy, "availability_percent", None)
        if legacy_percent is not None:
            legacy_percent = float(legacy_percent)
    except Exception:
        logger.warning(
            "Legacy availability comparison failed for subscription %s",
            subscription.id,
            exc_info=True,
        )
    measured = score.measured_availability_percent
    delta = (
        round(measured - legacy_percent, 3)
        if measured is not None and legacy_percent is not None
        else None
    )
    return SlaShadowComparison(
        score=score,
        legacy_availability_percent=legacy_percent,
        delta_percent=delta,
    )
