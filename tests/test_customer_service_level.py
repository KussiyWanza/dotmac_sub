"""customer.service_level shadow scorer (OUTAGE_SLA_SPINE §4).

Pins the approved scoring rules: no invented contractual SLA, union-merged
intervals, reviewed exclusions in their own bucket, Africa/Lagos calendar
periods, and verdicts that only exist against an effective policy.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.models.billing import ServiceEntitlement, ServiceEntitlementStatus
from app.models.catalog import BillingMode, SlaProfile
from app.models.lifecycle import LifecycleEventType, SubscriptionLifecycleEvent
from app.models.network_monitoring import CustomerOutageInterval
from app.models.usage import AccountingStatus, RadiusAccountingSession
from app.services import customer_service_level as sla
from app.services.service_impact_contracts import SlaVerdict

NOW = datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)


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

    evidence_start = NOW - timedelta(days=10)
    # The shared subscription fixture records its initial pending state at the
    # wall-clock time when the test runs.  These tests build a complete,
    # synthetic lifecycle history around the fixed NOW instant; retaining the
    # fixture event makes that history depend on the calendar date and can
    # close the active window partway through the scoring period.
    (
        db.query(SubscriptionLifecycleEvent)
        .filter(SubscriptionLifecycleEvent.subscription_id == subscription.id)
        .delete(synchronize_session=False)
    )
    subscription.status = SubscriptionStatus.active
    subscription.billing_mode = BillingMode.prepaid
    subscription.start_at = evidence_start
    subscription.next_billing_at = NOW + timedelta(days=1)
    evidence_id = uuid.uuid4()
    db.add(
        SubscriptionLifecycleEvent(
            id=evidence_id,
            subscription_id=subscription.id,
            event_type=LifecycleEventType.activate,
            to_status=SubscriptionStatus.active,
            evidence_grade="state_baseline",
            evidence_source="reconciliation_baseline",
            source_id=f"test:sla:{evidence_id}",
            evidence_fingerprint=f"sha256:{uuid.uuid4().hex * 2}",
            effective_at=evidence_start,
            recorded_at=evidence_start,
            created_at=evidence_start,
        )
    )
    db.add(
        ServiceEntitlement(
            account_id=subscription.subscriber_id,
            subscription_id=subscription.id,
            starts_at=evidence_start,
            ends_at=NOW + timedelta(days=1),
            status=ServiceEntitlementStatus.active,
        )
    )
    db.add(
        RadiusAccountingSession(
            subscription_id=subscription.id,
            session_id=f"sla-{uuid.uuid4()}",
            status_type=AccountingStatus.stop,
            session_start=evidence_start,
            session_end=NOW,
            last_update_at=NOW,
        )
    )
    db.flush()
    return evidence_start


def _attach_policy(db, subscription, *, uptime="99.50", credit="10.00"):
    profile = SlaProfile(
        name="Contract SLA",
        uptime_percent=uptime,
        credit_percent=credit,
        created_at=NOW - timedelta(days=10),
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


def test_explicit_period_instants_are_normalized_to_utc(db_session, subscription):
    from zoneinfo import ZoneInfo

    period_start = _activate(db_session, subscription)
    lagos = ZoneInfo("Africa/Lagos")

    score = sla.score_subscription_period(
        db_session,
        subscription,
        now=NOW.astimezone(lagos),
        period_start=period_start.astimezone(lagos),
        period_end=NOW.astimezone(lagos),
    )

    assert score.period_start == period_start
    assert score.period_end == NOW
    assert score.period_start.tzinfo is UTC
    assert score.period_end.tzinfo is UTC


def test_no_policy_scores_measured_availability_without_a_target(
    db_session, subscription
):
    period_start = _activate(db_session, subscription)
    _interval(
        db_session,
        subscription.id,
        start=NOW - timedelta(hours=3),
        end=NOW - timedelta(hours=2),
    )

    score = sla.score_subscription_period(
        db_session,
        subscription,
        now=NOW,
        period_start=period_start,
        period_end=NOW,
    )

    assert score.verdict is SlaVerdict.no_contractual_sla
    assert score.policy is None
    assert score.unavailable_seconds == 3600
    assert score.measured_availability_percent is not None


def test_overlapping_incident_intervals_are_unioned_not_summed(
    db_session, subscription
):
    period_start = _activate(db_session, subscription)
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

    score = sla.score_subscription_period(
        db_session,
        subscription,
        now=NOW,
        period_start=period_start,
        period_end=NOW,
    )

    assert score.unavailable_seconds == 3 * 3600
    assert len(score.interval_ids) == 2


def test_reviewed_exclusions_report_in_their_own_bucket(db_session, subscription):
    period_start = _activate(db_session, subscription)
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

    score = sla.score_subscription_period(
        db_session,
        subscription,
        now=NOW,
        period_start=period_start,
        period_end=NOW,
    )

    assert score.unavailable_seconds == 0
    assert score.excluded_seconds == 2 * 3600
    assert score.verdict is SlaVerdict.passing


def test_breach_and_at_risk_only_exist_against_the_policy(db_session, subscription):
    period_start = _activate(db_session, subscription)
    _attach_policy(db_session, subscription, uptime="99.90")
    # About 7 days elapsed this period; 0.1% budget is under 11 minutes. Ten hours of
    # confirmed downtime is a clear breach.
    _interval(
        db_session,
        subscription.id,
        start=NOW - timedelta(hours=11),
        end=NOW - timedelta(hours=1),
    )

    score = sla.score_subscription_period(
        db_session,
        subscription,
        now=NOW,
        period_start=period_start,
        period_end=NOW,
    )

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
    period_start = _activate(db_session, subscription)
    _attach_policy(db_session, subscription)
    row = _interval(
        db_session,
        subscription.id,
        start=NOW - timedelta(hours=2),
        end=None,
    )
    row.finalized_at = None
    db_session.flush()

    score = sla.score_subscription_period(
        db_session,
        subscription,
        now=NOW,
        period_start=period_start,
        period_end=NOW,
    )

    assert score.unavailable_seconds == 2 * 3600


def test_missing_postpaid_contract_authority_is_explicitly_incomplete(
    db_session, subscription
):
    from app.models.catalog import SubscriptionStatus

    subscription.status = SubscriptionStatus.active
    subscription.billing_mode = BillingMode.postpaid
    db_session.flush()

    score = sla.score_subscription_period(db_session, subscription, now=NOW)

    assert score.evidence_complete is False
    assert score.verdict is SlaVerdict.unavailable
    assert score.measured_availability_percent is None
    assert any(
        issue == "entitlement:missing_authoritative_billing_contract"
        for issue in score.completeness_issues
    )


def test_unknown_monitoring_time_exposes_bounds_and_never_passes(
    db_session, subscription
):
    period_start = _activate(db_session, subscription)
    _attach_policy(db_session, subscription, uptime="99.00")
    session = (
        db_session.query(RadiusAccountingSession)
        .filter(RadiusAccountingSession.subscription_id == subscription.id)
        .one()
    )
    session.session_end = NOW - timedelta(hours=1)
    session.last_update_at = NOW - timedelta(hours=1)
    db_session.flush()

    score = sla.score_subscription_period(
        db_session,
        subscription,
        now=NOW,
        period_start=period_start,
        period_end=NOW,
    )

    assert score.unknown_seconds == 3600
    assert score.evidence_complete is False
    assert score.verdict is SlaVerdict.unavailable
    assert score.measured_availability_percent is None
    assert score.availability_lower_bound_percent is not None
    assert score.availability_upper_bound_percent == 100.0


def test_incomplete_evidence_can_prove_breach_only_from_the_best_case_bound(
    db_session, subscription
):
    period_start = _activate(db_session, subscription)
    _attach_policy(db_session, subscription, uptime="99.99")
    session = (
        db_session.query(RadiusAccountingSession)
        .filter(RadiusAccountingSession.subscription_id == subscription.id)
        .one()
    )
    session.session_end = NOW - timedelta(hours=1)
    session.last_update_at = NOW - timedelta(hours=1)
    _interval(
        db_session,
        subscription.id,
        start=NOW - timedelta(hours=3),
        end=NOW - timedelta(hours=2),
    )
    db_session.flush()

    score = sla.score_subscription_period(
        db_session,
        subscription,
        now=NOW,
        period_start=period_start,
        period_end=NOW,
    )

    assert score.evidence_complete is False
    assert score.availability_upper_bound_percent is not None
    assert score.availability_upper_bound_percent < 99.99
    assert score.verdict is SlaVerdict.breach


# --- persisted effective-dated policy versions and precedence (§4) ----------


def _version(
    db,
    *,
    key,
    source,
    target="99.500",
    version=1,
    start,
    end=None,
    subscription=None,
    subscriber_id=None,
    offer_id=None,
):
    from decimal import Decimal

    from app.models.catalog import SlaPolicyVersion as Record

    row = Record(
        policy_key=key,
        version=version,
        source=source,
        subscription_id=subscription.id if subscription is not None else None,
        subscriber_id=subscriber_id,
        offer_id=offer_id,
        effective_from=start,
        effective_to=end,
        availability_target_percent=Decimal(target) if target is not None else None,
        calendar_timezone="Africa/Lagos",
        maintenance_excludable=True,
    )
    db.add(row)
    db.flush()
    return row


def test_persisted_version_beats_the_legacy_profile(db_session, subscription):
    """The persisted policy is the authority; SlaProfile is only a fallback."""

    _attach_policy(db_session, subscription, uptime="99.50")
    _version(
        db_session,
        key=f"sub:{subscription.id}",
        source="subscription_contract",
        target="99.900",
        start=NOW - timedelta(days=30),
        subscription=subscription,
    )

    policy = sla.resolve_effective_policy(db_session, subscription, at=NOW)

    assert policy is not None
    assert policy.availability_target_percent == 99.9
    assert policy.source.value == "subscription_contract"


def test_precedence_subscription_beats_account_beats_offer(db_session, subscription):
    start = NOW - timedelta(days=30)
    _version(
        db_session,
        key=f"offer:{subscription.offer_id}",
        source="offer_version",
        target="99.000",
        start=start,
        offer_id=subscription.offer_id,
    )
    _version(
        db_session,
        key=f"acct:{subscription.subscriber_id}",
        source="account_contract",
        target="99.500",
        start=start,
        subscriber_id=subscription.subscriber_id,
    )
    assert (
        sla.resolve_effective_policy(
            db_session, subscription, at=NOW
        ).availability_target_percent
        == 99.5
    ), "account contract must beat the offer version"

    _version(
        db_session,
        key=f"sub:{subscription.id}",
        source="subscription_contract",
        target="99.950",
        start=start,
        subscription=subscription,
    )
    assert (
        sla.resolve_effective_policy(
            db_session, subscription, at=NOW
        ).availability_target_percent
        == 99.95
    ), "subscription contract must beat the account contract"


def test_policy_is_resolved_at_an_instant_not_the_latest_row(db_session, subscription):
    """An expired version must not govern a period it did not cover."""

    _version(
        db_session,
        key=f"sub:{subscription.id}",
        source="subscription_contract",
        target="99.000",
        version=1,
        start=NOW - timedelta(days=60),
        end=NOW - timedelta(days=30),
        subscription=subscription,
    )
    _version(
        db_session,
        key=f"sub:{subscription.id}",
        source="subscription_contract",
        target="99.900",
        version=2,
        start=NOW - timedelta(days=30),
        subscription=subscription,
    )

    old = sla.resolve_effective_policy(
        db_session, subscription, at=NOW - timedelta(days=45)
    )
    new = sla.resolve_effective_policy(db_session, subscription, at=NOW)

    assert old.availability_target_percent == 99.0
    assert old.version == 1
    assert new.availability_target_percent == 99.9
    assert new.version == 2


def test_a_mid_period_change_splits_the_period(db_session, subscription):
    """§4: a mid-period policy change splits the calculation by version —
    the later terms must not be applied retroactively."""

    period_start, period_end = sla.period_bounds(NOW)
    change_at = period_start + timedelta(days=10)
    _version(
        db_session,
        key=f"sub:{subscription.id}",
        source="subscription_contract",
        target="99.000",
        version=1,
        start=period_start - timedelta(days=5),
        end=change_at,
        subscription=subscription,
    )
    _version(
        db_session,
        key=f"sub:{subscription.id}",
        source="subscription_contract",
        target="99.900",
        version=2,
        start=change_at,
        subscription=subscription,
    )

    segments = sla.policy_segments_for_period(
        db_session, subscription, period_start=period_start, period_end=period_end
    )

    assert len(segments) == 2
    assert segments[0].start == period_start and segments[0].end == change_at
    assert segments[0].policy.availability_target_percent == 99.0
    assert segments[1].start == change_at and segments[1].end == period_end
    assert segments[1].policy.availability_target_percent == 99.9
    # The split must partition the period exactly — no gap, no overlap.
    assert sum(s.seconds for s in segments) == int(
        (period_end - period_start).total_seconds()
    )


def test_a_precedence_change_mid_period_also_splits(db_session, subscription):
    """A subscription contract starting mid-month splits the period even
    though no single policy's terms changed."""

    period_start, period_end = sla.period_bounds(NOW)
    starts_at = period_start + timedelta(days=7)
    _version(
        db_session,
        key=f"offer:{subscription.offer_id}",
        source="offer_version",
        target="99.000",
        start=period_start - timedelta(days=60),
        offer_id=subscription.offer_id,
    )
    _version(
        db_session,
        key=f"sub:{subscription.id}",
        source="subscription_contract",
        target="99.950",
        start=starts_at,
        subscription=subscription,
    )

    segments = sla.policy_segments_for_period(
        db_session, subscription, period_start=period_start, period_end=period_end
    )

    assert len(segments) == 2
    assert segments[0].policy.source.value == "offer_version"
    assert segments[1].policy.source.value == "subscription_contract"


