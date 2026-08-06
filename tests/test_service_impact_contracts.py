"""Pins the outage/SLA spine's shared vocabulary and approved constants.

docs/designs/OUTAGE_SLA_SPINE.md is the design contract; these tests keep the
typed vocabulary honest: exposure is not downtime, unknown is never uptime,
history is append-only, and the approved policy constants cannot drift
silently.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.services import service_impact_contracts as contracts

NOW = datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC)


def _evidence(kind=contracts.ImpactEvidenceKind.shared_boundary_failure):
    return contracts.ImpactEvidence(
        kind=kind,
        owner="network.device_state",
        observed_at=NOW,
        reference="obs:1",
    )


# --- approved constants ----------------------------------------------------


def test_approved_policy_constants_cannot_drift_silently():
    assert contracts.RECOVERY_HOLD_MIN_OBSERVATIONS == 2
    assert contracts.RECOVERY_HOLD_WINDOW == timedelta(minutes=5)
    assert contracts.MAINTENANCE_NOTICE_DAYS == 7
    assert contracts.SLA_CALENDAR_TIMEZONE == "Africa/Lagos"
    assert contracts.MAJOR_OUTAGE_COMPENSATION_THRESHOLD == timedelta(hours=24)


def test_impact_vocabulary_is_exactly_the_approved_six_states():
    assert {state.value for state in contracts.ImpactState} == {
        "potentially_affected",
        "confirmed_unavailable",
        "degraded",
        "restored",
        "unknown",
        "excluded",
    }
    # Exposure is never an accruing state; unknown never accrues either.
    assert contracts.ImpactState.potentially_affected not in (
        contracts.ACCRUING_IMPACT_STATES
    )
    assert contracts.ImpactState.unknown not in contracts.ACCRUING_IMPACT_STATES


def test_sla_verdicts_include_no_contractual_sla():
    assert "no_contractual_sla" in {v.value for v in contracts.SlaVerdict}


def test_policy_precedence_order():
    assert [source.value for source in contracts.SlaPolicySource] == [
        "subscription_contract",
        "account_contract",
        "offer_version",
        "internal_measurement",
    ]


def test_maintenance_lifecycle_states():
    assert {state.value for state in contracts.MaintenanceState} == {
        "draft",
        "approved",
        "announced",
        "in_progress",
        "completed",
        "canceled",
        "overrun",
    }


# --- evidence and scope history --------------------------------------------


def test_evidence_requires_utc_owner_and_reference():
    with pytest.raises(ValueError):
        contracts.ImpactEvidence(
            kind=contracts.ImpactEvidenceKind.independent_observation,
            owner="network.device_state",
            observed_at=NOW.replace(tzinfo=None),
            reference="obs:1",
        )
    with pytest.raises(ValueError):
        contracts.ImpactEvidence(
            kind=contracts.ImpactEvidenceKind.independent_observation,
            owner="",
            observed_at=NOW,
            reference="obs:1",
        )


def test_scope_revision_invariants():
    revision = contracts.ScopeRevision(
        incident_id=uuid.uuid4(),
        sequence=1,
        effective_at=NOW,
        old_scope_type=None,
        old_scope_id=None,
        new_scope_type="node",
        new_scope_id=uuid.uuid4(),
        reason="initial declaration",
        membership_token="sha256:abc",
        entering_subscription_count=42,
        leaving_subscription_count=0,
        evidence=(_evidence(),),
    )
    assert revision.sequence == 1

    with pytest.raises(ValueError):
        contracts.ScopeRevision(
            incident_id=uuid.uuid4(),
            sequence=0,
            effective_at=NOW,
            old_scope_type=None,
            old_scope_id=None,
            new_scope_type="node",
            new_scope_id=uuid.uuid4(),
            reason="bad",
            membership_token="t",
            entering_subscription_count=0,
            leaving_subscription_count=0,
        )


# --- intervals -------------------------------------------------------------


def test_exact_confirmed_interval_requires_first_evidence():
    with pytest.raises(ValueError):
        contracts.ImpactInterval(
            incident_id=uuid.uuid4(),
            subscription_id=uuid.uuid4(),
            state=contracts.ImpactState.confirmed_unavailable,
            quality=contracts.IntervalQuality.exact,
            started_at=NOW,
            scope_revision_sequence=1,
            idempotency_key="k",
        )


def test_exposure_is_not_an_interval_state():
    with pytest.raises(ValueError):
        contracts.ImpactInterval(
            incident_id=uuid.uuid4(),
            subscription_id=uuid.uuid4(),
            state=contracts.ImpactState.potentially_affected,
            quality=contracts.IntervalQuality.exact,
            started_at=NOW,
            scope_revision_sequence=1,
            idempotency_key="k",
        )


def test_interval_duration_and_ordering():
    interval = contracts.ImpactInterval(
        incident_id=uuid.uuid4(),
        subscription_id=uuid.uuid4(),
        state=contracts.ImpactState.confirmed_unavailable,
        quality=contracts.IntervalQuality.exact,
        started_at=NOW,
        ended_at=NOW + timedelta(minutes=30),
        scope_revision_sequence=1,
        idempotency_key="k",
        first_evidence=_evidence(),
        recovery_evidence=_evidence(contracts.ImpactEvidenceKind.recovery_observation),
    )
    assert interval.duration == timedelta(minutes=30)

    with pytest.raises(ValueError):
        contracts.ImpactInterval(
            incident_id=uuid.uuid4(),
            subscription_id=uuid.uuid4(),
            state=contracts.ImpactState.confirmed_unavailable,
            quality=contracts.IntervalQuality.exact,
            started_at=NOW,
            ended_at=NOW - timedelta(minutes=1),
            scope_revision_sequence=1,
            idempotency_key="k",
            first_evidence=_evidence(),
        )


def test_unknown_periods_are_recordable_without_evidence():
    interval = contracts.ImpactInterval(
        incident_id=uuid.uuid4(),
        subscription_id=uuid.uuid4(),
        state=contracts.ImpactState.unknown,
        quality=contracts.IntervalQuality.unavailable,
        started_at=NOW,
        scope_revision_sequence=1,
        idempotency_key="k",
    )
    assert interval.duration is None


# --- SLA policy and scores -------------------------------------------------


def _policy(**overrides):
    defaults = dict(
        policy_id=uuid.uuid4(),
        version=1,
        source=contracts.SlaPolicySource.subscription_contract,
        effective_from=NOW - timedelta(days=30),
        effective_to=None,
        availability_target_percent=99.5,
    )
    defaults.update(overrides)
    return contracts.SlaPolicyVersion(**defaults)


def test_contractual_policy_requires_a_target():
    with pytest.raises(ValueError):
        _policy(availability_target_percent=None)
    # Internal measurement policies may exist without a contractual target.
    internal = _policy(
        source=contracts.SlaPolicySource.internal_measurement,
        availability_target_percent=None,
    )
    assert internal.availability_target_percent is None


def _score(**overrides):
    defaults = dict(
        subscription_id=uuid.uuid4(),
        period_start=NOW - timedelta(days=31),
        period_end=NOW,
        eligible_seconds=2_000_000,
        unavailable_seconds=3_600,
        excluded_seconds=0,
        unknown_seconds=0,
        verdict=contracts.SlaVerdict.passing,
        policy=_policy(),
        evidence_digest="sha256:score",
    )
    defaults.update(overrides)
    return contracts.SlaScore(**defaults)


def test_score_without_policy_cannot_claim_a_verdict():
    with pytest.raises(ValueError):
        _score(policy=None, verdict=contracts.SlaVerdict.passing)
    honest = _score(policy=None, verdict=contracts.SlaVerdict.no_contractual_sla)
    assert honest.measured_availability_percent is not None


def test_unknown_seconds_make_the_score_provisional_not_uptime():
    score = _score(
        unknown_seconds=7_200,
        evidence_complete=False,
        completeness_issues=("monitoring:unknown_eligible_coverage",),
        verdict=contracts.SlaVerdict.unavailable,
    )
    assert score.is_provisional is True
    assert score.measured_availability_percent is None
    assert score.availability_lower_bound_percent == round(
        100.0 * (2_000_000 - 3_600 - 7_200) / 2_000_000, 4
    )
    assert score.availability_upper_bound_percent == round(
        100.0 * (2_000_000 - 3_600) / 2_000_000, 4
    )


def test_incomplete_evidence_cannot_claim_passing_or_at_risk():
    for verdict in (
        contracts.SlaVerdict.passing,
        contracts.SlaVerdict.at_risk,
    ):
        with pytest.raises(ValueError):
            _score(
                evidence_complete=False,
                completeness_issues=("lifecycle:missing_supported_left_edge",),
                verdict=verdict,
            )


def test_reviewed_exclusions_leave_the_availability_denominator():
    score = _score(eligible_seconds=1000, excluded_seconds=100, unavailable_seconds=9)

    assert score.measured_availability_percent == 99.0


def test_score_accounting_cannot_exceed_eligible_time():
    with pytest.raises(ValueError):
        _score(unavailable_seconds=1_999_999, unknown_seconds=2)


def test_zero_eligible_time_yields_no_availability_claim():
    score = _score(
        eligible_seconds=0,
        unavailable_seconds=0,
        unknown_seconds=0,
        verdict=contracts.SlaVerdict.unavailable,
    )
    assert score.measured_availability_percent is None
