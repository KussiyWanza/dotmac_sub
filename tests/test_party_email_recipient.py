"""Which of a party's addresses a document is delivered to.

One rule, owned by the party service. Every document-delivery path (quote
today, shared catalog next) resolves through it rather than cloning the
precedence and letting the copies drift.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.models.party import (
    Party,
    PartyContactPoint,
    PartyContactPointType,
    PartyIdentityStatus,
    PartyType,
)
from app.services import party as party_service


def _party(db, display_name="Amina Bello"):
    party = Party(
        party_type=PartyType.person.value,
        display_name=display_name,
        status=PartyIdentityStatus.active.value,
    )
    db.add(party)
    db.flush()
    return party


def _email(db, party, value, *, primary=False, active=True, created_at=None):
    point = PartyContactPoint(
        party_id=party.id,
        channel_type=PartyContactPointType.email.value,
        normalized_value=value,
        is_primary=primary,
        is_active=active,
    )
    if created_at is not None:
        point.created_at = created_at
    db.add(point)
    db.flush()
    return point


def test_primary_wins_regardless_of_insertion_order(db_session):
    """A non-primary address added first must not capture delivery."""
    party = _party(db_session)
    _email(db_session, party, "other@example.com", primary=False)
    primary = _email(db_session, party, "amina@example.com", primary=True)

    recipient = party_service.resolve_email_recipient(db_session, party.id)

    assert recipient is not None
    assert recipient.email == "amina@example.com"
    assert recipient.contact_point_id == primary.id
    assert recipient.display_name == "Amina Bello"


def test_with_no_primary_the_oldest_address_wins(db_session):
    """Precedence is a total order — primary, then oldest, then id — so two
    equally-primary addresses cannot make delivery depend on row order."""
    party = _party(db_session)
    now = datetime.now(UTC)
    older = _email(
        db_session, party, "first@example.com", created_at=now - timedelta(days=5)
    )
    _email(db_session, party, "second@example.com", created_at=now)

    recipient = party_service.resolve_email_recipient(db_session, party.id)

    assert recipient is not None
    assert recipient.contact_point_id == older.id


def test_inactive_addresses_are_never_delivered_to(db_session):
    """Deactivating an address must actually stop mail reaching it."""
    party = _party(db_session)
    _email(db_session, party, "retired@example.com", primary=True, active=False)
    live = _email(db_session, party, "current@example.com", primary=False)

    recipient = party_service.resolve_email_recipient(db_session, party.id)

    assert recipient is not None
    assert recipient.contact_point_id == live.id


def test_a_party_with_no_deliverable_address_returns_none(db_session):
    """An ordinary state for a lead captured by phone — not an error here.
    Each caller phrases its own refusal."""
    party = _party(db_session)

    assert party_service.resolve_email_recipient(db_session, party.id) is None


def test_only_inactive_addresses_returns_none(db_session):
    party = _party(db_session)
    _email(db_session, party, "retired@example.com", primary=True, active=False)

    assert party_service.resolve_email_recipient(db_session, party.id) is None


def test_a_blank_address_is_not_deliverable(db_session):
    """An empty contact point would otherwise resolve and then fail at send."""
    party = _party(db_session)
    _email(db_session, party, "   ", primary=True)

    assert party_service.resolve_email_recipient(db_session, party.id) is None


def test_a_non_email_channel_is_not_used_for_email_delivery(db_session):
    party = _party(db_session)
    point = PartyContactPoint(
        party_id=party.id,
        channel_type=PartyContactPointType.phone.value,
        normalized_value="+2348000000000",
        is_primary=True,
        is_active=True,
    )
    db_session.add(point)
    db_session.flush()

    assert party_service.resolve_email_recipient(db_session, party.id) is None


def test_an_unknown_party_returns_none(db_session):
    from uuid import uuid4

    assert party_service.resolve_email_recipient(db_session, uuid4()) is None


def test_the_recipient_is_immutable_evidence(db_session):
    """Callers persist contact_point_id alongside the delivery record, so the
    resolved recipient must not be editable after the fact."""
    import pytest

    party = _party(db_session)
    _email(db_session, party, "amina@example.com", primary=True)

    recipient = party_service.resolve_email_recipient(db_session, party.id)

    assert recipient is not None
    with pytest.raises(Exception):
        recipient.email = "attacker@example.com"  # type: ignore[misc]
