"""The three queue filters that were demo-only: AI handling, sent-to-ticket,
and an activity window.

Each had its data already: `ai_handling` lives in conversation metadata and is
counted by the projection, and the ticket link landed with
`communications.conversation_ticket_handoff`. Only the filter was missing.

See docs/designs/TEAM_INBOX_ADMIN_UI_PORT.md §5, slice 4.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.models.subscriber import Subscriber
from app.models.team_inbox import InboxConversation, InboxConversationStatus
from app.services import conversation_ticket_handoff, team_inbox_read

SIDEBAR = Path("templates/admin/inbox/_sidebar.html").read_text()


def _conversation(db_session, *, subject, ai=False, last_message_at=None):
    conversation = InboxConversation(
        channel_type="email",
        subject=subject,
        contact_address="customer@example.com",
        status=InboxConversationStatus.open.value,
        last_message_at=last_message_at,
        metadata_={"ai_handling": True} if ai else None,
    )
    db_session.add(conversation)
    db_session.flush()
    captured = conversation.id
    db_session.commit()
    return captured


# --- AI handling --------------------------------------------------------


def test_ai_handling_selects_only_ai_threads(db_session):
    _conversation(db_session, subject="AI", ai=True)
    _conversation(db_session, subject="Human")

    result = team_inbox_read.list_conversations(db_session, ai_handling=True)

    assert [row.subject for row in result.items] == ["AI"]


def test_ai_handling_false_excludes_them(db_session):
    """A human triaging wants the threads no agent is already on."""
    _conversation(db_session, subject="AI", ai=True)
    _conversation(db_session, subject="Human")

    result = team_inbox_read.list_conversations(db_session, ai_handling=False)

    assert [row.subject for row in result.items] == ["Human"]


def test_omitting_ai_handling_returns_both(db_session):
    _conversation(db_session, subject="AI", ai=True)
    _conversation(db_session, subject="Human")

    assert team_inbox_read.list_conversations(db_session).count == 2


# --- sent to ticket -----------------------------------------------------


@pytest.fixture()
def customer_id(db_session):
    from app.services.subscriber import _default_reseller_id

    row = Subscriber(
        first_name="Filter",
        last_name="Customer",
        email=f"filter-{uuid.uuid4().hex}@example.com",
        reseller_id=_default_reseller_id(db_session),
    )
    db_session.add(row)
    db_session.flush()
    captured = row.id
    db_session.commit()
    return captured


def _issue_ticket(db_session, conversation_id):
    return conversation_ticket_handoff.issue_ticket(
        db_session,
        conversation_ticket_handoff.ConversationTicketIssueCommand(
            conversation_id=conversation_id,
            actor_id=uuid.uuid4(),
            actor_type=conversation_ticket_handoff.HandoffActorType.SYSTEM_USER,
            permission_keys=frozenset({"support:ticket:update"}),
            title="Escalated",
        ),
    )


def test_has_ticket_selects_threads_that_issued_one(db_session, customer_id):
    escalated = _conversation(db_session, subject="Escalated")
    _conversation(db_session, subject="Untouched")
    _issue_ticket(db_session, escalated)

    result = team_inbox_read.list_conversations(db_session, has_ticket=True)

    assert [row.subject for row in result.items] == ["Escalated"]


def test_has_ticket_false_is_the_backlog_that_still_needs_a_decision(db_session):
    escalated = _conversation(db_session, subject="Escalated")
    _conversation(db_session, subject="Untouched")
    _issue_ticket(db_session, escalated)

    result = team_inbox_read.list_conversations(db_session, has_ticket=False)

    assert [row.subject for row in result.items] == ["Untouched"]


# --- activity window ----------------------------------------------------


def test_activity_window_filters_on_last_activity(db_session):
    """The range means 'was this thread live then', not when it was created."""
    now = datetime.now(UTC)
    _conversation(db_session, subject="Old", last_message_at=now - timedelta(days=30))
    _conversation(
        db_session, subject="Recent", last_message_at=now - timedelta(hours=2)
    )

    result = team_inbox_read.list_conversations(
        db_session, activity_from=now - timedelta(days=1)
    )

    assert [row.subject for row in result.items] == ["Recent"]


def test_activity_window_is_bounded_at_both_ends(db_session):
    now = datetime.now(UTC)
    _conversation(db_session, subject="Old", last_message_at=now - timedelta(days=30))
    _conversation(db_session, subject="Mid", last_message_at=now - timedelta(days=10))
    _conversation(
        db_session, subject="Recent", last_message_at=now - timedelta(hours=2)
    )

    result = team_inbox_read.list_conversations(
        db_session,
        activity_from=now - timedelta(days=20),
        activity_to=now - timedelta(days=5),
    )

    assert [row.subject for row in result.items] == ["Mid"]


def test_every_new_filter_is_additive(db_session):
    """Omitting them must not narrow the default queue."""
    _conversation(db_session, subject="A", ai=True)
    _conversation(db_session, subject="B")

    assert team_inbox_read.list_conversations(db_session).count == 2


# --- surface ------------------------------------------------------------


def test_the_sidebar_exposes_them_and_they_survive_pagination():
    for field in ("ai_handling", "has_ticket", "activity_from", "activity_to"):
        assert f'name="{field}"' in SIDEBAR

    projection = Path("app/services/team_inbox_projection.py").read_text()
    for field in ("ai_handling", "has_ticket", "activity_from", "activity_to"):
        # Declared on the list definition so a sort or page click round-trips it.
        assert f'ListFieldDefinition("{field}"' in projection
