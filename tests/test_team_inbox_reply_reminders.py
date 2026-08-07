from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.models.service_team import ServiceTeam
from app.models.system_user import SystemUser
from app.models.team_inbox import (
    InboxConversation,
    InboxConversationAssignment,
    InboxMessage,
    InboxReplyReminder,
)
from app.services.owner_commands import CommandContext
from app.services.team_inbox_reply_reminders import (
    ReplyReminderSweepCommand,
    sweep_reply_reminders,
)


def test_reply_reminder_schedules_then_repeats(db_session, monkeypatch):
    now = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
    user = SystemUser(
        first_name="Reply",
        last_name="Agent",
        email=f"reply-{uuid4()}@example.test",
    )
    conversation = InboxConversation(channel_type="email", status="open")
    team = ServiceTeam(name=f"Reminder {uuid4()}", team_type="support")
    db_session.add_all([user, conversation, team])
    db_session.flush()
    assignment = InboxConversationAssignment(
        conversation_id=conversation.id,
        service_team_id=team.id,
        person_id=user.id,
        assigned_at=now - timedelta(minutes=20),
        is_active=True,
    )
    inbound = InboxMessage(
        conversation_id=conversation.id,
        channel_type="email",
        direction="inbound",
        body="Please help",
        received_at=now - timedelta(minutes=20),
    )
    db_session.add_all([assignment, inbound])
    db_session.commit()
    queued = []
    monkeypatch.setattr(
        "app.services.team_inbox_reply_reminders.Notifications.queue_internal_notification",
        lambda _db, payload: queued.append(payload) or object(),
    )

    first = sweep_reply_reminders(
        db_session,
        ReplyReminderSweepCommand(
            context=CommandContext.system(actor="test", scope="test", reason="test"),
            delay_minutes=15,
            repeat_minutes=10,
            now=now,
        ),
    )
    assert (first.scheduled, first.sent) == (1, 1)
    reminder = db_session.query(InboxReplyReminder).one()
    assert reminder.sent_count == 1
    assert len(queued) == 1
    db_session.commit()

    early = sweep_reply_reminders(
        db_session,
        ReplyReminderSweepCommand(
            context=CommandContext.system(actor="test", scope="test", reason="test"),
            delay_minutes=15,
            repeat_minutes=10,
            now=now + timedelta(minutes=9),
        ),
    )
    assert early.sent == 0

    repeat = sweep_reply_reminders(
        db_session,
        ReplyReminderSweepCommand(
            context=CommandContext.system(actor="test", scope="test", reason="test"),
            delay_minutes=15,
            repeat_minutes=10,
            now=now + timedelta(minutes=10),
        ),
    )
    assert repeat.sent == 1
    assert db_session.query(InboxReplyReminder).one().sent_count == 2
    assert len(queued) == 2


def test_agent_reply_resolves_active_reminder(db_session, monkeypatch):
    now = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
    user = SystemUser(first_name="Agent", last_name="One", email=f"a-{uuid4()}@x.test")
    conversation = InboxConversation(channel_type="email", status="open")
    team = ServiceTeam(name=f"Reminder {uuid4()}", team_type="support")
    db_session.add_all([user, conversation, team])
    db_session.flush()
    assignment = InboxConversationAssignment(
        conversation_id=conversation.id,
        service_team_id=team.id,
        person_id=user.id,
        assigned_at=now - timedelta(minutes=30),
    )
    db_session.add(assignment)
    db_session.flush()
    db_session.add_all(
        [
            InboxMessage(
                conversation_id=conversation.id,
                channel_type="email",
                direction="inbound",
                body="Help",
                received_at=now - timedelta(minutes=25),
            ),
            InboxReplyReminder(
                assignment_id=assignment.id,
                conversation_id=conversation.id,
                person_id=user.id,
                waiting_since=now - timedelta(minutes=25),
                next_due_at=now - timedelta(minutes=10),
                sent_count=1,
            ),
            InboxMessage(
                conversation_id=conversation.id,
                channel_type="email",
                direction="outbound",
                body="On it",
                sent_at=now - timedelta(minutes=5),
            ),
        ]
    )
    db_session.commit()
    result = sweep_reply_reminders(
        db_session,
        ReplyReminderSweepCommand(
            context=CommandContext.system(actor="test", scope="test", reason="test"),
            delay_minutes=15,
            repeat_minutes=10,
            now=now,
        ),
    )
    assert result.resolved == 1
    assert db_session.query(InboxReplyReminder).one().is_active is False
