"""Snoozing to an explicit moment rather than a fixed duration.

The menu offered one hour / tomorrow / next week, and "Custom date/time" was a
demo notice. An operator who knows when the customer is available needs the
moment, not an interval.

See docs/designs/TEAM_INBOX_ADMIN_UI_PORT.md §5, slice 4.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.models.team_inbox import InboxConversation, InboxConversationStatus
from app.services import team_inbox_commands, team_inbox_operations
from app.web.admin.inbox import _parse_datetime_field

CONVERSATION = Path("templates/admin/inbox/_conversation.html").read_text()


def _aware(value):
    """SQLite drops the timezone on read; Postgres preserves it."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _conversation_id(db_session):
    conversation = InboxConversation(
        channel_type="email",
        subject="Callback requested",
        contact_address="customer@example.com",
        status=InboxConversationStatus.open.value,
    )
    db_session.add(conversation)
    db_session.flush()
    captured = conversation.id
    db_session.commit()
    return captured


def test_snoozing_to_an_explicit_time_sets_that_time(db_session):
    conversation_id = _conversation_id(db_session)
    wake = datetime.now(UTC) + timedelta(days=3)

    team_inbox_commands.update_workflow(
        db_session, conversation_id=conversation_id, snooze_until=wake
    )

    row = db_session.get(InboxConversation, conversation_id)
    assert row.status == "snoozed"
    assert abs((_aware(row.snoozed_until) - wake).total_seconds()) < 2


def test_an_explicit_time_wins_over_a_duration(db_session):
    """The operator picked a moment; the interval must not override it."""
    conversation_id = _conversation_id(db_session)
    wake = datetime.now(UTC) + timedelta(days=5)

    team_inbox_commands.update_workflow(
        db_session,
        conversation_id=conversation_id,
        snooze_minutes=60,
        snooze_until=wake,
    )

    row = db_session.get(InboxConversation, conversation_id)
    assert (_aware(row.snoozed_until) - datetime.now(UTC)) > timedelta(days=4)


def test_a_past_time_is_refused_rather_than_waking_immediately(db_session):
    conversation_id = _conversation_id(db_session)
    past = datetime.now(UTC) - timedelta(hours=1)

    with pytest.raises(team_inbox_operations.InboxOperationError) as exc:
        team_inbox_commands.update_workflow(
            db_session, conversation_id=conversation_id, snooze_until=past
        )
    assert "future" in str(exc.value)


def test_fixed_durations_still_work(db_session):
    conversation_id = _conversation_id(db_session)

    team_inbox_commands.update_workflow(
        db_session, conversation_id=conversation_id, snooze_minutes=60
    )

    row = db_session.get(InboxConversation, conversation_id)
    assert row.status == "snoozed"
    assert _aware(row.snoozed_until) > datetime.now(UTC)


def test_zero_minutes_still_clears_a_snooze(db_session):
    conversation_id = _conversation_id(db_session)
    team_inbox_commands.update_workflow(
        db_session, conversation_id=conversation_id, snooze_minutes=60
    )

    team_inbox_commands.update_workflow(
        db_session, conversation_id=conversation_id, snooze_minutes=0
    )

    assert db_session.get(InboxConversation, conversation_id).snoozed_until is None


# --- the adapter's parsing ---------------------------------------------


def test_naive_browser_input_is_read_as_utc():
    parsed = _parse_datetime_field("2026-08-01T09:30")
    assert parsed is not None
    assert parsed.tzinfo is UTC
    assert parsed.hour == 9


def test_unparsable_input_is_treated_as_absent():
    """Better to ignore it than snooze to a moment nobody chose."""
    assert _parse_datetime_field("not-a-date") is None
    assert _parse_datetime_field("") is None
    assert _parse_datetime_field(None) is None


def test_the_menu_posts_a_real_form():
    assert "showDemoNotice('Custom snooze date')" not in CONVERSATION
    assert 'name="snooze_until"' in CONVERSATION
