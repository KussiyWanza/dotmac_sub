"""Celery tasks for native team inbox operations."""

from __future__ import annotations

import logging

from app.celery_app import celery_app
from app.models.domain_settings import SettingDomain
from app.services import (
    team_inbox_assignment,
    team_inbox_maintenance,
    team_inbox_reply_reminders,
)
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext
from app.services.settings_spec import resolve_integer

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.team_inbox.send_reply_reminders")
def send_reply_reminders(*, limit: int = 200) -> dict[str, int]:
    with db_session_adapter.session() as session:
        # Resolve decision inputs before entering the owner command. A settings
        # miss reads the database, which opens a read transaction on this
        # session, and the owner command requires a transaction-free session at
        # entry. Release that read transaction before handing the session over.
        delay_minutes = resolve_integer(
            session, SettingDomain.comms, "inbox_reply_reminder_delay_minutes"
        )
        repeat_minutes = resolve_integer(
            session, SettingDomain.comms, "inbox_reply_reminder_repeat_minutes"
        )
        db_session_adapter.release_read_transaction(session)
        result = team_inbox_reply_reminders.sweep_reply_reminders(
            session,
            team_inbox_reply_reminders.ReplyReminderSweepCommand(
                context=CommandContext.system(
                    actor="task:team-inbox-reply-reminders",
                    scope="team-inbox:reply-reminders",
                    reason="notify assigned agents about waiting inbound replies",
                ),
                delay_minutes=delay_minutes,
                repeat_minutes=repeat_minutes,
                limit=limit,
            ),
        )
        return {
            "scheduled": result.scheduled,
            "sent": result.sent,
            "resolved": result.resolved,
        }


@celery_app.task(name="app.tasks.team_inbox.promote_queued_conversations")
def promote_queued_conversations(*, limit: int = 200) -> dict[str, int]:
    with db_session_adapter.session() as session:
        result = team_inbox_assignment.sweep_queued_conversations(
            session,
            team_inbox_assignment.InboxQueueSweepCommand(
                context=CommandContext.system(
                    actor="task:team-inbox-queue-promotion",
                    scope="team-inbox:routing-command",
                    reason="promote oldest eligible FIFO queue entries",
                ),
                limit=limit,
            ),
        )
        payload = {
            "promoted": result.promoted,
            "cancelled": result.cancelled,
            "remaining": result.remaining,
        }
        logger.info(
            "team inbox FIFO queue promotion complete",
            extra={"event": "team_inbox_queue_promotion", **payload},
        )
        return payload


@celery_app.task(name="app.tasks.team_inbox.recover_stale_ai_intake")
def recover_stale_ai_intake(*, limit: int = 200) -> dict[str, int]:
    with db_session_adapter.session() as session:
        result = team_inbox_maintenance.recover_stale_ai_intake(
            session,
            team_inbox_maintenance.RecoverStaleAiIntakeCommand(
                context=CommandContext.system(
                    actor="task:team-inbox-ai-intake-recovery",
                    scope="team-inbox:maintenance",
                    reason="route expired AI intake waits through fallback policy",
                ),
                limit=limit,
            ),
        )
        payload = {"recovered": result.changed, "skipped": result.skipped}
        logger.info(
            "team inbox AI intake recovery complete",
            extra={"event": "team_inbox_ai_intake_recovery", **payload},
        )
        return payload


@celery_app.task(name="app.tasks.team_inbox.retry_failed_outbound_messages")
def retry_failed_outbound_messages(
    *,
    limit: int = 50,
    max_retry_count: int = 5,
) -> dict[str, int]:
    with db_session_adapter.session() as session:
        result = team_inbox_maintenance.retry_failed_outbound(
            session,
            team_inbox_maintenance.RetryFailedOutboundCommand(
                context=CommandContext.system(
                    actor="task:team-inbox-retry",
                    scope="team-inbox:maintenance",
                    reason="retry failed outbound Inbox intents",
                ),
                limit=limit,
                max_retry_count=max_retry_count,
            ),
        )
        payload = {
            "retried": result.changed,
            "skipped": result.skipped,
        }
        logger.info(
            "team inbox failed outbound retry complete",
            extra={"event": "team_inbox_failed_outbound_retry", **payload},
        )
        return payload


