"""Admin inbox queue list_query contract."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.models.team_inbox import InboxConversation, InboxConversationStatus
from app.services import team_inbox_projection, team_inbox_read


def _conv(
    db,
    *,
    priority,
    last_message_at,
    thread,
    status=InboxConversationStatus.open.value,
):
    conversation = InboxConversation(
        priority=priority,
        last_message_at=last_message_at,
        external_thread_id=thread,
        status=status,
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


def test_all_status_includes_every_status_while_active_excludes_resolved(db_session):
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    open_conversation = _conv(
        db_session, priority=1, last_message_at=now, thread="open"
    )
    pending_conversation = _conv(
        db_session,
        priority=1,
        last_message_at=now - timedelta(minutes=1),
        thread="pending",
        status=InboxConversationStatus.pending.value,
    )
    resolved_conversation = _conv(
        db_session,
        priority=1,
        last_message_at=now - timedelta(minutes=2),
        thread="resolved",
        status=InboxConversationStatus.resolved.value,
    )
    db_session.commit()

    all_queue = team_inbox_projection.build_queue_projection(
        db_session, team_inbox_projection.InboxQueueRequest()
    )
    active_queue = team_inbox_projection.build_queue_projection(
        db_session,
        team_inbox_projection.InboxQueueRequest(open_only=True),
    )
    done_queue = team_inbox_projection.build_queue_projection(
        db_session,
        team_inbox_projection.InboxQueueRequest(
            status=InboxConversationStatus.resolved.value
        ),
    )
    open_queue = team_inbox_projection.build_queue_projection(
        db_session,
        team_inbox_projection.InboxQueueRequest(
            status=InboxConversationStatus.open.value
        ),
    )
    pending_queue = team_inbox_projection.build_queue_projection(
        db_session,
        team_inbox_projection.InboxQueueRequest(
            status=InboxConversationStatus.pending.value
        ),
    )

    assert {row.id for row in all_queue.rows} == {
        str(open_conversation.id),
        str(pending_conversation.id),
        str(resolved_conversation.id),
    }
    assert {row.id for row in active_queue.rows} == {
        str(open_conversation.id),
        str(pending_conversation.id),
    }
    assert [row.id for row in done_queue.rows] == [str(resolved_conversation.id)]
    assert [row.id for row in open_queue.rows] == [str(open_conversation.id)]
    assert [row.id for row in pending_queue.rows] == [str(pending_conversation.id)]


def test_explicit_open_only_still_excludes_resolved_history(db_session):
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    active = _conv(db_session, priority=1, last_message_at=now, thread="active")
    _conv(
        db_session,
        priority=1,
        last_message_at=now - timedelta(minutes=1),
        thread="resolved",
        status=InboxConversationStatus.resolved.value,
    )
    db_session.commit()

    result = team_inbox_projection.build_queue_projection(
        db_session, team_inbox_projection.InboxQueueRequest(open_only=True)
    )

    assert [row.id for row in result.rows] == [str(active.id)]


def test_resolved_realtime_row_remains_under_all_and_leaves_active(db_session):
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    resolved = _conv(
        db_session,
        priority=1,
        last_message_at=now,
        thread="resolved-realtime",
        status=InboxConversationStatus.resolved.value,
    )
    db_session.commit()

    all_row = team_inbox_projection.get_queue_row_projection(
        db_session,
        conversation_id=resolved.id,
        request=team_inbox_projection.InboxQueueRequest(),
    )
    active_row = team_inbox_projection.get_queue_row_projection(
        db_session,
        conversation_id=resolved.id,
        request=team_inbox_projection.InboxQueueRequest(open_only=True),
    )

    assert all_row.row is not None
    assert all_row.row.id == str(resolved.id)
    assert active_row.row is None


def test_list_conversations_can_skip_exact_total_count(db_session):
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    newest = _conv(db_session, priority=1, last_message_at=now, thread="newest")
    middle = _conv(
        db_session,
        priority=1,
        last_message_at=now - timedelta(minutes=1),
        thread="middle",
    )
    _conv(
        db_session,
        priority=1,
        last_message_at=now - timedelta(minutes=2),
        thread="oldest",
    )
    db_session.commit()

    result = team_inbox_read.list_conversations(
        db_session,
        order_by="last_message_at",
        order_dir="desc",
        limit=2,
        include_total_count=False,
    )

    assert [row.id for row in result.items] == [str(newest.id), str(middle.id)]
    assert result.count == 3


def test_list_conversations_counts_when_bounded_page_is_out_of_range(db_session):
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    _conv(db_session, priority=1, last_message_at=now, thread="only")
    db_session.commit()

    result = team_inbox_read.list_conversations(
        db_session,
        limit=25,
        offset=250,
        include_total_count=False,
    )

    assert result.items == []
    assert result.count == 1
