"""customer.service_level shadow scorer (OUTAGE_SLA_SPINE §4).

Pins the approved scoring rules: no invented contractual SLA, union-merged
intervals, reviewed exclusions in their own bucket, Africa/Lagos calendar
periods, and verdicts that only exist against an effective policy.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.models.catalog import SlaProfile
from app.models.network_monitoring import CustomerOutageInterval
from app.services import customer_service_level as sla
from app.services.service_impact_contracts import SlaVerdict

NOW = datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC)


def _interval(
    db,
    subscription_id,
    *,
    start,
    end,
    state="confirmed_unavailable",
    quality="exact",
    exclusion=None,
):
    row = CustomerOutageInterval(
        incident_id=uuid.uuid4(),
        subscription_id=subscription_id,
        state=state,
        quality=quality,
        started_at=start,
        ended_at=end,
        finalized_at=end,
        scope_revision_sequence=1,
        exclusion_candidate=exclusion,
        idempotency_key=f"test:{uuid.uuid4()}",
    )
    db.add(row)
    db.flush()
    return row


def _activate(db, subscription):
    from app.models.catalog import SubscriptionStatus

    subscription.status = SubscriptionStatus.active
    db.flush()


def _attach_policy(db, subscription, *, uptime="99.50", credit="10.00"):
    profile = SlaProfile(
        name="Contract SLA", uptime_percent=uptime, credit_percent=credit
    )
    db.add(profile)
    db.flush()
    offer = subscription.offer
    offer.sla_profile_id = profile.id
    db.flush()
    return profile


def test_period_bounds_are_lagos_calendar_months():
    start, end = sla.period_bounds(NOW)

    # Africa/Lagos is UTC+1: the local month opens at 23:00 UTC the day
    # before the 1st.
    assert start == datetime(2026, 7, 31, 23, 0, 0, tzinfo=UTC)
    assert end == datetime(2026, 8, 31, 23, 0, 0, tzinfo=UTC)


def test_no_policy_scores_measured_availability_without_a_target(
    db_session, subscription
):
    _activate(db_session, subscription)
    _interval(
        db_session,
        subscription.id,
        start=NOW - timedelta(hours=3),
        end=NOW - timedelta(hours=2),
    )

    score = sla.score_subscription_period(db_session, subscription, now=NOW)

    assert score.verdict is SlaVerdict.no_contractual_sla
    assert score.policy is None
    assert score.unavailable_seconds == 3600
    assert score.measured_availability_percent is not None


def test_overlapping_incident_intervals_are_unioned_not_summed(
    db_session, subscription
):
    _activate(db_session, subscription)
    _attach_policy(db_session, subscription)
    _interval(
        db_session,
        subscription.id,
        start=NOW - timedelta(hours=4),
        end=NOW - timedelta(hours=2),
    )
    # A second incident overlapping the same hour must not double-count.
    _interval(
        db_session,
        subscription.id,
        start=NOW - timedelta(hours=3),
        end=NOW - timedelta(hours=1),
    )

    score = sla.score_subscription_period(db_session, subscription, now=NOW)

    assert score.unavailable_seconds == 3 * 3600
    assert len(score.interval_ids) == 2


def test_reviewed_exclusions_report_in_their_own_bucket(db_session, subscription):
    _activate(db_session, subscription)
    _attach_policy(db_session, subscription)
    _interval(
        db_session,
        subscription.id,
        start=NOW - timedelta(hours=2),
        end=NOW - timedelta(hours=1),
        exclusion="incident_discarded",
    )
    # Estimated evidence also never silently becomes contractual downtime.
    _interval(
        db_session,
        subscription.id,
        start=NOW - timedelta(hours=6),
        end=NOW - timedelta(hours=5),
        quality="estimated",
    )

    score = sla.score_subscription_period(db_session, subscription, now=NOW)

    assert score.unavailable_seconds == 0
    assert score.excluded_seconds == 2 * 3600
    assert score.verdict is SlaVerdict.passing


def test_breach_and_at_risk_only_exist_against_the_policy(db_session, subscription):
    _activate(db_session, subscription)
    _attach_policy(db_session, subscription, uptime="99.90")
    # ~19 days elapsed this period; 0.1% budget ≈ 28 minutes. Ten hours of
    # confirmed downtime is a clear breach.
    _interval(
        db_session,
        subscription.id,
        start=NOW - timedelta(hours=11),
        end=NOW - timedelta(hours=1),
    )

    score = sla.score_subscription_period(db_session, subscription, now=NOW)

    assert score.verdict is SlaVerdict.breach
    assert score.policy is not None
    assert score.policy.availability_target_percent == 99.9
    assert score.evidence_digest.startswith("sha256:")


def test_inactive_subscription_scores_unavailable(db_session, subscription):
    from app.models.catalog import SubscriptionStatus

    subscription.status = SubscriptionStatus.suspended
    db_session.flush()

    score = sla.score_subscription_period(db_session, subscription, now=NOW)

    assert score.eligible_seconds == 0
    assert score.verdict is SlaVerdict.unavailable
    assert score.measured_availability_percent is None


def test_open_interval_accrues_to_evaluation_time(db_session, subscription):
    _activate(db_session, subscription)
    _attach_policy(db_session, subscription)
    row = _interval(
        db_session,
        subscription.id,
        start=NOW - timedelta(hours=2),
        end=None,
    )
    row.finalized_at = None
    db_session.flush()

    score = sla.score_subscription_period(db_session, subscription, now=NOW)

    assert score.unavailable_seconds == 2 * 3600