def test_legacy_offer_profile_never_applies_before_its_creation(
    db_session, subscription
):
    """The shadow fallback still has an effective instant; ignoring it would
    let mutable legacy terms govern time before they existed."""

    profile = _attach_policy(db_session, subscription, uptime="99.500")
    created_at = NOW - timedelta(days=1)
    profile.created_at = created_at
    db_session.flush()

    segments = sla.policy_segments_for_period(
        db_session,
        subscription,
        period_start=NOW - timedelta(days=3),
        period_end=NOW,
    )

    assert len(segments) == 2
    assert segments[0].end == created_at
    assert segments[0].policy is None
    assert segments[1].start == created_at
    assert segments[1].policy is not None
    assert segments[1].policy.availability_target_percent == 99.5


def test_no_policy_yields_a_single_uncontracted_segment(db_session, subscription):
    period_start, period_end = sla.period_bounds(NOW)

    segments = sla.policy_segments_for_period(
        db_session, subscription, period_start=period_start, period_end=period_end
    )

    assert len(segments) == 1
    assert segments[0].policy is None


# --- recording versions is append-only (§4) ---------------------------------


@pytest.fixture
def staged_owner_command(monkeypatch):
    """Run the owner operation under the test session's open transaction.

    The production wrapper demands a transaction-free session and commits;
    the shared fixture keeps one transaction open so rows roll back. This is
    the same seam `tests/fup_helpers.execute_owner_command_for_test` uses —
    the operation body, and therefore every rule under test, is unchanged.
    """
    import app.services.owner_commands as oc

    monkeypatch.setattr(
        oc,
        "execute_owner_command",
        lambda db, *, definition, context, operation: operation(),
    )
    return None


