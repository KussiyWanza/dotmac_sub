"""Reply reminder scheduling through the canonical notification outbox."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.notification import NotificationChannel
from app.models.system_user import SystemUser
from app.models.team_inbox import (
    InboxConversationAssignment,
    InboxMessage,
    InboxMessageDirection,
    InboxReplyReminder,
)
from app.schemas.notification import NotificationCreate
from app.services.notification import Notifications
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

OWNER = "communications.team_inbox_reply_reminders"
_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="agent reply reminder scheduling and repeat delivery",
    name="sweep_team_inbox_reply_reminders",
)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@dataclass(frozen=True)
class ReplyReminderSweepCommand:
    context: CommandContext
    delay_minutes: int
    repeat_minutes: int
    limit: int = 200
    now: datetime | None = None


@dataclass(frozen=True)
class ReplyReminderSweepResult:
    scheduled: int
    sent: int
    resolved: int


def sweep_reply_reminders(
    db: Session, command: ReplyReminderSweepCommand
) -> ReplyReminderSweepResult:
    if min(command.delay_minutes, command.repeat_minutes, command.limit) < 1:
        raise ValueError("delay_minutes, repeat_minutes and limit must be positive")
    now = command.now or datetime.now(UTC)

    def _operation() -> ReplyReminderSweepResult:
        scheduled = sent = resolved = 0
        assignments = (
            db.query(InboxConversationAssignment)
            .filter(InboxConversationAssignment.is_active.is_(True))
            .order_by(InboxConversationAssignment.assigned_at.asc())
            .limit(command.limit)
            .with_for_update(skip_locked=True)
            .all()
        )
        for assignment in assignments:
            latest_inbound = (
                db.query(
                    func.max(
                        func.coalesce(InboxMessage.received_at, InboxMessage.created_at)
                    )
                )
                .filter(InboxMessage.conversation_id == assignment.conversation_id)
                .filter(InboxMessage.direction == InboxMessageDirection.inbound.value)
                .scalar()
            )
            if latest_inbound is None:
                continue
            latest_outbound = (
                db.query(
                    func.max(
                        func.coalesce(InboxMessage.sent_at, InboxMessage.created_at)
                    )
                )
                .filter(InboxMessage.conversation_id == assignment.conversation_id)
                .filter(InboxMessage.direction == InboxMessageDirection.outbound.value)
                .scalar()
            )
            waiting_since = max(_aware(assignment.assigned_at), _aware(latest_inbound))
            reminder = (
                db.query(InboxReplyReminder)
                .filter(InboxReplyReminder.assignment_id == assignment.id)
                .with_for_update()
                .one_or_none()
            )
            if latest_outbound is not None and _aware(latest_outbound) >= waiting_since:
                if reminder is not None and reminder.is_active:
                    reminder.is_active = False
                    reminder.resolved_at = now
                    resolved += 1
                continue
            if reminder is None:
                reminder = InboxReplyReminder(
                    assignment_id=assignment.id,
                    conversation_id=assignment.conversation_id,
                    person_id=assignment.person_id,
                    waiting_since=waiting_since,
                    next_due_at=waiting_since
                    + timedelta(minutes=command.delay_minutes),
                )
                db.add(reminder)
                db.flush()
                scheduled += 1
            elif _aware(reminder.waiting_since) != waiting_since:
                reminder.waiting_since = waiting_since
                reminder.next_due_at = waiting_since + timedelta(
                    minutes=command.delay_minutes
                )
                reminder.last_sent_at = None
                reminder.sent_count = 0
                reminder.is_active = True
                reminder.resolved_at = None
                scheduled += 1
            if reminder.is_active and _aware(reminder.next_due_at) <= now:
                user = db.get(SystemUser, assignment.person_id)
                if user is None or not user.is_active:
                    continue
                Notifications.queue_internal_notification(
                    db,
                    NotificationCreate(
                        channel=NotificationChannel.email,
                        audience_type="system_user",
                        audience_id=user.id,
                        recipient=user.email,
                        event_type="team_inbox.reply_reminder",
                        category="support",
                        subject="Customer reply waiting in Team Inbox",
                        body="An assigned Team Inbox conversation is waiting for your reply.",
                        metadata_={
                            "conversation_id": str(assignment.conversation_id),
                            "assignment_id": str(assignment.id),
                            "reminder_number": reminder.sent_count + 1,
                        },
                    ),
                )
                reminder.sent_count += 1
                reminder.last_sent_at = now
                reminder.next_due_at = now + timedelta(minutes=command.repeat_minutes)
                sent += 1
        return ReplyReminderSweepResult(
            scheduled=scheduled, sent=sent, resolved=resolved
        )

    return execute_owner_command(
        db, definition=_COMMAND, context=command.context, operation=_operation
    )
