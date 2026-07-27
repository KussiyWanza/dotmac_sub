"""Committed maintenance and repair commands for Team Inbox projections."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.services import team_inbox_media, team_inbox_operations
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

OWNER = "communications.team_inbox_maintenance"
_MAINTENANCE_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="scheduled Inbox projection maintenance and repair",
    name="execute_team_inbox_maintenance_command",
)


@dataclass(frozen=True, slots=True)
class RetryFailedOutboundCommand:
    context: CommandContext
    limit: int = 50
    max_retry_count: int = 5


@dataclass(frozen=True, slots=True)
class PromoteMediaAssetsCommand:
    context: CommandContext
    limit: int = 200


@dataclass(frozen=True, slots=True)
class AutoResolveStaleCommand:
    context: CommandContext
    stale_hours: int = 72
    limit: int = 200


@dataclass(frozen=True, slots=True)
class MaintenanceOutcome:
    changed: int
    skipped: int = 0


def retry_failed_outbound(
    db: Session, command: RetryFailedOutboundCommand
) -> MaintenanceOutcome:
    def operation() -> MaintenanceOutcome:
        result = team_inbox_operations.retry_failed_outbound_batch(
            db,
            limit=max(1, command.limit),
            max_retry_count=max(1, command.max_retry_count),
        )
        retried = result.get("retried")
        skipped = result.get("skipped")
        return MaintenanceOutcome(
            changed=len(retried) if isinstance(retried, list) else 0,
            skipped=len(skipped) if isinstance(skipped, list) else 0,
        )

    return execute_owner_command(
        db,
        definition=_MAINTENANCE_COMMAND,
        context=command.context,
        operation=operation,
    )


def promote_media_assets(
    db: Session, command: PromoteMediaAssetsCommand
) -> MaintenanceOutcome:
    return execute_owner_command(
        db,
        definition=_MAINTENANCE_COMMAND,
        context=command.context,
        operation=lambda: MaintenanceOutcome(
            changed=team_inbox_media.promote_unmaterialized_assets(
                db, limit=max(1, command.limit)
            )
        ),
    )


def auto_resolve_stale(
    db: Session, command: AutoResolveStaleCommand
) -> MaintenanceOutcome:
    return execute_owner_command(
        db,
        definition=_MAINTENANCE_COMMAND,
        context=command.context,
        operation=lambda: MaintenanceOutcome(
            changed=team_inbox_operations.auto_resolve_stale_conversations(
                db,
                stale_hours=max(1, command.stale_hours),
                limit=max(1, command.limit),
            )
        ),
    )


@dataclass(frozen=True, slots=True)
class BackfillParticipantsCommand:
    context: CommandContext
    limit: int = 200


def backfill_participants(
    db: Session, command: BackfillParticipantsCommand
) -> MaintenanceOutcome:
    """Project participants for conversations that have none yet.

    Walks oldest-first over unprojected conversations, so repeated runs move
    forward rather than re-treading the same head. Idempotent: it admits only
    missing endpoints, so a completed conversation is untouched and a partial
    one is finished.
    """
    from app.services import team_inbox_participants

    def operation() -> MaintenanceOutcome:
        result = team_inbox_participants.backfill_conversations(
            db, limit=max(1, command.limit)
        )
        return MaintenanceOutcome(
            changed=int(result.get("participants") or 0),
            skipped=int(result.get("conversations") or 0),
        )

    return execute_owner_command(
        db,
        definition=_MAINTENANCE_COMMAND,
        context=command.context,
        operation=operation,
    )


@dataclass(frozen=True, slots=True)
class WakeDueSnoozedCommand:
    context: CommandContext
    limit: int = 200


def wake_due_snoozed(db: Session, command: WakeDueSnoozedCommand) -> MaintenanceOutcome:
    """Return conversations whose chosen wake time has passed to the open queue."""

    return execute_owner_command(
        db,
        definition=_MAINTENANCE_COMMAND,
        context=command.context,
        operation=lambda: MaintenanceOutcome(
            changed=team_inbox_operations.wake_due_snoozed_conversations(
                db, limit=max(1, command.limit)
            )
        ),
    )


@dataclass(frozen=True, slots=True)
class ReleaseScheduledRepliesCommand:
    context: CommandContext
    limit: int = 50


def release_scheduled_replies(
    db: Session, command: ReleaseScheduledRepliesCommand
) -> MaintenanceOutcome:
    """Send scheduled replies whose time has come.

    Each send is independent: one failure marks that message failed and the
    batch continues, so a single bad recipient cannot hold up everyone else's
    scheduled mail.
    """

    def operation() -> MaintenanceOutcome:
        from app.services import team_inbox_outbound

        due = team_inbox_outbound.due_scheduled_replies(db, limit=max(1, command.limit))
        sent = 0
        skipped = 0
        for message in due:
            result = team_inbox_outbound.send_scheduled_reply(db, message=message)
            if result.kind in {"sent", "queued"}:
                sent += 1
            else:
                skipped += 1
        return MaintenanceOutcome(changed=sent, skipped=skipped)

    return execute_owner_command(
        db,
        definition=_MAINTENANCE_COMMAND,
        context=command.context,
        operation=operation,
    )
