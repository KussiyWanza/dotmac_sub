"""Committed maintenance and repair commands for Team Inbox projections."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.models.team_inbox import (
    InboxConversation,
    InboxConversationAssignment,
    InboxConversationStatus,
)
from app.services import (
    team_inbox_media,
    team_inbox_operations,
    team_inbox_realtime,
    team_inbox_routing,
)
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

logger = logging.getLogger(__name__)

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


@dataclass(frozen=True, slots=True)
class RecoverStaleAiIntakeCommand:
    context: CommandContext
    now: datetime | None = None
    limit: int = 200


def _parse_instant(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def recover_stale_ai_intake(
    db: Session, command: RecoverStaleAiIntakeCommand
) -> MaintenanceOutcome:
    """Move expired intake waits to the normal fallback team path.

    This reconciler never creates messages, queue entries, or assignments. It
    only repairs destination-team state for unowned conversations.
    """

    def operation() -> MaintenanceOutcome:
        now = (command.now or datetime.now(UTC)).astimezone(UTC)
        conversations = (
            db.query(InboxConversation)
            .filter(InboxConversation.is_active.is_(True))
            .filter(InboxConversation.status != InboxConversationStatus.resolved.value)
            .filter(
                InboxConversation.metadata_["ai_intake"]["status"]
                .as_string()
                .in_(("classifying", "awaiting_follow_up"))
            )
            .order_by(InboxConversation.updated_at.asc())
            .limit(max(1, min(command.limit, 1000)))
            .with_for_update(skip_locked=True)
            .all()
        )
        changed = 0
        skipped = 0
        for conversation in conversations:
            metadata = dict(conversation.metadata_ or {})
            state_value = metadata.get("ai_intake")
            if not isinstance(state_value, dict):
                continue
            state = dict(state_value)
            if state.get("status") not in {"classifying", "awaiting_follow_up"}:
                continue
            due_at = _parse_instant(state.get("ai_intake_fallback_due_at"))
            if due_at is None:
                updated_at = _parse_instant(state.get("updated_at"))
                baseline = updated_at or conversation.updated_at or now
                if baseline.tzinfo is None:
                    baseline = baseline.replace(tzinfo=UTC)
                due_at = baseline.astimezone(UTC) + timedelta(minutes=5)
            if due_at > now:
                continue
            active_assignment = (
                db.query(InboxConversationAssignment.id)
                .filter(InboxConversationAssignment.conversation_id == conversation.id)
                .filter(InboxConversationAssignment.is_active.is_(True))
                .first()
            )
            if active_assignment is not None:
                state.update(
                    {
                        "status": "skipped",
                        "reason": "active_owner",
                        "updated_at": now.isoformat(),
                    }
                )
                metadata["ai_intake"] = state
                conversation.metadata_ = metadata
                skipped += 1
                continue
            decision = team_inbox_routing.resolve_channel_routing_decision(
                db,
                channel_type=conversation.channel_type,
                provider=str(state.get("provider") or "default"),
                account_scope=str(state.get("account_scope") or "default"),
                fallback_service_team_id=team_inbox_routing.default_service_team_id(db),
                metadata={**state, "ai_intake_status": "escalated"},
            )
            participants = [
                item
                for item in (
                    decision.primary_service_team_id,
                    decision.channel_service_team_id,
                )
                if item
            ]
            team_inbox_routing.apply_email_routing_plan(
                db,
                conversation=conversation,
                plan=team_inbox_routing.EmailTeamRoutingPlan(
                    primary_service_team_id=decision.primary_service_team_id,
                    participant_service_team_ids=list(dict.fromkeys(participants)),
                    matches=[],
                    unmatched_recipients=[],
                ),
            )
            state.update(
                {
                    "status": "escalated",
                    "reason": "fallback_timeout",
                    "destination_team_id": decision.primary_service_team_id,
                    "routing_reason": decision.reason,
                    "updated_at": now.isoformat(),
                }
            )
            metadata["ai_intake"] = state
            conversation.metadata_ = metadata
            logger.info(
                "stale AI intake routed to fallback",
                extra={
                    "event": "ai_intake_fallback_selected",
                    "conversation_id": str(conversation.id),
                    "destination_team_id": decision.primary_service_team_id,
                    "reason": "fallback_timeout",
                },
            )
            team_inbox_realtime.publish_queue_event(
                db, conversation_id=str(conversation.id), created=False
            )
            changed += 1
        return MaintenanceOutcome(changed=changed, skipped=skipped)

    return execute_owner_command(
        db,
        definition=_MAINTENANCE_COMMAND,
        context=command.context,
        operation=operation,
    )


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