def _ctx():
    from app.services.owner_commands import CommandContext

    return CommandContext.system(
        actor="test:sla",
        scope="policy",
        reason="acceptance",
        idempotency_key=f"sla-{uuid.uuid4()}",
    )


def _score_context(key: str):
    from app.services.owner_commands import CommandContext

    return CommandContext.system(
        actor="test:sla-score",
        scope="sla-period-score",
        reason="shadow scoring acceptance",
        idempotency_key=key,
    )


def test_period_score_recording_replays_then_appends_changed_evidence(
    db_session, subscription, staged_owner_command
):
    from app.models.sla import (
        SlaEligibilityInterval,
        SlaMonitoringInterval,
        SlaPeriodScoreRevision,
    )

    period_start = _activate(db_session, subscription)
    _attach_policy(db_session, subscription, uptime="99.50")
    first_command = sla.RecordPeriodScoreCommand(
        subscription_id=subscription.id,
        period_start=period_start,
        period_end=NOW,
        evaluated_at=NOW,
        context=_score_context("score-period-1"),
    )

    first = sla.record_period_score(db_session, first_command)
    replay = sla.record_period_score(db_session, first_command)

    assert first.revision == 1
    assert first.replayed is False
    assert replay.score_revision_id == first.score_revision_id
    assert replay.replayed is True
    assert db_session.query(SlaPeriodScoreRevision).count() == 1
    assert db_session.query(SlaEligibilityInterval).count() == 1
    assert db_session.query(SlaMonitoringInterval).count() == 1

    _interval(
        db_session,
        subscription.id,
        start=NOW - timedelta(hours=2),
        end=NOW - timedelta(hours=1),
    )
    second = sla.record_period_score(
        db_session,
        sla.RecordPeriodScoreCommand(
            subscription_id=subscription.id,
            period_start=period_start,
            period_end=NOW,
            evaluated_at=NOW,
            context=_score_context("score-period-2"),
        ),
    )

    assert second.revision == 2
    assert second.supersedes_id == first.score_revision_id
    assert second.evidence_digest != first.evidence_digest
    assert db_session.query(SlaPeriodScoreRevision).count() == 2


