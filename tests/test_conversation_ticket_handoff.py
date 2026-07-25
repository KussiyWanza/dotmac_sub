"""Behaviour of communications.conversation_ticket_handoff.

Owner commands require a transaction-free adapter session, so these helpers
commit and return plain identifiers — holding an ORM object across the call
would re-open a transaction on attribute access and fail at entry.

See docs/designs/TEAM_INBOX_ADMIN_UI_PORT.md §5, slice 3.
"""

from __future__ import annotations

import uuid

import pytest

from app.models.subscriber import Subscriber
from app.models.support import Ticket
from app.models.team_inbox import InboxConversation, InboxConversationStatus
from app.services import conversation_ticket_handoff as handoff


@pytest.fixture()
def customer_id(db_session) -> uuid.UUID:
    from app.services.subscriber import _default_reseller_id

    row = Subscriber(
        first_name="Inbox",
        last_name="Customer",
        email=f"handoff-{uuid.uuid4().hex}@example.com",
        reseller_id=_default_reseller_id(db_session),
    )
    db_session.add(row)
    # Capture before commit: expire_on_commit means reading `.id` afterwards
    # would re-open a transaction, and owner commands refuse a session that is
    # already in one.
    db_session.flush()
    captured = row.id
    db_session.commit()
    return captured


def _conversation_id(db_session, *, subscriber_id=None, status=None) -> uuid.UUID:
    conversation = InboxConversation(
        channel_type="whatsapp",
        subject="No internet since morning",
        contact_address="+2348000000000",
        status=status or InboxConversationStatus.open.value,
        subscriber_id=subscriber_id,
    )
    db_session.add(conversation)
    db_session.flush()
    captured = conversation.id
    db_session.commit()
    return captured


def _command(conversation_id, **overrides):
    base = dict(
        conversation_id=conversation_id,
        actor_id=uuid.uuid4(),
        actor_type=handoff.HandoffActorType.SYSTEM_USER,
        permission_keys=frozenset({"support:ticket:update"}),
        title="No internet since morning",
        description="Customer reports total outage.",
        reason="issued from inbox",
    )
    base.update(overrides)
    return handoff.ConversationTicketIssueCommand(**base)


def _ticket_count(db_session, conversation_id) -> int:
    return (
        db_session.query(Ticket)
        .filter(Ticket.origin_conversation_id == conversation_id)
        .count()
    )


def test_issue_records_provenance_and_inherits_customer(db_session, customer_id):
    conversation_id = _conversation_id(db_session, subscriber_id=customer_id)

    result = handoff.issue_ticket(db_session, _command(conversation_id))

    assert result.replayed is False
    assert result.ticket.origin_conversation_id == conversation_id
    assert result.ticket.subscriber_id == customer_id
    assert result.ticket.number


def test_reissuing_the_same_intent_replays(db_session, customer_id):
    """A double-submitted form must not open a second ticket."""
    conversation_id = _conversation_id(db_session, subscriber_id=customer_id)
    actor = uuid.uuid4()

    # No ORM access between commands: attributes expire on commit and reading
    # one would re-open a transaction, which the next command refuses. A real
    # caller issues one command per request, so this only bites in tests.
    handoff.issue_ticket(db_session, _command(conversation_id, actor_id=actor))
    second = handoff.issue_ticket(db_session, _command(conversation_id, actor_id=actor))

    assert second.replayed is True
    assert _ticket_count(db_session, conversation_id) == 1


def test_a_different_intent_opens_a_second_ticket(db_session, customer_id):
    """One conversation may legitimately issue many tickets."""
    conversation_id = _conversation_id(db_session, subscriber_id=customer_id)
    actor = uuid.uuid4()

    handoff.issue_ticket(db_session, _command(conversation_id, actor_id=actor))
    handoff.issue_ticket(
        db_session,
        _command(conversation_id, actor_id=actor, title="Separate billing dispute"),
    )

    assert _ticket_count(db_session, conversation_id) == 2


def test_issuance_does_not_transition_the_conversation(db_session, customer_id):
    """Opening a ticket and resolving a thread are separate decisions."""
    conversation_id = _conversation_id(db_session, subscriber_id=customer_id)

    handoff.issue_ticket(db_session, _command(conversation_id))

    conversation = db_session.get(InboxConversation, conversation_id)
    assert conversation.status == InboxConversationStatus.open.value


def test_missing_permission_is_refused(db_session, customer_id):
    conversation_id = _conversation_id(db_session, subscriber_id=customer_id)

    with pytest.raises(handoff.ConversationTicketHandoffError) as exc:
        handoff.issue_ticket(
            db_session, _command(conversation_id, permission_keys=frozenset())
        )
    assert exc.value.kind == "forbidden"


def test_resolved_conversation_cannot_issue(db_session, customer_id):
    conversation_id = _conversation_id(
        db_session,
        subscriber_id=customer_id,
        status=InboxConversationStatus.resolved.value,
    )

    with pytest.raises(handoff.ConversationTicketHandoffError) as exc:
        handoff.issue_ticket(db_session, _command(conversation_id))
    assert exc.value.kind == "conflict"


def test_blank_title_is_refused(db_session, customer_id):
    conversation_id = _conversation_id(db_session, subscriber_id=customer_id)

    with pytest.raises(handoff.ConversationTicketHandoffError) as exc:
        handoff.issue_ticket(db_session, _command(conversation_id, title="   "))
    assert exc.value.kind == "invalid"


def test_unknown_conversation_is_not_found(db_session):
    with pytest.raises(handoff.ConversationTicketHandoffError) as exc:
        handoff.issue_ticket(db_session, _command(uuid.uuid4()))
    assert exc.value.kind == "not_found"


def test_list_for_conversation_returns_issued_tickets(db_session, customer_id):
    conversation_id = _conversation_id(db_session, subscriber_id=customer_id)
    result = handoff.issue_ticket(db_session, _command(conversation_id))
    issued_id = result.ticket.id

    listed = handoff.list_for_conversation(db_session, conversation_id)

    assert [row.id for row in listed] == [issued_id]


def test_conversation_without_a_customer_still_issues(db_session):
    """An unlinked thread can still raise a ticket; identity is simply absent."""
    conversation_id = _conversation_id(db_session)

    result = handoff.issue_ticket(db_session, _command(conversation_id))

    assert result.ticket.origin_conversation_id == conversation_id
    assert result.ticket.subscriber_id is None
