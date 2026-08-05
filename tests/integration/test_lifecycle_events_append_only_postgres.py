"""The append-only guarantee, proven where it is actually enforced.

The service no longer exposes `update`/`delete`, but a service can be re-added
by a later refactor and a database trigger cannot be argued with. SLA
eligibility will rest on these rows, so the guarantee has to hold against any
writer — ORM, raw SQL or a future admin tool.

Runs on the migrated PostgreSQL target from `make test-integration`; the
trigger is migration-only and a metadata-built schema does not have it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.models.catalog import SubscriptionStatus
from app.models.lifecycle import LifecycleEventType, SubscriptionLifecycleEvent

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)

# SQLAlchemy wraps psycopg's RestrictViolation (raised by the trigger) and
# CheckViolation in IntegrityError. Each test asserts on the specific message
# too, so a rejection for some unrelated reason cannot pass as the proof.
_REJECTED = IntegrityError


@pytest.fixture
def transition(db_session, subscription):
    evidence_id = uuid.uuid4()
    row = SubscriptionLifecycleEvent(
        id=evidence_id,
        subscription_id=subscription.id,
        event_type=LifecycleEventType.activate,
        to_status=SubscriptionStatus.active,
        created_at=NOW,
        evidence_grade="transition_evidence",
        evidence_source="lifecycle_command",
        source_id=f"test:{evidence_id}",
        evidence_fingerprint=f"sha256:{'a' * 64}",
        effective_at=NOW,
        recorded_at=NOW,
        notes="original",
    )
    db_session.add(row)
    db_session.flush()
    return row


def test_the_trigger_exists_in_the_migrated_schema(db_session):
    found = (
        db_session.execute(
            text(
                "SELECT tgname FROM pg_trigger "
                "WHERE tgrelid = 'subscription_lifecycle_events'::regclass "
                "AND NOT tgisinternal"
            )
        )
        .scalars()
        .all()
    )
    assert "trg_subscription_lifecycle_events_append_only" in found


def test_updating_a_transition_is_rejected(db_session, transition):
    """The exact hole: to_status was freely settable, so entitlement history
    could be rewritten after a period had been scored against it."""

    with pytest.raises(_REJECTED) as caught:
        db_session.execute(
            text(
                "UPDATE subscription_lifecycle_events "
                "SET to_status = 'suspended' WHERE id = :id"
            ),
            {"id": str(transition.id)},
        )
    assert "append-only" in str(caught.value)
    assert "UPDATE" in str(caught.value)
    db_session.rollback()


def test_rewriting_the_timestamp_is_rejected(db_session, transition):
    """created_at is the instant the window opens; moving it moves the
    customer's entitlement boundary."""

    with pytest.raises(_REJECTED) as caught:
        db_session.execute(
            text(
                "UPDATE subscription_lifecycle_events "
                "SET created_at = :when WHERE id = :id"
            ),
            {"when": NOW, "id": str(transition.id)},
        )
    assert "append-only" in str(caught.value)
    db_session.rollback()


def test_deleting_a_transition_is_rejected(db_session, transition):
    with pytest.raises(_REJECTED) as caught:
        db_session.execute(
            text("DELETE FROM subscription_lifecycle_events WHERE id = :id"),
            {"id": str(transition.id)},
        )
    assert "append-only" in str(caught.value)
    assert "DELETE" in str(caught.value)
    db_session.rollback()


def test_inserting_an_untrusted_observation_is_still_allowed(db_session, subscription):
    """Append-only, not read-only; raw inserts are admitted only as unsupported."""

    evidence_id = uuid.uuid4()
    db_session.execute(
        text(
            "INSERT INTO subscription_lifecycle_events "
            "(id, subscription_id, event_type, to_status, created_at) "
            "VALUES (:id, :sub, 'activate', 'active', :when)"
        ),
        {"id": str(evidence_id), "sub": str(subscription.id), "when": NOW},
    )
    grades = db_session.execute(
        text(
            "SELECT evidence_grade, evidence_source "
            "FROM subscription_lifecycle_events WHERE id = :id"
        ),
        {"id": str(evidence_id)},
    ).one()
    assert grades == ("unsupported_observation", "untrusted_observation")


def test_a_raw_writer_cannot_claim_trusted_evidence_without_the_full_shape(
    db_session, subscription
):
    with pytest.raises(_REJECTED) as caught:
        db_session.execute(
            text(
                "INSERT INTO subscription_lifecycle_events "
                "(id, subscription_id, event_type, to_status, created_at, "
                " evidence_grade, evidence_source) "
                "VALUES (:id, :sub, 'activate', 'active', :when, "
                "'transition_evidence', 'lifecycle_command')"
            ),
            {"id": str(uuid.uuid4()), "sub": str(subscription.id), "when": NOW},
        )
    assert "ck_subscription_lifecycle_events_trusted_shape" in str(caught.value)
    db_session.rollback()


def test_source_identity_arbitrates_replay(db_session, subscription, transition):
    with pytest.raises(_REJECTED) as caught:
        db_session.execute(
            text(
                "INSERT INTO subscription_lifecycle_events "
                "(id, subscription_id, event_type, to_status, created_at, "
                " evidence_grade, evidence_source, source_id, "
                " evidence_fingerprint, effective_at, recorded_at) "
                "VALUES (:id, :sub, 'activate', 'active', :when, "
                "'transition_evidence', 'lifecycle_command', :source_id, "
                ":fingerprint, :when, :when)"
            ),
            {
                "id": str(uuid.uuid4()),
                "sub": str(subscription.id),
                "when": NOW,
                "source_id": transition.source_id,
                "fingerprint": f"sha256:{'b' * 64}",
            },
        )
    assert "uq_subscription_lifecycle_events_source_identity" in str(caught.value)
    db_session.rollback()


def test_the_evidence_grade_is_constrained(db_session, subscription):
    with pytest.raises(_REJECTED) as caught:
        db_session.execute(
            text(
                "INSERT INTO subscription_lifecycle_events "
                "(id, subscription_id, event_type, created_at, evidence_grade) "
                "VALUES (:id, :sub, 'activate', :when, 'invented-grade')"
            ),
            {"id": str(uuid.uuid4()), "sub": str(subscription.id), "when": NOW},
        )
    assert "ck_subscription_lifecycle_events_evidence_grade" in str(caught.value)
    db_session.rollback()