def test_exact_score_evidence_under_a_new_identity_is_not_claimed_as_replay(
    db_session, subscription, staged_owner_command
):
    period_start = _activate(db_session, subscription)

    def command(key: str):
        return sla.RecordPeriodScoreCommand(
            subscription_id=subscription.id,
            period_start=period_start,
            period_end=NOW,
            evaluated_at=NOW,
            context=_score_context(key),
        )

    sla.record_period_score(db_session, command("score-key-a"))

    with pytest.raises(sla.SlaScoreError) as caught:
        sla.record_period_score(db_session, command("score-key-b"))

    assert caught.value.code == "customer.service_level.duplicate_score_evidence"


def test_recording_a_new_version_closes_the_one_it_supersedes(
    db_session, subscription, staged_owner_command
):
    """Terms are never edited: superseding closes the open version exactly
    where the next begins, so a scored period keeps the terms it was measured
    under."""

    first = sla.record_policy_version(
        db_session,
        sla.RecordPolicyVersionCommand(
            source=sla.SlaPolicySource.subscription_contract,
            effective_from=NOW - timedelta(days=60),
            availability_target_percent=99.0,
            subscription_id=subscription.id,
            context=_ctx(),
        ),
    )
    second = sla.record_policy_version(
        db_session,
        sla.RecordPolicyVersionCommand(
            source=sla.SlaPolicySource.subscription_contract,
            effective_from=NOW - timedelta(days=30),
            availability_target_percent=99.9,
            subscription_id=subscription.id,
            context=_ctx(),
        ),
    )

    from app.models.catalog import SlaPolicyVersion as Record

    first_row = db_session.get(Record, first.policy_version_id)
    # SQLite returns naive datetimes; normalise rather than assert a dialect
    # behaviour. The migrated-PostgreSQL canary covers the tz-aware storage.
    closed_at = first_row.effective_to
    if closed_at.tzinfo is None:
        closed_at = closed_at.replace(tzinfo=UTC)
    assert closed_at == NOW - timedelta(days=30), "must abut exactly"
    assert second.version == 2
    assert second.superseded_version_id == first.policy_version_id
    assert second.replayed is False
    # The old terms still govern the period they covered.
    assert (
        sla.resolve_effective_policy(
            db_session, subscription, at=NOW - timedelta(days=45)
        ).availability_target_percent
        == 99.0
    )


