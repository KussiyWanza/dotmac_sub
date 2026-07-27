"""Conversation participants: the endpoints observed taking part in a thread.

Shadow projection. These tests pin the two separations the model exists to
hold — endpoint before party, admission source before relationship — and the
fact that nothing reads it for a decision yet.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.models.service_team import ServiceTeam, ServiceTeamType
from app.models.team_inbox import (
    InboxChannelType,
    InboxConversation,
    InboxConversationParticipant,
    InboxConversationStatus,
    InboxMessage,
    InboxParticipantAdmissionSource,
    InboxParticipantRelationship,
    TeamInboxEmailRoute,
)
from app.services import team_inbox_participants, team_inbox_receive


def _conversation(db_session, *, channel=InboxChannelType.email.value):
    conversation = InboxConversation(
        channel_type=channel,
        subject="Line fault",
        contact_address="customer@example.com",
        status=InboxConversationStatus.open.value,
    )
    db_session.add(conversation)
    db_session.flush()
    return conversation


def _message(
    db_session,
    conversation,
    *,
    direction="inbound",
    from_address=None,
    to=None,
    cc=None,
    channel=None,
    metadata=None,
):
    message = InboxMessage(
        conversation_id=conversation.id,
        channel_type=channel or conversation.channel_type,
        direction=direction,
        body="Body.",
        from_address=from_address,
        to_addresses=to or [],
        cc_addresses=cc or [],
        received_at=datetime.now(UTC) if direction == "inbound" else None,
        sent_at=None if direction == "inbound" else datetime.now(UTC),
        metadata_=metadata,
    )
    db_session.add(message)
    db_session.flush()
    return message


def _route(db_session, address: str) -> None:
    team = ServiceTeam(name="Support", team_type=ServiceTeamType.support.value)
    db_session.add(team)
    db_session.flush()
    db_session.add(
        TeamInboxEmailRoute(
            service_team_id=team.id,
            email_address=address.lower(),
            is_active=True,
            priority=100,
        )
    )
    db_session.flush()


# --- what counts as a participant -------------------------------------------


def test_from_to_and_cc_are_all_admitted(db_session):
    """A colleague addressed directly is as much a participant as one copied."""
    _route(db_session, "support@dotmac.io")
    conversation = _conversation(db_session)
    message = _message(
        db_session,
        conversation,
        from_address="customer@example.com",
        to=["support@dotmac.io", "colleague@example.com"],
        cc=["vendor@partner.test"],
    )

    team_inbox_participants.record_message_participants(
        db_session, conversation=conversation, message=message
    )

    rows = team_inbox_participants.list_participants(
        db_session, conversation_id=conversation.id
    )
    by_endpoint = {row.normalized_endpoint: row for row in rows}
    assert set(by_endpoint) == {
        "customer@example.com",
        "colleague@example.com",
        "vendor@partner.test",
    }
    assert (
        by_endpoint["customer@example.com"].admission_source
        == InboxParticipantAdmissionSource.inbound_from.value
    )
    assert (
        by_endpoint["colleague@example.com"].admission_source
        == InboxParticipantAdmissionSource.inbound_to.value
    )
    assert (
        by_endpoint["vendor@partner.test"].admission_source
        == InboxParticipantAdmissionSource.inbound_cc.value
    )


def test_our_own_mailbox_is_never_a_participant(db_session):
    """The routing table is the register of what is ours."""
    _route(db_session, "support@dotmac.io")
    conversation = _conversation(db_session)
    message = _message(
        db_session,
        conversation,
        from_address="customer@example.com",
        to=["support@dotmac.io"],
    )

    team_inbox_participants.record_message_participants(
        db_session, conversation=conversation, message=message
    )

    endpoints = {
        row.normalized_endpoint
        for row in team_inbox_participants.list_participants(
            db_session, conversation_id=conversation.id
        )
    }
    assert "support@dotmac.io" not in endpoints


def test_a_retired_mailbox_is_still_ours(db_session):
    """Old messages carry addresses we have since stopped routing."""
    _route(db_session, "oldsupport@dotmac.io")
    route = db_session.query(TeamInboxEmailRoute).one()
    route.is_active = False
    db_session.flush()
    conversation = _conversation(db_session)
    message = _message(
        db_session,
        conversation,
        from_address="customer@example.com",
        to=["oldsupport@dotmac.io"],
    )

    team_inbox_participants.record_message_participants(
        db_session, conversation=conversation, message=message
    )

    endpoints = {
        row.normalized_endpoint
        for row in team_inbox_participants.list_participants(
            db_session, conversation_id=conversation.id
        )
    }
    assert endpoints == {"customer@example.com"}


def test_an_outbound_sender_is_not_a_participant(db_session):
    conversation = _conversation(db_session)
    message = _message(
        db_session,
        conversation,
        direction="outbound",
        from_address="support@dotmac.io",
        to=["customer@example.com"],
    )

    team_inbox_participants.record_message_participants(
        db_session, conversation=conversation, message=message
    )

    rows = team_inbox_participants.list_participants(
        db_session, conversation_id=conversation.id
    )
    assert [row.normalized_endpoint for row in rows] == ["customer@example.com"]
    assert rows[0].admission_source == (
        InboxParticipantAdmissionSource.outbound_to.value
    )


# --- the separations the model exists to hold --------------------------------


def test_a_participant_starts_with_no_party_binding(db_session):
    """Inbox owns that an endpoint took part; Party owns whose it is.

    A mandatory Party FK would make an unknown colleague unrepresentable.
    """
    conversation = _conversation(db_session)
    message = _message(db_session, conversation, from_address="stranger@nowhere.test")

    team_inbox_participants.record_message_participants(
        db_session, conversation=conversation, message=message
    )

    row = team_inbox_participants.list_participants(
        db_session, conversation_id=conversation.id
    )[0]
    assert row.party_contact_point_id is None
    assert row.relationship_type == InboxParticipantRelationship.unknown.value


def test_relationship_is_not_admission_source(db_session):
    """A customer may be copied and a third party may be the sender.

    Reclassifying a participant must leave the evidence of how it arrived
    untouched, so the two are stored separately.
    """
    conversation = _conversation(db_session)
    message = _message(
        db_session,
        conversation,
        from_address="vendor@partner.test",
        cc=["customer@example.com"],
    )
    team_inbox_participants.record_message_participants(
        db_session, conversation=conversation, message=message
    )

    customer = (
        db_session.query(InboxConversationParticipant)
        .filter(
            InboxConversationParticipant.normalized_endpoint == "customer@example.com"
        )
        .one()
    )
    customer.relationship_type = InboxParticipantRelationship.customer.value
    db_session.flush()

    assert customer.relationship_type == InboxParticipantRelationship.customer.value
    assert customer.admission_source == InboxParticipantAdmissionSource.inbound_cc.value


def test_party_evidence_is_all_or_nothing(db_session):
    """Same rule as inbox_contact_links: a binding carries its evidence."""
    from sqlalchemy.exc import IntegrityError

    conversation = _conversation(db_session)
    db_session.add(
        InboxConversationParticipant(
            conversation_id=conversation.id,
            channel_type=InboxChannelType.email.value,
            normalized_endpoint="bound@example.com",
            admission_source=InboxParticipantAdmissionSource.inbound_from.value,
            party_contact_point_id=uuid.uuid4(),
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


# --- idempotency and lifecycle ----------------------------------------------


def test_the_same_address_on_many_messages_admits_one_participant(db_session):
    conversation = _conversation(db_session)
    for _ in range(3):
        message = _message(
            db_session, conversation, from_address="customer@example.com"
        )
        team_inbox_participants.record_message_participants(
            db_session, conversation=conversation, message=message
        )

    rows = team_inbox_participants.list_participants(
        db_session, conversation_id=conversation.id
    )
    assert len(rows) == 1


def test_a_later_sighting_does_not_rewrite_how_it_arrived(db_session):
    """First admission keeps its source; history is not re-derived."""
    conversation = _conversation(db_session)
    first = _message(db_session, conversation, cc=["someone@example.com"])
    team_inbox_participants.record_message_participants(
        db_session, conversation=conversation, message=first
    )
    later = _message(db_session, conversation, from_address="someone@example.com")
    team_inbox_participants.record_message_participants(
        db_session, conversation=conversation, message=later
    )

    row = team_inbox_participants.list_participants(
        db_session, conversation_id=conversation.id
    )[0]
    assert row.admission_source == InboxParticipantAdmissionSource.inbound_cc.value


def test_a_removed_participant_records_when(db_session):
    from sqlalchemy.exc import IntegrityError

    conversation = _conversation(db_session)
    participant = InboxConversationParticipant(
        conversation_id=conversation.id,
        channel_type=InboxChannelType.email.value,
        normalized_endpoint="leaving@example.com",
        admission_source=InboxParticipantAdmissionSource.inbound_cc.value,
    )
    db_session.add(participant)
    db_session.flush()

    participant.is_active = False
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


# --- membership lookup -------------------------------------------------------


def test_membership_is_tested_on_the_exact_endpoint(db_session):
    conversation = _conversation(db_session)
    message = _message(db_session, conversation, from_address="Ada <ada@example.com>")
    team_inbox_participants.record_message_participants(
        db_session, conversation=conversation, message=message
    )

    assert team_inbox_participants.endpoint_is_participant(
        db_session,
        conversation_id=conversation.id,
        channel_type=InboxChannelType.email.value,
        endpoint="ADA@example.com",
    )
    assert not team_inbox_participants.endpoint_is_participant(
        db_session,
        conversation_id=conversation.id,
        channel_type=InboxChannelType.email.value,
        endpoint="someone-else@example.com",
    )


# --- ingestion wiring and backfill ------------------------------------------


def test_inbound_email_projects_its_participants(db_session):
    _route(db_session, "support@dotmac.io")
    db_session.commit()

    result = team_inbox_receive.receive_inbound_email(
        db_session,
        team_inbox_receive.InboundEmailPayload(
            from_address="Ada <ada@example.com>",
            to_addresses=["support@dotmac.io", "colleague@example.com"],
            cc_addresses=["vendor@partner.test"],
            body="Hello.",
            message_id="<p1@example.com>",
        ),
    )
    db_session.commit()

    endpoints = {
        row.normalized_endpoint
        for row in team_inbox_participants.list_participants(
            db_session, conversation_id=result.conversation_id
        )
    }
    assert endpoints == {
        "ada@example.com",
        "colleague@example.com",
        "vendor@partner.test",
    }


def test_backfill_projects_conversations_that_have_none(db_session):
    conversation = _conversation(db_session)
    _message(
        db_session,
        conversation,
        from_address="customer@example.com",
        cc=["colleague@example.com"],
    )
    db_session.commit()

    result = team_inbox_participants.backfill_conversations(db_session)
    db_session.commit()

    assert result["conversations"] == 1
    assert result["participants"] == 2


def test_backfill_is_idempotent(db_session):
    conversation = _conversation(db_session)
    _message(db_session, conversation, from_address="customer@example.com")
    db_session.commit()

    team_inbox_participants.backfill_conversations(db_session)
    db_session.commit()
    second = team_inbox_participants.backfill_conversations(db_session)

    assert second["participants"] == 0


def test_backfill_yields_only_from_when_headers_were_not_preserved(db_session):
    """Coverage is bounded by what the headers kept.

    An imported conversation whose messages carry no To/Cc yields only its
    From endpoints, so a parity figure must be read against that.
    """
    conversation = _conversation(db_session)
    _message(db_session, conversation, from_address="customer@example.com")
    db_session.commit()

    team_inbox_participants.backfill_conversations(db_session)
    db_session.commit()

    rows = team_inbox_participants.list_participants(
        db_session, conversation_id=conversation.id
    )
    assert [row.admission_source for row in rows] == [
        InboxParticipantAdmissionSource.inbound_from.value
    ]


# --- it stays shadow ---------------------------------------------------------


def test_nothing_reads_participants_for_a_threading_or_send_decision():
    """Pins the shadow position, so a reader lands as a deliberate cutover."""
    from pathlib import Path

    for module in (
        "app/services/team_inbox_receive.py",
        "app/services/team_inbox_channel_receive.py",
        "app/services/team_inbox_outbound.py",
    ):
        source = Path(module).read_text()
        assert "endpoint_is_participant" not in source, module
        assert "list_participants" not in source, module
