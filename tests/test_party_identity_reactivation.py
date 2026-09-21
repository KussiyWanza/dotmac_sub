from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.models.audit import AuditEvent
from app.models.event_store import EventStore
from app.models.party import Party, PartyIdentityStatus, PartyType
from app.services import party
from app.services.events.types import EventType
from app.services.owner_commands import CommandContext
from app.services.party_identity_reactivation import (
    AUDIT_ACTION,
    COMMAND_SCOPE,
    PartyIdentityReactivationError,
    PartyReactivationDecisionSource,
    ReactivateQuarantinedPartyCommand,
    reactivate_quarantined_party,
)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _quarantined_party(db_session):
    target = party.create_party(
        db_session,
        party_type=PartyType.person,
        display_name="Reviewed staff identity",
    )
    target.status = PartyIdentityStatus.quarantined.value
    target.merge_reason = "Ambiguous import identity"
    db_session.flush()
    party_id = target.id
    expected_updated_at = _aware(target.updated_at)
    db_session.commit()
    return party_id, expected_updated_at


def _command(
    *, party_id, expected_updated_at, command_id=None, actor_id=None, reason=None
):
    command_id = command_id or uuid4()
    actor_id = actor_id or uuid4()
    return ReactivateQuarantinedPartyCommand(
        context=CommandContext(
            command_id=command_id,
            correlation_id=command_id,
            actor=f"user:{actor_id}",
            scope=COMMAND_SCOPE,
            reason="Reviewed canonical Party identity reactivation",
            idempotency_key=f"test-party-reactivation:{command_id}",
        ),
        party_id=party_id,
        expected_party_type=PartyType.person,
        expected_updated_at=expected_updated_at,
        reviewed_by_user_id=actor_id,
        reviewed_at=datetime.now(UTC),
        decision_source=PartyReactivationDecisionSource.administrative_review,
        review_reason=reason
        or "Administrator resolved the ambiguous identity evidence.",
    )


def test_reactivates_quarantined_party_with_audit_and_event(db_session):
    party_id, expected_updated_at = _quarantined_party(db_session)
    command = _command(party_id=party_id, expected_updated_at=expected_updated_at)

    outcome = reactivate_quarantined_party(db_session, command)

    assert outcome.party_id == party_id
    assert outcome.previous_status is PartyIdentityStatus.quarantined
    assert outcome.current_status is PartyIdentityStatus.active
    assert outcome.replayed is False
    refreshed = db_session.get(Party, party_id)
    assert refreshed is not None
    assert refreshed.status == PartyIdentityStatus.active.value
    assert refreshed.merge_reason is None
    audit = (
        db_session.query(AuditEvent)
        .filter_by(action=AUDIT_ACTION, entity_id=str(party_id))
        .one()
    )
    assert audit.metadata_["previous_status"] == "quarantined"
    assert audit.metadata_["current_status"] == "active"
    assert "review_reason" not in audit.metadata_
    event = (
        db_session.query(EventStore)
        .filter_by(event_type=EventType.party_identity_reactivated.value)
        .one()
    )
    assert event.payload["aggregate_id"] == str(party_id)
    assert "review_reason" not in event.payload


def test_exact_command_replays_without_second_audit_or_event(db_session):
    party_id, expected_updated_at = _quarantined_party(db_session)
    command = _command(party_id=party_id, expected_updated_at=expected_updated_at)

    first = reactivate_quarantined_party(db_session, command)
    replay = reactivate_quarantined_party(db_session, command)

    assert first.replayed is False
    assert replay.replayed is True
    assert db_session.query(AuditEvent).filter_by(action=AUDIT_ACTION).count() == 1
    assert (
        db_session.query(EventStore)
        .filter_by(event_type=EventType.party_identity_reactivated.value)
        .count()
        == 1
    )


def test_active_party_without_matching_evidence_is_refused(db_session):
    target = party.create_party(
        db_session, party_type=PartyType.person, display_name="Already active"
    )
    db_session.flush()
    party_id = target.id
    expected_updated_at = _aware(target.updated_at)
    db_session.commit()
    command = _command(
        party_id=party_id,
        expected_updated_at=expected_updated_at,
    )

    with pytest.raises(PartyIdentityReactivationError, match="matching"):
        reactivate_quarantined_party(db_session, command)


@pytest.mark.parametrize(
    "status", (PartyIdentityStatus.archived, PartyIdentityStatus.merged)
)
def test_archived_and_merged_parties_are_refused(db_session, status):
    target = party.create_party(
        db_session, party_type=PartyType.person, display_name="Unavailable identity"
    )
    if status is PartyIdentityStatus.merged:
        canonical = party.create_party(
            db_session, party_type=PartyType.person, display_name="Canonical identity"
        )
        target.merged_into_party_id = canonical.id
    target.status = status.value
    db_session.flush()
    party_id = target.id
    expected_updated_at = _aware(target.updated_at)
    db_session.commit()
    command = _command(
        party_id=party_id,
        expected_updated_at=expected_updated_at,
    )

    with pytest.raises(PartyIdentityReactivationError, match="Only a quarantined"):
        reactivate_quarantined_party(db_session, command)


def test_stale_party_and_mismatched_actor_are_refused(db_session):
    party_id, expected_updated_at = _quarantined_party(db_session)
    stale = _command(
        party_id=party_id,
        expected_updated_at=expected_updated_at - timedelta(seconds=1),
    )
    with pytest.raises(PartyIdentityReactivationError, match="changed"):
        reactivate_quarantined_party(db_session, stale)

    actor_mismatch = _command(
        party_id=party_id,
        expected_updated_at=expected_updated_at,
    )
    actor_mismatch = replace(actor_mismatch, reviewed_by_user_id=uuid4())
    with pytest.raises(PartyIdentityReactivationError, match="must match"):
        reactivate_quarantined_party(db_session, actor_mismatch)