def test_backdating_behind_a_closed_version_is_refused(
    db_session, subscription, staged_owner_command
):
    """Rewriting an already-scored period is exactly what superseding the
    mutable SlaProfile was meant to stop."""

    for days, target in ((60, 99.0), (30, 99.9)):
        sla.record_policy_version(
            db_session,
            sla.RecordPolicyVersionCommand(
                source=sla.SlaPolicySource.subscription_contract,
                effective_from=NOW - timedelta(days=days),
                availability_target_percent=target,
                subscription_id=subscription.id,
                context=_ctx(),
            ),
        )

    with pytest.raises(sla.SlaPolicyError):
        sla.record_policy_version(
            db_session,
            sla.RecordPolicyVersionCommand(
                source=sla.SlaPolicySource.subscription_contract,
                effective_from=NOW - timedelta(days=45),
                availability_target_percent=98.0,
                subscription_id=subscription.id,
                context=_ctx(),
            ),
        )


def test_a_contractual_version_requires_a_target(
    db_session, subscription, staged_owner_command
):
    with pytest.raises(sla.SlaPolicyError):
        sla.record_policy_version(
            db_session,
            sla.RecordPolicyVersionCommand(
                source=sla.SlaPolicySource.subscription_contract,
                effective_from=NOW,
                availability_target_percent=None,
                subscription_id=subscription.id,
                context=_ctx(),
            ),
        )


def test_internal_measurement_may_have_no_target(
    db_session, subscription, staged_owner_command
):
    """It states what we measure, never what we promised."""

    outcome = sla.record_policy_version(
        db_session,
        sla.RecordPolicyVersionCommand(
            source=sla.SlaPolicySource.internal_measurement,
            effective_from=NOW - timedelta(days=1),
            availability_target_percent=None,
            context=_ctx(),
        ),
    )

    assert outcome.policy_key == "internal_measurement:global"
    # It must not masquerade as a contractual promise.
    policy = sla.resolve_effective_policy(db_session, subscription, at=NOW)
    assert policy is not None
    assert policy.source is sla.SlaPolicySource.internal_measurement
    assert policy.availability_target_percent is None


# --- review blockers: identity, replay, and the scorer gate ------------------


def test_policy_identity_is_derived_from_the_real_scope(db_session, subscription):
    """A caller-supplied key would let two series target one subscription for
    the same period, giving two equal-precedence policies and an undefined
    winner. The key is a function of (source, scope)."""

    assert (
        sla.derive_policy_key(
            sla.SlaPolicySource.subscription_contract,
            subscription_id=subscription.id,
        )
        == f"subscription_contract:{subscription.id}"
    )
    assert (
        sla.derive_policy_key(sla.SlaPolicySource.internal_measurement)
        == "internal_measurement:global"
    )
    # A precedence claim with no scope cannot name a series at all.
    with pytest.raises(sla.SlaPolicyError):
        sla.derive_policy_key(sla.SlaPolicySource.subscription_contract)
    with pytest.raises(sla.SlaPolicyError):
        sla.derive_policy_key(sla.SlaPolicySource.account_contract)


def test_replaying_the_same_key_returns_the_original_outcome(
    db_session, subscription, staged_owner_command
):
    """A retry with the SAME key must not raise against the row it created."""

    from app.services.owner_commands import CommandContext

    ctx = CommandContext.system(
        actor="test:sla",
        scope="policy",
        reason="acceptance",
        idempotency_key="sla-replay-key",
    )

    def _cmd():
        return sla.RecordPolicyVersionCommand(
            source=sla.SlaPolicySource.subscription_contract,
            effective_from=NOW - timedelta(days=10),
            availability_target_percent=99.9,
            subscription_id=subscription.id,
            context=ctx,
        )

    first = sla.record_policy_version(db_session, _cmd())
    replay = sla.record_policy_version(db_session, _cmd())

    assert first.replayed is False
    assert replay.replayed is True
    assert replay.policy_version_id == first.policy_version_id

    from app.models.catalog import SlaPolicyVersion as Record

    assert (
        db_session.query(Record).filter(Record.policy_key == first.policy_key).count()
        == 1
    ), "a replay must not append a second version"


def test_a_new_key_cannot_claim_success_for_existing_terms(
    db_session, subscription, staged_owner_command
):
    """The hole this closes: key A records terms F; key B submits F. Replaying
    on the fingerprint would report success while B was never persisted,
    leaving B free to append later with different terms instead of
    conflicting."""

    from app.services.owner_commands import CommandContext

    def _ctx_named(key):
        return CommandContext.system(
            actor="test:sla",
            scope="policy",
            reason="acceptance",
            idempotency_key=key,
        )

    def _cmd(key, target):
        return sla.RecordPolicyVersionCommand(
            source=sla.SlaPolicySource.subscription_contract,
            effective_from=NOW - timedelta(days=10),
            availability_target_percent=target,
            subscription_id=subscription.id,
            context=_ctx_named(key),
        )

    sla.record_policy_version(db_session, _cmd("key-A", 99.9))

    # B/F must NOT report success — B reserves nothing.
    with pytest.raises(sla.SlaPolicyError) as caught:
        sla.record_policy_version(db_session, _cmd("key-B", 99.9))
    assert caught.value.code == "customer.service_level.duplicate_policy_terms"

    from app.models.catalog import SlaPolicyVersion as Record

    assert (
        db_session.query(Record)
        .filter(Record.command_idempotency_key == "key-B")
        .count()
        == 0
    ), "a key that never succeeded must not be recorded"


