"""The last three demo controls: until-reply snooze, scheduled send, transcript.

See docs/designs/TEAM_INBOX_ADMIN_UI_PORT.md §5, slice 4.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Query

from app.models.team_inbox import (
    InboxConversation,
    InboxConversationStatus,
    InboxMessage,
)
from app.services import (
    team_inbox_commands,
    team_inbox_operations,
    team_inbox_outbound,
)

CONVERSATION = Path("templates/admin/inbox/_conversation.html").read_text()
JAVASCRIPT = Path("static/js/admin-inbox.js").read_text()


def _conversation_id(db_session):
    """Owner commands refuse a session already in a transaction, and touching an
    ORM object after commit re-opens one — so capture the id at flush."""
    conversation = InboxConversation(
        channel_type="email",
        subject="Line fault",
        contact_address="customer@example.com",
        status=InboxConversationStatus.open.value,
    )
    db_session.add(conversation)
    db_session.flush()
    captured = conversation.id
    db_session.commit()
    return captured


def _conversation(db_session, *, status=None):
    conversation = InboxConversation(
        channel_type="email",
        subject="Line fault",
        contact_address="customer@example.com",
        status=status or InboxConversationStatus.open.value,
    )
    db_session.add(conversation)
    db_session.flush()
    db_session.commit()
    return conversation


# --- until-next-reply snooze -------------------------------------------


def test_snoozing_until_reply_sets_no_wake_time(db_session):
    """A far-future date would invent a wake moment the operator never chose."""
    conversation = _conversation(db_session)

    team_inbox_operations.snooze_until_reply(db_session, conversation=conversation)

    assert conversation.status == "snoozed"
    assert conversation.snoozed_until is None
    assert (conversation.metadata_ or {})[
        team_inbox_operations.SNOOZE_UNTIL_REPLY_KEY
    ] is True


def test_an_inbound_message_wakes_it(db_session):
    conversation = _conversation(db_session)
    team_inbox_operations.snooze_until_reply(db_session, conversation=conversation)

    woke = team_inbox_operations.wake_on_inbound(db_session, conversation=conversation)

    assert woke is True
    assert conversation.status == "open"
    assert team_inbox_operations.SNOOZE_UNTIL_REPLY_KEY not in (
        conversation.metadata_ or {}
    )


def test_waking_records_why_it_woke(db_session):
    conversation = _conversation(db_session)
    team_inbox_operations.snooze_until_reply(db_session, conversation=conversation)

    team_inbox_operations.wake_on_inbound(db_session, conversation=conversation)

    history = (conversation.metadata_ or {}).get("workflow_history") or []
    assert history[-1]["woke_on_reply"] is True
    assert history[-1]["source"] == "team_inbox_inbound"


def test_a_time_snoozed_conversation_keeps_sleeping(db_session):
    """The operator picked that time knowing the customer might write again."""
    conversation_id = _conversation_id(db_session)
    team_inbox_commands.update_workflow(
        db_session,
        conversation_id=conversation_id,
        snooze_until=datetime.now(UTC) + timedelta(days=2),
    )
    conversation = db_session.get(InboxConversation, conversation_id)

    woke = team_inbox_operations.wake_on_inbound(db_session, conversation=conversation)

    assert woke is False
    assert conversation.status == "snoozed"
    assert conversation.snoozed_until is not None


def test_waking_is_a_no_op_for_an_ordinary_conversation(db_session):
    conversation = _conversation(db_session)

    assert (
        team_inbox_operations.wake_on_inbound(db_session, conversation=conversation)
        is False
    )
    assert conversation.status == "open"


def test_a_resolved_conversation_is_not_reopened(db_session):
    """An inbound message must not silently reopen closed work."""
    conversation = _conversation(
        db_session, status=InboxConversationStatus.resolved.value
    )
    metadata = dict(conversation.metadata_ or {})
    metadata[team_inbox_operations.SNOOZE_UNTIL_REPLY_KEY] = True
    conversation.metadata_ = metadata
    db_session.flush()

    team_inbox_operations.wake_on_inbound(db_session, conversation=conversation)

    assert conversation.status == "resolved"


def test_both_inbound_paths_wake_conversations():
    """Two independent receive paths; a flag cleared in only one is a bug."""
    for module in (
        "app/services/team_inbox_channel_receive.py",
        "app/services/team_inbox_receive.py",
    ):
        assert "wake_on_inbound" in Path(module).read_text(), module


# --- scheduled send -----------------------------------------------------


def test_scheduling_a_reply_does_not_send_it(db_session):
    conversation = _conversation(db_session)

    message = team_inbox_outbound.schedule_inbox_reply(
        db_session,
        conversation=conversation,
        payload=team_inbox_outbound.InboxReplyPayload(
            body_html="<p>later</p>", body_text="later"
        ),
        send_after=datetime.now(UTC) + timedelta(hours=2),
    )

    assert message.sent_at is None
    assert (message.metadata_ or {})["delivery_status"] == "scheduled"


def test_a_past_send_time_is_refused(db_session):
    conversation = _conversation(db_session)

    with pytest.raises(ValueError):
        team_inbox_outbound.schedule_inbox_reply(
            db_session,
            conversation=conversation,
            payload=team_inbox_outbound.InboxReplyPayload(
                body_html="<p>x</p>", body_text="x"
            ),
            send_after=datetime.now(UTC) - timedelta(minutes=1),
        )


def test_a_past_send_time_is_a_command_error(db_session):
    conversation_id = _conversation_id(db_session)

    with pytest.raises(
        team_inbox_commands.InboxCommandError,
        match="Choose a send time in the future",
    ):
        team_inbox_commands.reply(
            db_session,
            conversation_id=conversation_id,
            body_text="too late",
            actor_person_id=None,
            send_after=datetime.now(UTC) - timedelta(minutes=1),
        )


def test_only_due_replies_are_released(db_session):
    conversation = _conversation(db_session)
    for delta, _label in ((timedelta(hours=-2), "due"), (timedelta(days=2), "later")):
        message = InboxMessage(
            conversation_id=conversation.id,
            channel_type="email",
            direction="outbound",
            body="queued",
            sent_at=None,
            metadata_={
                "delivery_status": "scheduled",
                "scheduled_for": (datetime.now(UTC) + delta).isoformat(),
            },
        )
        db_session.add(message)
    db_session.flush()

    due = team_inbox_outbound.due_scheduled_replies(db_session)

    assert len(due) == 1


def test_due_replies_are_claimed_with_skip_locked(db_session, monkeypatch):
    conversation = _conversation(db_session)
    db_session.add(
        InboxMessage(
            conversation_id=conversation.id,
            channel_type="email",
            direction="outbound",
            body="queued",
            sent_at=None,
            metadata_={
                "delivery_status": "scheduled",
                "scheduled_for": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
            },
        )
    )
    db_session.flush()

    original = Query.with_for_update
    calls: list[dict[str, object]] = []

    def record_lock(query, **kwargs):
        calls.append(kwargs)
        return original(query, **kwargs)

    monkeypatch.setattr(Query, "with_for_update", record_lock)

    team_inbox_outbound.due_scheduled_replies(db_session)

    assert calls == [{"skip_locked": True}]


def test_scheduled_release_reuses_the_placeholder(db_session, monkeypatch):
    conversation = _conversation(db_session)
    message = InboxMessage(
        conversation_id=conversation.id,
        channel_type="email",
        direction="outbound",
        body="queued",
        sent_at=None,
        metadata_={
            "delivery_status": "scheduled",
            "scheduled_for": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
            "body_html": "<p>queued</p>",
            "body_text": "queued",
        },
    )
    db_session.add(message)
    db_session.flush()
    message_id = message.id

    def fake_send(*args, **kwargs):
        assert kwargs["existing_message"] is message
        message.sent_at = datetime.now(UTC)
        message.metadata_ = {"delivery_status": "queued"}
        return team_inbox_outbound.InboxReplyResult(
            kind="queued",
            conversation_id=str(conversation.id),
            message_id=str(message.id),
        )

    monkeypatch.setattr(team_inbox_outbound, "send_inbox_reply", fake_send)

    result = team_inbox_outbound.send_scheduled_reply(db_session, message=message)

    assert result.message_id == str(message_id)
    assert (
        db_session.query(InboxMessage)
        .filter(InboxMessage.conversation_id == conversation.id)
        .count()
        == 1
    )
    assert (message.metadata_ or {})["delivery_status"] == "queued"
    assert "scheduled_released_at" in (message.metadata_ or {})


def test_an_already_sent_reply_is_never_re_released(db_session):
    conversation = _conversation(db_session)
    message = InboxMessage(
        conversation_id=conversation.id,
        channel_type="email",
        direction="outbound",
        body="already gone",
        sent_at=datetime.now(UTC),
        metadata_={
            "delivery_status": "scheduled",
            "scheduled_for": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
        },
    )
    db_session.add(message)
    db_session.flush()

    assert team_inbox_outbound.due_scheduled_replies(db_session) == []


def test_the_composer_submits_a_send_time():
    assert 'name="send_after"' in CONVERSATION
    assert 'showDemoNotice?.("Scheduled send")' not in JAVASCRIPT
    # Toggling schedule off must clear the value, or an empty toggle still sends later.
    marker = JAVASCRIPT.index("toggleSchedule()")
    assert 'this.scheduledAt = ""' in JAVASCRIPT[marker : marker + 400]


def test_the_release_task_exists():
    tasks = Path("app/tasks/team_inbox.py").read_text()
    assert "release_scheduled_replies" in tasks
    maintenance = Path("app/services/team_inbox_maintenance.py").read_text()
    assert "class ReleaseScheduledRepliesCommand" in maintenance


# --- transcript ---------------------------------------------------------


def test_a_transcript_contains_the_exchanged_messages(db_session):
    conversation = _conversation(db_session)
    db_session.add_all(
        [
            InboxMessage(
                conversation_id=conversation.id,
                channel_type="email",
                direction="inbound",
                body="My line is down.",
            ),
            InboxMessage(
                conversation_id=conversation.id,
                channel_type="email",
                direction="outbound",
                body="An engineer is on the way.",
            ),
        ]
    )
    db_session.flush()

    subject, html = team_inbox_operations.render_conversation_transcript(
        db_session, conversation=conversation
    )

    assert "Line fault" in subject
    assert "My line is down." in html
    assert "An engineer is on the way." in html


def test_a_transcript_excludes_internal_notes(db_session):
    """Transcripts get forwarded; internal collaboration is not theirs to read."""
    conversation = _conversation(db_session)
    db_session.add_all(
        [
            InboxMessage(
                conversation_id=conversation.id,
                channel_type="email",
                direction="inbound",
                body="Customer says hello.",
            ),
            InboxMessage(
                conversation_id=conversation.id,
                channel_type="email",
                direction="internal",
                body="Careful, this customer is in arrears.",
            ),
        ]
    )
    db_session.flush()

    _subject, html = team_inbox_operations.render_conversation_transcript(
        db_session, conversation=conversation
    )

    assert "Customer says hello." in html
    assert "arrears" not in html


def test_a_transcript_excludes_a_reply_that_has_not_been_sent(db_session):
    conversation = _conversation(db_session)
    db_session.add(
        InboxMessage(
            conversation_id=conversation.id,
            channel_type="email",
            direction="outbound",
            body="Queued for tomorrow.",
            sent_at=None,
            metadata_={"delivery_status": "scheduled"},
        )
    )
    db_session.flush()

    _subject, html = team_inbox_operations.render_conversation_transcript(
        db_session, conversation=conversation
    )

    assert "Queued for tomorrow." not in html


def test_a_transcript_escapes_message_bodies(db_session):
    conversation = _conversation(db_session)
    db_session.add(
        InboxMessage(
            conversation_id=conversation.id,
            channel_type="email",
            direction="inbound",
            body="<script>alert(1)</script>",
        )
    )
    db_session.flush()

    _subject, html = team_inbox_operations.render_conversation_transcript(
        db_session, conversation=conversation
    )

    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_an_invalid_recipient_is_refused(db_session):
    conversation_id = _conversation_id(db_session)

    with pytest.raises(team_inbox_commands.InboxCommandError):
        team_inbox_commands.email_transcript(
            db_session, conversation_id=conversation_id, recipient="not-an-address"
        )


def test_the_transcript_control_is_a_real_form():
    assert "showDemoNotice('Email transcript')" not in CONVERSATION
    assert "/transcript" in CONVERSATION
    assert 'name="recipient"' in CONVERSATION
    assert "Internal notes and comments are not included." in CONVERSATION


def test_until_reply_snooze_still_applies_priority_and_mute(db_session):
    """Both arrived in one submit; honouring only the snooze loses half of it."""
    conversation_id = _conversation_id(db_session)

    team_inbox_commands.update_workflow(
        db_session,
        conversation_id=conversation_id,
        priority=10,
        is_muted=True,
        snooze_until_reply=True,
    )
    conversation = db_session.get(InboxConversation, conversation_id)

    assert conversation.priority == 10
    assert conversation.is_muted is True
    assert conversation.status == "snoozed"
    assert conversation.snoozed_until is None


def test_an_unsubmitted_until_reply_flag_does_not_snooze(db_session):
    """`Form(default=False)` is a truthy sentinel until FastAPI resolves it."""
    from app.web.admin import inbox as inbox_web

    assert inbox_web._form_flag(inbox_web.Form(default=False)) is False
    assert inbox_web._form_flag(True) is True
    assert inbox_web._form_flag("") is False
