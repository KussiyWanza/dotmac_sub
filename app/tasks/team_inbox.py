"""Celery tasks for native team inbox operations."""

from __future__ import annotations

import logging

from app.celery_app import celery_app
from app.services import team_inbox_maintenance
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext

logger = logging.getLogger(__name__)


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
