"""Starting a conversation the operator initiates.

`team_inbox_outbound` could only reply to an existing thread, so "New
conversation" was a demo adapter. This opens the thread and sends its first
message in one command.

See docs/designs/TEAM_INBOX_ADMIN_UI_PORT.md §5, slice 4.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app.models.subscriber import Subscriber
from app.models.team_inbox import InboxConversation, InboxMessage
from app.services import team_inbox_commands

OVERLAYS = Path("templates/admin/inbox/_overlays.html").read_text()
JAVASCRIPT = Path("static/js/admin-inbox.js").read_text()


@pytest.fixture()
def customer(db_session):
    from app.services.subscriber import _default_reseller_id

    row = Subscriber(
        first_name="Known",
        last_name="Customer",
        email=f"known-{uuid.uuid4().hex}@example.com",
        reseller_id=_default_reseller_id(db_session),
    )
    db_session.add(row)
    db_session.flush()
    captured = (row.id, row.email)
    db_session.commit()
    return captured


def test_starting_a_conversation_opens_it_and_sends_the_first_message(db_session):
    outcome = team_inbox_commands.start_conversation(
        db_session,
        channel_type="email",
        contact_address="stranger@example.com",
        subject="Scheduled maintenance",
        body_text="We will be working on your line tomorrow.",
    )

    conversation = db_session.get(InboxConversation, outcome.conversation_id)
    assert conversation.status == "open"
    assert conversation.subject == "Scheduled maintenance"
    assert (conversation.metadata_ or {}).get("source") == "operator_initiated"

    messages = (
        db_session.query(InboxMessage)
        .filter(InboxMessage.conversation_id == conversation.id)
        .all()
    )
    assert [m.direction for m in messages] == ["outbound"]


def test_a_known_address_resolves_to_its_customer(db_session, customer):
    """A thread the operator starts must resolve like an inbound one would."""
    subscriber_id, email = customer

    outcome = team_inbox_commands.start_conversation(
        db_session,
        channel_type="email",
        contact_address=email,
        body_text="Following up on your report.",
    )

    conversation = db_session.get(InboxConversation, outcome.conversation_id)
    assert conversation.subscriber_id == subscriber_id


def test_an_unknown_address_still_opens_a_thread(db_session):
    """The operator may be reaching someone the system does not know yet."""
    outcome = team_inbox_commands.start_conversation(
        db_session,
        channel_type="email",
        contact_address="nobody@example.com",
        body_text="Hello.",
    )

    conversation = db_session.get(InboxConversation, outcome.conversation_id)
    assert conversation.subscriber_id is None
    # Recorded, so the drawer can offer a contact link instead of showing an
    # anonymous thread as though it were resolved.
    assert (conversation.metadata_ or {}).get("contact_resolution")
    assert outcome.contact_status


def test_a_missing_body_is_refused(db_session):
    with pytest.raises(team_inbox_commands.InboxCommandError):
        team_inbox_commands.start_conversation(
            db_session,
            channel_type="email",
            contact_address="someone@example.com",
            body_text="   ",
        )


def test_a_missing_contact_is_refused(db_session):
    with pytest.raises(team_inbox_commands.InboxCommandError):
        team_inbox_commands.start_conversation(
            db_session,
            channel_type="email",
            contact_address="  ",
            body_text="Hello.",
        )


def test_an_unknown_channel_is_refused(db_session):
    with pytest.raises(team_inbox_commands.InboxCommandError) as exc:
        team_inbox_commands.start_conversation(
            db_session,
            channel_type="carrier-pigeon",
            contact_address="someone@example.com",
            body_text="Hello.",
        )
    assert "channel" in str(exc.value).lower()


def test_no_conversation_survives_a_failed_first_send(db_session, monkeypatch):
    """A thread whose opening message never left is worse than none — the queue
    would show a conversation the customer never received."""
    from app.services import team_inbox_outbound

    def _fail(*args, **kwargs):
        return team_inbox_outbound.InboxReplyResult(
            kind="failed", conversation_id="x", reason="no sender configured"
        )

    monkeypatch.setattr(team_inbox_outbound, "send_inbox_reply", _fail)

    with pytest.raises(team_inbox_commands.InboxCommandRejected):
        team_inbox_commands.start_conversation(
            db_session,
            channel_type="email",
            contact_address="someone@example.com",
            body_text="Hello.",
        )

    assert db_session.query(InboxConversation).count() == 0


# --- surface ------------------------------------------------------------


def test_the_overlay_posts_a_real_form():
    assert "submitDemoConversation" not in OVERLAYS
    assert "submitDemoConversation" not in JAVASCRIPT
    assert 'action="/admin/inbox/conversations"' in OVERLAYS
    assert "Demo state" not in OVERLAYS
    assert "components/forms/csrf_input.html" in OVERLAYS


def test_the_form_states_how_an_unknown_contact_behaves():
    assert "still opens a thread you can link later" in OVERLAYS