def test_a_concurrent_loser_retrying_with_the_same_terms_replays(
    db_session, subscription, staged_owner_command
):
    """Command-level semantics after losing the unique-key race: the winner
    wrote the SAME terms, so the retry replays rather than conflicting.

    The raw PostgreSQL canary proves the constraint; this proves the service
    behaviour built on it."""

    from app.services.owner_commands import CommandContext

    ctx = CommandContext.system(
        actor="test:sla",
        scope="policy",
        reason="acceptance",
        idempotency_key="sla-race-key",
    )
    cmd = sla.RecordPolicyVersionCommand(
        source=sla.SlaPolicySource.subscription_contract,
        effective_from=NOW - timedelta(days=10),
        availability_target_percent=99.9,
        subscription_id=subscription.id,
        context=ctx,
    )
    winner = sla.record_policy_version(db_session, cmd)

    # The loser retries the identical command.
    retry = sla.record_policy_version(db_session, cmd)

    assert retry.replayed is True
    assert retry.policy_version_id == winner.policy_version_id


def test_a_concurrent_loser_retrying_with_different_terms_conflicts(
    db_session, subscription, staged_owner_command
):
    """Same key, but the winner wrote different terms — the retry must not
    quietly append a second version."""

    from app.services.owner_commands import CommandContext

    ctx = CommandContext.system(
        actor="test:sla",
        scope="policy",
        reason="acceptance",
        idempotency_key="sla-race-key-2",
    )

    def _cmd(target):
        return sla.RecordPolicyVersionCommand(
            source=sla.SlaPolicySource.subscription_contract,
            effective_from=NOW - timedelta(days=10),
            availability_target_percent=target,
            subscription_id=subscription.id,
            context=ctx,
        )

    sla.record_policy_version(db_session, _cmd(99.9))

    with pytest.raises(sla.SlaPolicyError) as caught:
        sla.record_policy_version(db_session, _cmd(99.1))
    assert caught.value.code == "customer.service_level.idempotency_conflict"


def test_the_outcome_is_immutable_and_not_the_orm_row(
    db_session, subscription, staged_owner_command
):
    """Returning the entity would hand callers a mutable handle to an
    append-only record."""

    outcome = sla.record_policy_version(
        db_session,
        sla.RecordPolicyVersionCommand(
            source=sla.SlaPolicySource.subscription_contract,
            effective_from=NOW - timedelta(days=5),
            availability_target_percent=99.5,
            subscription_id=subscription.id,
            context=_ctx(),
        ),
    )

    assert isinstance(outcome, sla.PolicyVersionOutcome)
    with pytest.raises((AttributeError, TypeError)):
        outcome.version = 99  # frozen


def test_recorded_policy_changes_only_its_effective_segment(
    db_session, subscription, staged_owner_command
):
    """PR 2 consumes effective segments without applying terms retroactively."""

    period_start = _activate(db_session, subscription)
    _interval(
        db_session,
        subscription.id,
        start=NOW - timedelta(hours=2),
        end=NOW - timedelta(hours=1),
    )
    before = sla.score_subscription_period(
        db_session,
        subscription,
        now=NOW,
        period_start=period_start,
        period_end=NOW,
    )

    sla.record_policy_version(
        db_session,
        sla.RecordPolicyVersionCommand(
            source=sla.SlaPolicySource.subscription_contract,
            effective_from=NOW - timedelta(days=1),
            availability_target_percent=99.999,
            subscription_id=subscription.id,
            context=_ctx(),
        ),
    )
    after = sla.score_subscription_period(
        db_session,
        subscription,
        now=NOW,
        period_start=period_start,
        period_end=NOW,
    )

    assert before.verdict is SlaVerdict.no_contractual_sla
    assert after.verdict is SlaVerdict.breach
    assert after.evidence_digest != before.evidence_digest
    assert len(after.policy_segments) == 2
    assert after.policy_segments[0].policy is None
    assert after.policy_segments[1].policy is not None
    assert after.policy_segments[1].policy.availability_target_percent == 99.999


# --- review round 2: precedence during shadow migration, idempotency, input --


def test_a_persisted_internal_policy_does_not_mask_the_legacy_offer_sla(
    db_session, subscription, staged_owner_command
):
    """Both sources are live during the shadow migration, so both must be
    ranked by ONE precedence order. `internal_measurement` is the lowest
    precedence there is; preferring it because it happens to be persisted
    would hide the customer's actual offer SLA."""

    _attach_policy(db_session, subscription, uptime="99.50")
    sla.record_policy_version(
        db_session,
        sla.RecordPolicyVersionCommand(
            source=sla.SlaPolicySource.internal_measurement,
            effective_from=NOW - timedelta(days=30),
            availability_target_percent=95.0,
            context=_ctx(),
        ),
    )

    policy = sla.resolve_effective_policy(db_session, subscription, at=NOW)

    assert policy.source is sla.SlaPolicySource.offer_version
    assert policy.availability_target_percent == 99.5


