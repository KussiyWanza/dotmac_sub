"""Customer 360 Communications: a customer-scoped read of the inbox.

`communications.team_inbox` stays the owner. The customer record projects its
conversations read-only; acting on one still means opening the workspace.

See docs/designs/TEAM_INBOX_ADMIN_UI_PORT.md §5, slice 3.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app.models.subscriber import Subscriber
from app.models.team_inbox import InboxConversation, InboxConversationStatus
from app.services import team_inbox_read


def _subscriber(db_session) -> uuid.UUID:
    """inbox_conversations.subscriber_id is a real FK — bare UUIDs will not do."""
    from app.services.subscriber import _default_reseller_id

    row = Subscriber(
        first_name="Inbox",
        last_name="Customer",
        email=f"inbox-{uuid.uuid4().hex}@example.com",
        reseller_id=_default_reseller_id(db_session),
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row.id


@pytest.fixture()
def subscriber_a(db_session):
    return _subscriber(db_session)


@pytest.fixture()
def subscriber_b(db_session):
    return _subscriber(db_session)


def _conversation(db_session, *, subscriber_id, subject: str, channel="whatsapp"):
    conversation = InboxConversation(
        channel_type=channel,
        subject=subject,
        contact_address="+2348000000000",
        status=InboxConversationStatus.open.value,
        subscriber_id=subscriber_id,
    )
    db_session.add(conversation)
    db_session.flush()
    db_session.commit()
    return conversation.id


def test_list_conversations_scopes_to_subscriber(
    db_session, subscriber_a, subscriber_b
):
    kept = _conversation(db_session, subscriber_id=subscriber_a, subject="Mine")
    _conversation(db_session, subscriber_id=subscriber_b, subject="Theirs")

    result = team_inbox_read.list_conversations(db_session, subscriber_id=subscriber_a)

    assert [row.id for row in result.items] == [str(kept)]
    assert result.count == 1


def test_unscoped_read_is_unchanged(db_session, subscriber_a, subscriber_b):
    """The filter is additive — omitting it must not narrow the queue."""
    _conversation(db_session, subscriber_id=subscriber_a, subject="A")
    _conversation(db_session, subscriber_id=subscriber_b, subject="B")

    assert team_inbox_read.list_conversations(db_session).count == 2


def test_subscriber_filter_ignores_unlinked_conversations(db_session, subscriber_a):
    """An unresolved conversation has no subscriber and must not leak."""
    _conversation(db_session, subscriber_id=None, subject="Unresolved")
    _conversation(db_session, subscriber_id=subscriber_a, subject="Mine")

    result = team_inbox_read.list_conversations(db_session, subscriber_id=subscriber_a)
    assert result.count == 1
    assert result.items[0].subject == "Mine"


def test_customer_detail_renders_communications_behind_permission():
    detail = Path("templates/admin/customers/detail.html").read_text()
    assert "can_view_conversations" in detail
    assert "customer_conversations" in detail
    assert "Communications" in detail

    # Cross-domain assembly belongs in the web_* context builder, not the route,
    # matching how linked work orders are assembled for ticket detail.
    routes = Path("app/web/admin/customers.py").read_text()
    assert "team_inbox_read" not in routes, "route must stay a thin adapter"
    assert "include_conversations=show_conversations" in routes

    builder = Path("app/services/web_customer_details.py").read_text()
    assert "team_inbox_read.list_conversations" in builder
    assert "subscriber_id=customer.id" in builder


def test_ticket_detail_shows_customer_conversation_history():
    """An agent replying on a ticket should see what the customer already said
    on other channels."""
    detail = Path("templates/admin/support/tickets/detail.html").read_text()
    assert "ticket_conversations" in detail
    assert "Customer conversations" in detail

    routes = Path("app/web/admin/support_tickets.py").read_text()
    assert "team_inbox_read" not in routes, "route must stay a thin adapter"

    builder = Path("app/services/web_support_tickets.py").read_text()
    assert "team_inbox_read.list_conversations" in builder
    # Scoped by subscriber until a conversation->ticket link exists.
    assert "subscriber_id=ticket.subscriber_id" in builder
