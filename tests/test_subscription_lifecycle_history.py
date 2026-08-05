"""Honest, period-scoped lifecycle history for SLA eligibility."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.models.catalog import BillingMode, Subscription, SubscriptionStatus
from app.models.lifecycle import LifecycleEventType, SubscriptionLifecycleEvent
from app.services.subscription_lifecycle_evidence import (
    LifecycleEvidenceGrade,
    LifecycleEvidenceSource,
)
from app.services.subscription_lifecycle_history import (
    lifecycle_history_for_period,
    transition_history,
)

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)


@pytest.fixture
def history_subscription(db_session, subscriber, catalog_offer):
    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=SubscriptionStatus.active,
        billing_mode=BillingMode.postpaid,
        start_at=NOW,
    )
    db_session.add(subscription)
    db_session.flush()
    return subscription


def _event(
    db,
    subscription,
    *,
    to: SubscriptionStatus,
    at: datetime,
    frm: SubscriptionStatus | None = None,
    grade: LifecycleEvidenceGrade = LifecycleEvidenceGrade.transition_evidence,
    source: LifecycleEvidenceSource = LifecycleEvidenceSource.lifecycle_command,
    effective: bool = True,
):
    evidence_id = uuid4()
    trusted = grade in {
        LifecycleEvidenceGrade.transition_evidence,
        LifecycleEvidenceGrade.state_baseline,
    }
    row = SubscriptionLifecycleEvent(
        id=evidence_id,
        subscription_id=subscription.id,
        event_type=LifecycleEventType.activate,
        from_status=frm,
        to_status=to,
        created_at=at,
        evidence_grade=grade.value,
        evidence_source=source.value,
        source_id=f"test:{evidence_id}" if trusted else None,
        evidence_fingerprint=f"sha256:{'a' * 64}" if trusted else None,
        effective_at=at if effective else None,
        recorded_at=at if trusted else None,
    )
    db.add(row)
    db.flush()
    return row


def _baseline(db, subscription, *, status, at):
    return _event(
        db,
        subscription,
        to=status,
        at=at,
        grade=LifecycleEvidenceGrade.state_baseline,
        source=LifecycleEvidenceSource.cutover_baseline,
    )


def test_no_evidence_is_incomplete_not_empty_proof(db_session, history_subscription):
    history = transition_history(db_session, history_subscription.id)

    assert history.transitions == ()
    assert history.active_windows == ()
    assert history.complete is False
    assert history.has_evidence is False


def test_trusted_active_then_suspended_yields_one_closed_window(
    db_session, history_subscription
):
    _baseline(
        db_session,
        history_subscription,
        status=SubscriptionStatus.active,
        at=NOW,
    )
    _event(
        db_session,
        history_subscription,
        frm=SubscriptionStatus.active,
        to=SubscriptionStatus.suspended,
        at=NOW + timedelta(days=5),
    )

    history = transition_history(db_session, history_subscription.id)

    assert history.complete is True
    assert [(window.start, window.end) for window in history.active_windows] == [
        (NOW, NOW + timedelta(days=5))
    ]


def test_period_needs_a_supported_left_edge(db_session, history_subscription):
    _baseline(
        db_session,
        history_subscription,
        status=SubscriptionStatus.active,
        at=NOW + timedelta(days=1),
    )

    history = lifecycle_history_for_period(
        db_session,
        history_subscription.id,
        period_start=NOW,
        period_end=NOW + timedelta(days=3),
    )

    assert history.complete is False
    assert history.active_windows == (
        type(history.active_windows[0])(
            start=NOW + timedelta(days=1),
            end=NOW + timedelta(days=3),
        ),
    )
    assert "missing_supported_left_edge" in history.issues


def test_untrusted_observation_breaks_coverage_without_becoming_state(
    db_session, history_subscription
):
    _baseline(
        db_session,
        history_subscription,
        status=SubscriptionStatus.active,
        at=NOW,
    )
    observation = _event(
        db_session,
        history_subscription,
        to=SubscriptionStatus.suspended,
        at=NOW + timedelta(days=2),
        grade=LifecycleEvidenceGrade.unsupported_observation,
        source=LifecycleEvidenceSource.untrusted_observation,
    )

    history = lifecycle_history_for_period(
        db_session,
        history_subscription.id,
        period_start=NOW + timedelta(days=1),
        period_end=NOW + timedelta(days=4),
    )

    assert history.complete is False
    assert [(window.start, window.end) for window in history.active_windows] == [
        (NOW + timedelta(days=1), NOW + timedelta(days=2))
    ]
    assert f"unsupported_observation:{observation.id}" in history.issues


def test_later_baseline_restores_only_future_periods(db_session, history_subscription):
    _baseline(
        db_session,
        history_subscription,
        status=SubscriptionStatus.active,
        at=NOW,
    )
    _event(
        db_session,
        history_subscription,
        to=SubscriptionStatus.suspended,
        at=NOW + timedelta(days=1),
        grade=LifecycleEvidenceGrade.unsupported_observation,
        source=LifecycleEvidenceSource.untrusted_observation,
    )
    _baseline(
        db_session,
        history_subscription,
        status=SubscriptionStatus.active,
        at=NOW + timedelta(days=2),
    )

    spanning = lifecycle_history_for_period(
        db_session,
        history_subscription.id,
        period_start=NOW,
        period_end=NOW + timedelta(days=3),
    )
    future = lifecycle_history_for_period(
        db_session,
        history_subscription.id,
        period_start=NOW + timedelta(days=2, hours=1),
        period_end=NOW + timedelta(days=3),
    )

    assert spanning.complete is False
    assert future.complete is True
    assert [(window.start, window.end) for window in future.active_windows] == [
        (NOW + timedelta(days=2, hours=1), NOW + timedelta(days=3))
    ]


def test_legacy_row_does_not_poison_a_later_cutover_baseline(
    db_session, history_subscription
):
    _event(
        db_session,
        history_subscription,
        to=SubscriptionStatus.active,
        at=NOW,
        grade=LifecycleEvidenceGrade.unsupported_pre_cutover,
        source=LifecycleEvidenceSource.legacy_unattributed,
        effective=False,
    )
    baseline = _baseline(
        db_session,
        history_subscription,
        status=SubscriptionStatus.active,
        at=NOW + timedelta(days=1),
    )

    history = lifecycle_history_for_period(
        db_session,
        history_subscription.id,
        period_start=NOW + timedelta(days=2),
        period_end=NOW + timedelta(days=3),
    )

    assert history.complete is True
    assert history.supporting_evidence_ids == (baseline.id,)


def test_discontinuous_trusted_lineage_is_incomplete(db_session, history_subscription):
    _baseline(
        db_session,
        history_subscription,
        status=SubscriptionStatus.active,
        at=NOW,
    )
    broken = _event(
        db_session,
        history_subscription,
        frm=SubscriptionStatus.pending,
        to=SubscriptionStatus.suspended,
        at=NOW + timedelta(days=1),
    )

    history = lifecycle_history_for_period(
        db_session,
        history_subscription.id,
        period_start=NOW,
        period_end=NOW + timedelta(days=2),
    )

    assert history.complete is False
    assert f"lineage_discontinuity:{broken.id}" in history.issues