def test_a_persisted_offer_policy_wins_the_tie_against_the_legacy_profile(
    db_session, subscription, staged_owner_command
):
    """Equal precedence, so the persisted authority wins — it is what the
    legacy derivation is being retired in favour of."""

    _attach_policy(db_session, subscription, uptime="99.50")
    sla.record_policy_version(
        db_session,
        sla.RecordPolicyVersionCommand(
            source=sla.SlaPolicySource.offer_version,
            effective_from=NOW - timedelta(days=30),
            availability_target_percent=99.8,
            offer_id=subscription.offer_id,
            context=_ctx(),
        ),
    )

    policy = sla.resolve_effective_policy(db_session, subscription, at=NOW)

    assert policy.availability_target_percent == 99.8


def test_reusing_an_idempotency_key_for_different_terms_conflicts(
    db_session, subscription, staged_owner_command
):
    """Same key + different inputs must never append a second version."""

    from app.services.owner_commands import CommandContext

    shared = CommandContext.system(
        actor="test:sla",
        scope="policy",
        reason="acceptance",
        idempotency_key="sla-fixed-key",
    )

    def _cmd(target):
        return sla.RecordPolicyVersionCommand(
            source=sla.SlaPolicySource.subscription_contract,
            effective_from=NOW - timedelta(days=10),
            availability_target_percent=target,
            subscription_id=subscription.id,
            context=shared,
        )

    first = sla.record_policy_version(db_session, _cmd(99.9))
    # Same key, same terms -> replay.
    assert sla.record_policy_version(db_session, _cmd(99.9)).replayed is True
    # Same key, different terms -> conflict, not a second version.
    with pytest.raises(sla.SlaPolicyError) as caught:
        sla.record_policy_version(db_session, _cmd(99.1))
    assert caught.value.code == "customer.service_level.idempotency_conflict"

    from app.models.catalog import SlaPolicyVersion as Record

    assert (
        db_session.query(Record).filter(Record.policy_key == first.policy_key).count()
        == 1
    )


def test_a_scope_id_from_the_wrong_source_is_invalid_input(
    db_session, subscription, staged_owner_command
):
    """Not a concurrency conflict — telling this caller to retry would loop."""

    with pytest.raises(sla.SlaPolicyError) as caught:
        sla.record_policy_version(
            db_session,
            sla.RecordPolicyVersionCommand(
                source=sla.SlaPolicySource.subscription_contract,
                effective_from=NOW,
                availability_target_percent=99.9,
                subscription_id=subscription.id,
                offer_id=subscription.offer_id,  # extra scope
                context=_ctx(),
            ),
        )
    assert caught.value.code == "customer.service_level.invalid_scope"


def test_a_nonexistent_parent_is_invalid_input_not_concurrency(
    db_session, subscription, staged_owner_command
):
    with pytest.raises(sla.SlaPolicyError) as caught:
        sla.record_policy_version(
            db_session,
            sla.RecordPolicyVersionCommand(
                source=sla.SlaPolicySource.subscription_contract,
                effective_from=NOW,
                availability_target_percent=99.9,
                subscription_id=uuid.uuid4(),
                context=_ctx(),
            ),
        )
    assert caught.value.code == "customer.service_level.unknown_scope"


def test_version_one_supersedes_nothing(db_session, subscription, staged_owner_command):
    outcome = sla.record_policy_version(
        db_session,
        sla.RecordPolicyVersionCommand(
            source=sla.SlaPolicySource.subscription_contract,
            effective_from=NOW - timedelta(days=1),
            availability_target_percent=99.9,
            subscription_id=subscription.id,
            context=_ctx(),
        ),
    )

    assert outcome.version == 1
    assert outcome.superseded_version_id is None
    assert outcome.superseded_at is None, "nothing was superseded, so no instant"


# --- plan-family scope ------------------------------------------------------


def test_family_policy_identity_is_the_family_itself(db_session):
    """A family default is a real scope, so it names its own series. Without
    this the terms would have to be copied onto every offer in the family and
    would drift the moment one was edited."""

    assert (
        sla.derive_policy_key(
            sla.SlaPolicySource.plan_family,
            plan_family=sla.SlaPlanFamily.unlimited,
        )
        == "plan_family:unlimited"
    )
    # A precedence claim with no family names no series.
    with pytest.raises(sla.SlaPolicyError):
        sla.derive_policy_key(sla.SlaPolicySource.plan_family)
    # SLA authority is a NARROWER vocabulary than catalog classification, which
    # is settings-driven and may grow. A raw string — even a real catalog
    # family — is refused, because it would name a series the resolver has no
    # typed way to match.
    with pytest.raises(sla.SlaPolicyError) as caught:
        sla.derive_policy_key(sla.SlaPolicySource.plan_family, plan_family="platinum")
    assert caught.value.code == "customer.service_level.unknown_plan_family"


