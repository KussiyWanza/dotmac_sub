"""The lifecycle owner is the only admission path to trusted evidence."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.models.catalog import SubscriptionStatus
from app.models.lifecycle import SubscriptionLifecycleEvent
from app.services.owner_commands import CommandContext
from app.services.subscription_lifecycle_evidence import (
    LifecycleEvidenceError,
    LifecycleEvidenceGrade,
    LifecycleEvidenceRetention,
    LifecycleEvidenceSource,
    RecordLifecycleEvidenceCommand,
    lifecycle_evidence_retention,
    record_current_state_baseline,
    record_lifecycle_evidence,
)

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
COMMAND_ID = UUID("11111111-1111-4111-8111-111111111111")


def _context(*, key: str = "transition:test") -> CommandContext:
    return CommandContext.system(
        command_id=COMMAND_ID,
        correlation_id=COMMAND_ID,
        actor="test.lifecycle_owner",
        scope="subscription:test",
        reason="Test lifecycle transition",
        idempotency_key=key,
    )


def _command(subscription, **overrides) -> RecordLifecycleEvidenceCommand:
    values = {
        "subscription_id": subscription.id,
        "from_status": SubscriptionStatus.pending,
        "to_status": SubscriptionStatus.active,
        "effective_at": NOW,
        "evidence_source": LifecycleEvidenceSource.lifecycle_command,
        "evidence_grade": LifecycleEvidenceGrade.transition_evidence,
        "context": _context(),
        "reason": "Activated",
    }
    values.update(overrides)
    return RecordLifecycleEvidenceCommand(**values)


def test_writer_appends_complete_evidence_and_replays_by_source_identity(
    db_session, subscription
):
    subscription.status = SubscriptionStatus.active
    db_session.flush()
    command = _command(subscription)

    first = record_lifecycle_evidence(db_session, command)
    replay = record_lifecycle_evidence(db_session, command)

    assert first.replayed is False
    assert replay.replayed is True
    assert replay.evidence_id == first.evidence_id
    row = db_session.get(SubscriptionLifecycleEvent, first.evidence_id)
    assert row is not None
    assert row.effective_at.replace(tzinfo=UTC) == NOW
    assert row.recorded_at is not None
    assert row.evidence_source == LifecycleEvidenceSource.lifecycle_command.value
    assert row.evidence_fingerprint.startswith("sha256:")


def test_same_source_identity_with_different_material_is_a_conflict(
    db_session, subscription
):
    subscription.status = SubscriptionStatus.active
    db_session.flush()
    record_lifecycle_evidence(db_session, _command(subscription))

    with pytest.raises(LifecycleEvidenceError) as caught:
        record_lifecycle_evidence(
            db_session,
            _command(subscription, reason="Different transition claim"),
        )

    assert caught.value.code.endswith(".idempotency_conflict")


def test_untrusted_transport_cannot_promote_an_observation(db_session, subscription):
    subscription.status = SubscriptionStatus.active
    db_session.flush()

    with pytest.raises(LifecycleEvidenceError) as caught:
        record_lifecycle_evidence(
            db_session,
            _command(
                subscription,
                evidence_source=LifecycleEvidenceSource.untrusted_observation,
            ),
        )

    assert caught.value.code.endswith(".untrusted_source")


def test_writer_refuses_evidence_before_status_is_applied(db_session, subscription):
    subscription.status = SubscriptionStatus.pending
    db_session.flush()

    with pytest.raises(LifecycleEvidenceError) as caught:
        record_lifecycle_evidence(db_session, _command(subscription))

    assert caught.value.code.endswith(".status_not_applied")


def test_baseline_is_prospective_and_carries_no_invented_from_state(
    db_session, subscription
):
    baseline = record_current_state_baseline(
        db_session,
        subscription=subscription,
        effective_at=NOW,
        evidence_source=LifecycleEvidenceSource.reconciliation_baseline,
        context=_context(key="baseline:test"),
    )

    row = db_session.get(SubscriptionLifecycleEvent, baseline.evidence_id)
    assert row is not None
    assert row.from_status is None
    assert row.to_status == subscription.status
    assert row.evidence_grade == LifecycleEvidenceGrade.state_baseline.value
    assert row.effective_at.replace(tzinfo=UTC) == NOW


def test_effective_time_must_be_timezone_aware(db_session, subscription):
    subscription.status = SubscriptionStatus.active
    db_session.flush()

    with pytest.raises(LifecycleEvidenceError) as caught:
        record_lifecycle_evidence(
            db_session,
            _command(subscription, effective_at=NOW.replace(tzinfo=None)),
        )

    assert caught.value.code.endswith(".naive_effective_at")


def test_retention_query_returns_one_exact_retained_identity(db_session, subscription):
    subscription.status = SubscriptionStatus.active
    db_session.flush()
    record_lifecycle_evidence(db_session, _command(subscription))

    retention = lifecycle_evidence_retention(
        db_session,
        subscription_id=subscription.id,
    )

    assert isinstance(retention, LifecycleEvidenceRetention)
    assert retention.subscription_id == subscription.id
    assert retention.retained_evidence_id is not None
    retained = db_session.get(
        SubscriptionLifecycleEvent,
        retention.retained_evidence_id,
    )
    assert retained is not None
    assert retained.subscription_id == subscription.id
    assert retention.blocks_deletion is True