@celery_app.task(name="app.tasks.team_inbox.promote_message_media_assets")
def promote_message_media_assets(*, limit: int = 200) -> dict[str, int]:
    with db_session_adapter.session() as session:
        result = team_inbox_maintenance.promote_media_assets(
            session,
            team_inbox_maintenance.PromoteMediaAssetsCommand(
                context=CommandContext.system(
                    actor="task:team-inbox-media",
                    scope="team-inbox:maintenance",
                    reason="repair Inbox media projections",
                ),
                limit=limit,
            ),
        )
        payload = {"promoted": result.changed}
        logger.info(
            "team inbox media asset promotion complete",
            extra={"event": "team_inbox_media_asset_promotion", **payload},
        )
        return payload


@celery_app.task(name="app.tasks.team_inbox.auto_resolve_stale_conversations")
def auto_resolve_stale_conversations(
    *,
    stale_hours: int = 72,
    limit: int = 200,
) -> dict[str, int]:
    with db_session_adapter.session() as session:
        result = team_inbox_maintenance.auto_resolve_stale(
            session,
            team_inbox_maintenance.AutoResolveStaleCommand(
                context=CommandContext.system(
                    actor="task:team-inbox-auto-resolve",
                    scope="team-inbox:maintenance",
                    reason="apply configured stale-conversation maintenance",
                ),
                stale_hours=stale_hours,
                limit=limit,
            ),
        )
        payload = {"resolved": result.changed}
        logger.info(
            "team inbox stale conversation auto-resolve complete",
            extra={"event": "team_inbox_auto_resolve", **payload},
        )
        return payload


@celery_app.task(name="app.tasks.team_inbox.release_scheduled_replies")
def release_scheduled_replies(*, limit: int = 50) -> dict[str, int]:
    """Send inbox replies whose scheduled time has passed."""
    with db_session_adapter.session() as session:
        result = team_inbox_maintenance.release_scheduled_replies(
            session,
            team_inbox_maintenance.ReleaseScheduledRepliesCommand(
                context=CommandContext.system(
                    actor="task:team-inbox-scheduled-send",
                    scope="team-inbox:maintenance",
                    reason="release due scheduled Inbox replies",
                ),
                limit=limit,
            ),
        )
        payload = {"sent": result.changed, "skipped": result.skipped}
        logger.info(
            "team inbox scheduled reply release complete",
            extra={"event": "team_inbox_scheduled_release", **payload},
        )
        return payload


@celery_app.task(name="app.tasks.team_inbox.wake_due_snoozed_conversations")
def wake_due_snoozed_conversations(*, limit: int = 200) -> dict[str, int]:
    """Return conversations whose snooze has expired to the open queue."""
    with db_session_adapter.session() as session:
        result = team_inbox_maintenance.wake_due_snoozed(
            session,
            team_inbox_maintenance.WakeDueSnoozedCommand(
                context=CommandContext.system(
                    actor="task:team-inbox-snooze-waker",
                    scope="team-inbox:maintenance",
                    reason="settle conversations whose snooze expired",
                ),
                limit=limit,
            ),
        )
        payload = {"woken": result.changed}
        logger.info(
            "team inbox snooze wake complete",
            extra={"event": "team_inbox_snooze_wake", **payload},
        )
        return payload


@celery_app.task(name="app.tasks.team_inbox.backfill_conversation_participants")
def backfill_conversation_participants(*, limit: int = 200) -> dict[str, int]:
    """Project participants for conversations that do not have them yet."""
    with db_session_adapter.session() as session:
        result = team_inbox_maintenance.backfill_participants(
            session,
            team_inbox_maintenance.BackfillParticipantsCommand(
                context=CommandContext.system(
                    actor="task:team-inbox-participant-backfill",
                    scope="team-inbox:maintenance",
                    reason="project conversation participants from stored headers",
                ),
                limit=limit,
            ),
        )
        payload = {"participants": result.changed, "conversations": result.skipped}
        logger.info(
            "team inbox participant backfill complete",
            extra={"event": "team_inbox_participant_backfill", **payload},
        )
        return payload