def test_family_scope_rejects_a_foreign_scope(
    db_session, subscription, staged_owner_command
):
    """A family policy carrying a subscription id would claim two scopes and
    leave the resolver with two equal-precedence answers."""

    with pytest.raises(sla.SlaPolicyError) as caught:
        sla.record_policy_version(
            db_session,
            sla.RecordPolicyVersionCommand(
                source=sla.SlaPolicySource.plan_family,
                effective_from=NOW,
                availability_target_percent=99.5,
                plan_family=sla.SlaPlanFamily.unlimited,
                subscription_id=subscription.id,
                context=_ctx(),
            ),
        )
    assert caught.value.code == "customer.service_level.invalid_scope"


def test_family_default_applies_through_the_subscribed_offer(
    db_session, subscription, catalog_offer, staged_owner_command
):
    """The whole point of the scope: terms set once on the family reach a
    subscription that never names them, via its offer's plan_family."""

    catalog_offer.plan_family = "unlimited"
    db_session.commit()

    sla.record_policy_version(
        db_session,
        sla.RecordPolicyVersionCommand(
            source=sla.SlaPolicySource.plan_family,
            effective_from=NOW - timedelta(days=10),
            availability_target_percent=99.5,
            plan_family=sla.SlaPlanFamily.unlimited,
            context=_ctx(),
        ),
    )

    resolved = sla.resolve_effective_policy(db_session, subscription, at=NOW)
    assert resolved is not None
    assert resolved.source is sla.SlaPolicySource.plan_family
    assert float(resolved.availability_target_percent) == 99.5


def test_offer_terms_outrank_the_family_default(
    db_session, subscription, catalog_offer, staged_owner_command
):
    """A plan that negotiates its own terms must not be overridden by the
    family it happens to sit in — that is the reason family sits BELOW
    offer_version in the precedence order."""

    catalog_offer.plan_family = "unlimited"
    db_session.commit()

    sla.record_policy_version(
        db_session,
        sla.RecordPolicyVersionCommand(
            source=sla.SlaPolicySource.plan_family,
            effective_from=NOW - timedelta(days=10),
            availability_target_percent=99.5,
            plan_family=sla.SlaPlanFamily.unlimited,
            context=_ctx(),
        ),
    )
    sla.record_policy_version(
        db_session,
        sla.RecordPolicyVersionCommand(
            source=sla.SlaPolicySource.offer_version,
            effective_from=NOW - timedelta(days=10),
            availability_target_percent=99.9,
            offer_id=catalog_offer.id,
            context=_ctx(),
        ),
    )

    resolved = sla.resolve_effective_policy(db_session, subscription, at=NOW)
    assert resolved is not None
    assert resolved.source is sla.SlaPolicySource.offer_version
    assert float(resolved.availability_target_percent) == 99.9


def test_family_default_outranks_internal_measurement(
    db_session, subscription, catalog_offer, staged_owner_command
):
    """A family default is a promise; internal measurement only states what we
    measure. The promise must win, or a global measurement row would mask
    every family's actual terms."""

    catalog_offer.plan_family = "dedicated"
    db_session.commit()

    sla.record_policy_version(
        db_session,
        sla.RecordPolicyVersionCommand(
            source=sla.SlaPolicySource.internal_measurement,
            effective_from=NOW - timedelta(days=20),
            availability_target_percent=95.0,
            context=_ctx(),
        ),
    )
    sla.record_policy_version(
        db_session,
        sla.RecordPolicyVersionCommand(
            source=sla.SlaPolicySource.plan_family,
            effective_from=NOW - timedelta(days=10),
            availability_target_percent=99.9,
            plan_family=sla.SlaPlanFamily.dedicated,
            context=_ctx(),
        ),
    )

    resolved = sla.resolve_effective_policy(db_session, subscription, at=NOW)
    assert resolved is not None
    assert resolved.source is sla.SlaPolicySource.plan_family


def test_family_terms_are_superseded_not_edited(
    db_session, catalog_offer, staged_owner_command
):
    """Family terms are append-only like every other scope: raising the target
    opens a new version and closes the old one, so a period already scored
    keeps the terms it was measured under."""

    first = sla.record_policy_version(
        db_session,
        sla.RecordPolicyVersionCommand(
            source=sla.SlaPolicySource.plan_family,
            effective_from=NOW - timedelta(days=60),
            availability_target_percent=99.0,
            plan_family=sla.SlaPlanFamily.home_flex,
            context=_ctx(),
        ),
    )
    second = sla.record_policy_version(
        db_session,
        sla.RecordPolicyVersionCommand(
            source=sla.SlaPolicySource.plan_family,
            effective_from=NOW - timedelta(days=30),
            availability_target_percent=99.5,
            plan_family=sla.SlaPlanFamily.home_flex,
            context=_ctx(),
        ),
    )

    assert second.version == 2
    assert second.superseded_version_id == first.policy_version_id
    assert first.policy_key == second.policy_key == "plan_family:home_flex"
