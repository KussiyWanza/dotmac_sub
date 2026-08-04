"""Typed lifecycle history contract (prerequisite for SLA eligibility).

The load-bearing behaviour is what this reports as NOT known. A contractual
score built on mutable pre-cutover rows must be able to say so, and absence of
evidence must never read as evidence of absence.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.models.catalog import SubscriptionStatus
from app.models.lifecycle import LifecycleEventType, SubscriptionLifecycleEvent
from app.services.subscription_lifecycle_history import (
    EvidenceGrade,
    transition_history,
)

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)


def _event(db, subscription, *, to, at, frm=None, grade="transition_evidence"):
    row = SubscriptionLifecycleEvent(
        subscription_id=subscription.id,
        event_type=LifecycleEventType.activate,
        from_status=frm,
        to_status=to,
        created_at=at,
        evidence_grade=grade,
    )
    db.add(row)
    db.flush()
    return row


def test_no_evidence_is_incomplete_not_an_empty_active_history(
    db_session, subscription
):
    """Absence of transitions is not proof the subscription was never active."""

    history = transition_history(db_session, subscription.id)

    assert history.transitions == ()
    assert history.active_windows == ()
    assert history.complete is False
    assert history.has_evidence is False


def test_active_then_suspended_yields_one_closed_window(db_session, subscription):
    _event(db_session, subscription, to=SubscriptionStatus.active, at=NOW)
    _event(
        db_session,
        subscription,
        frm=SubscriptionStatus.active,
        to=SubscriptionStatus.suspended,
        at=NOW + timedelta(days=5),
    )

    history = transition_history(db_session, subscription.id)

    assert len(history.active_windows) == 1
    assert history.active_windows[0].start == NOW
    assert history.active_windows[0].end == NOW + timedelta(days=5)
    assert history.complete is True


def test_a_still_active_subscription_has_an_open_window(db_session, subscription):
    _event(db_session, subscription, to=SubscriptionStatus.active, at=NOW)

    history = transition_history(db_session, subscription.id)

    assert len(history.active_windows) == 1
    assert history.active_windows[0].end is None, "open, not active forever"


def test_repeated_activation_does_not_open_a_second_window(db_session, subscription):
    """Double-counting the same entitlement would inflate the SLA denominator."""

    _event(db_session, subscription, to=SubscriptionStatus.active, at=NOW)
    _event(
        db_session,
        subscription,
        to=SubscriptionStatus.active,
        at=NOW + timedelta(days=1),
    )
    _event(
        db_session,
        subscription,
        frm=SubscriptionStatus.active,
        to=SubscriptionStatus.canceled,
        at=NOW + timedelta(days=3),
    )

    history = transition_history(db_session, subscription.id)

    assert len(history.active_windows) == 1
    assert history.active_windows[0].start == NOW
    assert history.active_windows[0].end == NOW + timedelta(days=3)


def test_pre_cutover_history_is_reported_incomplete(db_session, subscription):
    """Rows that were mutable for their whole life cannot be vouched for."""

    _event(
        db_session,
        subscription,
        to=SubscriptionStatus.active,
        at=NOW,
        grade="unsupported_pre_cutover",
    )

    history = transition_history(db_session, subscription.id)

    assert history.complete is False
    assert history.unsupported_transitions == 1
    assert history.earliest_supported_at is None
    # The windows are still derived — the caller decides what to do with
    # incomplete evidence; this contract does not hide it or discard it.
    assert len(history.active_windows) == 1


def test_mixed_history_is_incomplete_and_names_the_supported_boundary(
    db_session, subscription
):
    _event(
        db_session,
        subscription,
        to=SubscriptionStatus.active,
        at=NOW,
        grade="unsupported_pre_cutover",
    )
    _event(
        db_session,
        subscription,
        frm=SubscriptionStatus.active,
        to=SubscriptionStatus.suspended,
        at=NOW + timedelta(days=2),
    )

    history = transition_history(db_session, subscription.id)

    assert history.complete is False
    assert history.unsupported_transitions == 1
    assert history.earliest_supported_at == NOW + timedelta(days=2)


def test_an_unrecognised_grade_is_treated_as_unsupported(db_session, subscription):
    """Guessing 'probably fine' produces a confident wrong number."""

    _event(
        db_session,
        subscription,
        to=SubscriptionStatus.active,
        at=NOW,
        grade="something-new",
    )

    history = transition_history(db_session, subscription.id)

    assert history.transitions[0].grade is EvidenceGrade.unsupported_pre_cutover
    assert history.complete is False
