"""Admin inbox queue list_query contract."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.models.team_inbox import InboxConversation
from app.services import team_inbox_projection, team_inbox_read


def _conv(db, *, priority, last_message_at, thread):
    conversation = InboxConversation(
        priority=priority,
        last_message_at=last_message_at,
        external_thread_id=thread,
    )
    db.add(conversation)
    db.flush()
    return conversation


def test_inbox_definition_declares_its_capabilities():
    definition = team_inbox_projection.INBOX_LIST_DEFINITION
    assert set(definition.sortable_keys) == {
        "priority",
        "last_message_at",
        "created_at",
    }
    assert definition.default_sort == "last_message_at"
    assert definition.default_sort_dir == "desc"
    for key in (
        "status",
        "needs_response",
        "muted",
        "snoozed",
        "service_team_id",
        "priority_at_most",
    ):
        assert key in definition.filterable_keys


def test_projection_default_order_is_newest_activity_first(db_session):
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    a = _conv(db_session, priority=1, last_message_at=now, thread="a")
    b = _conv(
        db_session, priority=0, last_message_at=now - timedelta(days=5), thread="b"
    )
    c = _conv(
        db_session, priority=1, last_message_at=now - timedelta(days=1), thread="c"
    )
    db_session.commit()

    result = team_inbox_projection.build_queue_projection(
        db_session,
        team_inbox_projection.InboxQueueRequest(),
    )
    assert [row.id for row in result.rows] == [str(a.id), str(c.id), str(b.id)]


def test_order_by_last_message_at_ignores_priority(db_session):
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    a = _conv(db_session, priority=5, last_message_at=now, thread="a")
    b = _conv(
        db_session, priority=1, last_message_at=now - timedelta(days=2), thread="b"
    )
    db_session.commit()

    result = team_inbox_read.list_conversations(
        db_session, order_by="last_message_at", order_dir="desc"
    )
    # Sorted by recency only — the more-recent a (despite lower urgency) leads.
    assert [row.id for row in result.items] == [str(a.id), str(b.id)]
