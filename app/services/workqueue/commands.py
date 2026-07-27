"""Typed command owner for the native agent workqueue.

The workqueue owns ranking, scope, personal snooze state, and action
coordination. Ticket and conversation lifecycle decisions remain with their
canonical owners, which participate flush-only in this coordinator transaction.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hmac import compare_digest
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.idempotency import IdempotencyKey
from app.models.support import Ticket, TicketStatus
from app.models.team_inbox import InboxConversation, InboxConversationStatus
from app.models.work_order import WorkOrder
from app.models.workqueue import WorkqueueSnooze
from app.schemas.support import TicketUpdate
from app.services import team_inbox_commands
from app.services.audit_adapter import stage_audit_event
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.support import Tickets
from app.services.workqueue import snooze as snooze_service
from app.services.workqueue.aggregator import build_workqueue
from app.services.workqueue.permissions import (
    WorkqueuePermissionError,
    WorkqueuePrincipal,
)
from app.services.workqueue.types import ActionKind, ItemKind, WorkqueueItem

OWNER = "operations.agent_workqueue"
ACTION_CONCERN = "agent workqueue action coordination"
_ACTION_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern=ACTION_CONCERN,
    name="execute_agent_workqueue_action",
)
_IDEMPOTENCY_SCOPE = "agent-workqueue.action"


class SnoozeMode(StrEnum):
    minutes_30 = "30_minutes"
    hours_2 = "2_hours"
    day_1 = "1_day"
    next_reply = "next_reply"
    indefinite = "indefinite"
    explicit = "explicit"


class WorkqueueActionError(DomainError):
    """Stable transport-neutral action rejection."""


@dataclass(frozen=True)
class WorkqueueActionCommand:
    context: CommandContext
    principal: WorkqueuePrincipal
    item_kind: ItemKind
    item_id: UUID
    action: ActionKind
    requested_audience: str | None = None
    service_team_id: UUID | None = None
    snooze_mode: SnoozeMode | None = None
    explicit_snooze_until: datetime | None = None
    state_fingerprint: str | None = None
    confirmed: bool = False


@dataclass(frozen=True)
class WorkqueueSnoozeSnapshot:
    snooze_id: UUID
    system_user_id: UUID
    item_kind: ItemKind
    item_id: UUID
    snooze_until: datetime | None
    until_next_reply: bool
    created_at: datetime


@dataclass(frozen=True)
class WorkqueueActionOutcome:
    item_kind: ItemKind
    item_id: UUID
    action: ActionKind
    result: str
    replayed: bool
    service_team_id: UUID | None
    assigned_system_user_id: UUID | None
    previous_assigned_system_user_id: UUID | None
    snooze: WorkqueueSnoozeSnapshot | None = None


def _error(code: str, message: str, **details: object) -> WorkqueueActionError:
    return WorkqueueActionError(
        code=f"{OWNER}.{code}",
        message=message,
        details=details,
    )


def _actor(context: CommandContext) -> tuple[AuditActorType, str | None]:
    actor_type, separator, actor_id = context.actor.partition(":")
    if separator and actor_type == "api_key":
        return AuditActorType.api_key, actor_id or None
    if separator and actor_type == "user":
        return AuditActorType.user, actor_id or None
    return AuditActorType.system, context.actor or None


def _idempotency_reference(command: WorkqueueActionCommand) -> str:
    return f"{command.action.value}:{command.item_kind.value}:{command.item_id}"


def action_state_fingerprint(
    item: WorkqueueItem,
    action: ActionKind,
) -> str:
    """Bind a lifecycle-action review to the owner-projected current state."""

    if action not in {ActionKind.claim, ActionKind.complete}:
        raise ValueError("Only lifecycle workqueue actions have state fingerprints")
    canonical_state = json.dumps(
        {
            "schema_version": 1,
            "item_kind": item.item_kind.value,
            "item_id": str(item.item_id),
            "action": action.value,
            "status": item.status,
            "service_team_id": (
                str(item.service_team_id) if item.service_team_id else None
            ),
            "assigned_system_user_id": (
                str(item.assigned_person_id) if item.assigned_person_id else None
            ),
            "available_actions": sorted(value.value for value in item.actions),
            "can_act": item.can_act,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical_state.encode("utf-8")).hexdigest()


def _validate_action_review(
    command: WorkqueueActionCommand,
    item: WorkqueueItem,
) -> None:
    if command.action not in {ActionKind.claim, ActionKind.complete}:
        return
    supplied = (command.state_fingerprint or "").strip()
    if not supplied:
        raise _error(
            "action_review_required",
            "Review the current workqueue action before submitting it.",
        )
    expected = action_state_fingerprint(item, command.action)
    if not compare_digest(supplied, expected):
        raise _error(
            "stale_action_review",
            "The workqueue item changed after this action was reviewed. Reload it.",
        )
    if command.action is ActionKind.complete and not command.confirmed:
        raise _error(
            "confirmation_required",
            "Confirm the source lifecycle impact before completing this item.",
        )


def _replay(
    db: Session,
    command: WorkqueueActionCommand,
) -> IdempotencyKey | None:
    key = str(command.context.idempotency_key or "").strip()
    if not key:
        raise _error(
            "idempotency_key_required",
            "A stable idempotency key is required for a workqueue action.",
        )
    if len(key) > 120:
        raise _error(
            "invalid_idempotency_key",
            "The workqueue idempotency key is too long.",
        )
    existing = db.scalar(
        select(IdempotencyKey)
        .where(
            IdempotencyKey.scope == _IDEMPOTENCY_SCOPE,
            IdempotencyKey.key == key,
        )
        .with_for_update()
    )
    if existing is None:
        return None
    if (
        existing.account_id != command.principal.person_id
        or existing.ref_id != _idempotency_reference(command)
    ):
        raise _error(
            "idempotency_conflict",
            "This idempotency key belongs to another workqueue action.",
        )
    return existing


def _reserve(db: Session, command: WorkqueueActionCommand) -> IdempotencyKey:
    reservation = IdempotencyKey(
        scope=_IDEMPOTENCY_SCOPE,
        key=str(command.context.idempotency_key),
        account_id=command.principal.person_id,
        ref_id=_idempotency_reference(command),
    )
    db.add(reservation)
    db.flush()
    return reservation


def _lock_target(db: Session, command: WorkqueueActionCommand) -> None:
    target: object | None
    if command.item_kind is ItemKind.ticket:
        target = db.scalar(
            select(Ticket).where(Ticket.id == command.item_id).with_for_update()
        )
    elif command.item_kind is ItemKind.conversation:
        target = db.scalar(
            select(InboxConversation)
            .where(InboxConversation.id == command.item_id)
            .with_for_update()
        )
    else:
        target = db.scalar(
            select(WorkOrder).where(WorkOrder.id == command.item_id).with_for_update()
        )
    if target is None:
        raise _error(
            "item_not_found",
            "The workqueue item no longer exists.",
            item_kind=command.item_kind.value,
            item_id=str(command.item_id),
        )


def _visible_item(
    db: Session,
    command: WorkqueueActionCommand,
) -> WorkqueueItem:
    try:
        view = build_workqueue(
            db,
            command.principal,
            requested_audience=command.requested_audience,
            service_team_id=command.service_team_id,
            include_snoozed=True,
        )
    except WorkqueuePermissionError as exc:
        raise _error(
            "item_out_of_scope",
            "The requested workqueue scope is unavailable to this operator.",
        ) from exc
    for section in view.sections:
        if section.item_kind is not command.item_kind:
            continue
        for item in section.items:
            if item.item_id == command.item_id:
                return item
    raise _error(
        "item_out_of_scope",
        "The workqueue item is unavailable or outside your active team scope.",
        item_kind=command.item_kind.value,
        item_id=str(command.item_id),
    )


def _snooze_values(
    command: WorkqueueActionCommand,
    *,
    now: datetime,
) -> tuple[datetime | None, bool]:
    mode = command.snooze_mode
    if mode is SnoozeMode.minutes_30:
        return now + timedelta(minutes=30), False
    if mode is SnoozeMode.hours_2:
        return now + timedelta(hours=2), False
    if mode is SnoozeMode.day_1:
        return now + timedelta(days=1), False
    if mode is SnoozeMode.next_reply:
        if command.item_kind is not ItemKind.conversation:
            raise _error(
                "invalid_snooze_mode",
                "Only Inbox conversations can snooze until the next reply.",
            )
        return None, True
    if mode is SnoozeMode.indefinite:
        return None, False
    if mode is SnoozeMode.explicit and command.explicit_snooze_until is not None:
        return command.explicit_snooze_until, False
    raise _error(
        "invalid_snooze_mode",
        "Choose a supported workqueue snooze duration.",
    )


def _snapshot(snooze: WorkqueueSnooze | None) -> WorkqueueSnoozeSnapshot | None:
    if snooze is None:
        return None
    return WorkqueueSnoozeSnapshot(
        snooze_id=snooze.id,
        system_user_id=snooze.user_id,
        item_kind=ItemKind(snooze.item_kind),
        item_id=snooze.item_id,
        snooze_until=snooze.snooze_until,
        until_next_reply=snooze.until_next_reply,
        created_at=snooze.created_at,
    )


def _current_snooze(
    db: Session,
    command: WorkqueueActionCommand,
) -> WorkqueueSnooze | None:
    return db.scalar(
        select(WorkqueueSnooze).where(
            WorkqueueSnooze.user_id == command.principal.person_id,
            WorkqueueSnooze.item_kind == command.item_kind.value,
            WorkqueueSnooze.item_id == command.item_id,
        )
    )


def _apply_action(
    db: Session,
    command: WorkqueueActionCommand,
    item: WorkqueueItem,
) -> WorkqueueActionOutcome:
    previous_assignee = item.assigned_person_id
    if command.action is ActionKind.snooze:
        snooze_until, until_next_reply = _snooze_values(
            command,
            now=datetime.now(UTC),
        )
        snooze = snooze_service.snooze_item(
            db,
            user_id=command.principal.person_id,
            item_kind=command.item_kind,
            item_id=command.item_id,
            snooze_until=snooze_until,
            until_next_reply=until_next_reply,
        )
        return WorkqueueActionOutcome(
            item_kind=command.item_kind,
            item_id=command.item_id,
            action=command.action,
            result="snoozed",
            replayed=False,
            service_team_id=item.service_team_id,
            assigned_system_user_id=item.assigned_person_id,
            previous_assigned_system_user_id=previous_assignee,
            snooze=_snapshot(snooze),
        )
    if command.action is ActionKind.clear_snooze:
        cleared = snooze_service.clear_snooze(
            db,
            user_id=command.principal.person_id,
            item_kind=command.item_kind,
            item_id=command.item_id,
        )
        return WorkqueueActionOutcome(
            item_kind=command.item_kind,
            item_id=command.item_id,
            action=command.action,
            result="snooze_cleared" if cleared else "already_clear",
            replayed=False,
            service_team_id=item.service_team_id,
            assigned_system_user_id=item.assigned_person_id,
            previous_assigned_system_user_id=previous_assignee,
        )
    if not command.principal.can_act and not command.principal.is_admin:
        raise _error(
            "permission_denied",
            "You do not have permission to change this workqueue item.",
        )
    if command.action not in item.actions or not item.can_act:
        raise _error(
            "action_unavailable",
            "That action is not available for the current workqueue item state.",
            action=command.action.value,
        )

    if command.action is ActionKind.claim:
        if command.item_kind is ItemKind.ticket:
            ticket = Tickets.update(
                db,
                str(command.item_id),
                TicketUpdate(
                    assigned_to_person_id=command.principal.person_id,
                    assignee_person_ids=[command.principal.person_id],
                ),
                actor_id=str(command.principal.person_id),
            )
            assigned_id = ticket.assigned_to_person_id
        elif command.item_kind is ItemKind.conversation:
            if item.service_team_id is None:
                raise _error(
                    "team_required",
                    "The Inbox conversation has no active owning service team.",
                )
            assignment = team_inbox_commands.assign_conversation(
                db,
                conversation_id=command.item_id,
                service_team_id=item.service_team_id,
                person_id=command.principal.person_id,
                actor_person_id=command.principal.person_id,
                reason="Claimed from native agent workqueue",
            )
            if assignment.kind != "assigned":
                raise _error(
                    "claim_rejected",
                    assignment.reason or "The Inbox owner rejected the claim.",
                )
            assigned_id = command.principal.person_id
        else:
            raise _error(
                "action_unavailable",
                "Work orders must be claimed through the dispatch owner.",
            )
        return WorkqueueActionOutcome(
            item_kind=command.item_kind,
            item_id=command.item_id,
            action=command.action,
            result="claimed",
            replayed=False,
            service_team_id=item.service_team_id,
            assigned_system_user_id=assigned_id,
            previous_assigned_system_user_id=previous_assignee,
        )

    if command.action is ActionKind.complete:
        if command.item_kind is ItemKind.ticket:
            ticket = Tickets.update(
                db,
                str(command.item_id),
                TicketUpdate(status=TicketStatus.resolved.value),
                actor_id=str(command.principal.person_id),
            )
            assigned_id = ticket.assigned_to_person_id
        elif command.item_kind is ItemKind.conversation:
            outcome = team_inbox_commands.update_status(
                db,
                conversation_id=command.item_id,
                status_value=InboxConversationStatus.resolved.value,
                actor_person_id=command.principal.person_id,
            )
            assigned_id = item.assigned_person_id
            if outcome.status != InboxConversationStatus.resolved.value:
                raise _error(
                    "completion_rejected",
                    "The Inbox owner did not resolve the conversation.",
                )
        else:
            raise _error(
                "action_unavailable",
                "Work orders complete through the field work-order owner.",
            )
        return WorkqueueActionOutcome(
            item_kind=command.item_kind,
            item_id=command.item_id,
            action=command.action,
            result="completed",
            replayed=False,
            service_team_id=item.service_team_id,
            assigned_system_user_id=assigned_id,
            previous_assigned_system_user_id=previous_assignee,
        )
    raise _error(
        "action_unavailable",
        "Choose a supported workqueue action.",
    )


def _replayed_outcome(
    db: Session,
    command: WorkqueueActionCommand,
) -> WorkqueueActionOutcome:
    snooze = _current_snooze(db, command)
    result = {
        ActionKind.snooze: "snoozed",
        ActionKind.clear_snooze: "snooze_cleared",
        ActionKind.claim: "claimed",
        ActionKind.complete: "completed",
    }.get(command.action, "completed")
    return WorkqueueActionOutcome(
        item_kind=command.item_kind,
        item_id=command.item_id,
        action=command.action,
        result=result,
        replayed=True,
        service_team_id=None,
        assigned_system_user_id=(
            command.principal.person_id if command.action is ActionKind.claim else None
        ),
        previous_assigned_system_user_id=None,
        snooze=_snapshot(snooze),
    )


def _stage_evidence(
    db: Session,
    command: WorkqueueActionCommand,
    outcome: WorkqueueActionOutcome,
) -> None:
    actor_type, actor_id = _actor(command.context)
    evidence = {
        "schema_version": 1,
        "owner": OWNER,
        "command_id": str(command.context.command_id),
        "correlation_id": str(command.context.correlation_id),
        "item_kind": command.item_kind.value,
        "item_id": str(command.item_id),
        "action": command.action.value,
        "result": outcome.result,
        "service_team_id": (
            str(outcome.service_team_id) if outcome.service_team_id else None
        ),
        "assigned_system_user_id": (
            str(outcome.assigned_system_user_id)
            if outcome.assigned_system_user_id
            else None
        ),
    }
    stage_audit_event(
        db,
        action=f"workqueue.{command.action.value}",
        entity_type=f"workqueue_{command.item_kind.value}",
        entity_id=str(command.item_id),
        actor_type=actor_type,
        actor_id=actor_id,
        request_id=str(command.context.command_id),
        metadata=evidence,
    )
    emit_event(
        db,
        EventType.workqueue_action_coordinated,
        {
            **evidence,
            "aggregate_type": "agent_workqueue_action",
            "aggregate_id": str(command.item_id),
            "aggregate_version": str(command.context.command_id),
        },
        actor=command.context.actor,
    )


def _emit_realtime(outcome: WorkqueueActionOutcome, principal_id: UUID) -> None:
    from app.services.workqueue.events import emit_change, emit_item_change

    if outcome.action is ActionKind.snooze:
        emit_change(
            item_kind=outcome.item_kind,
            item_id=outcome.item_id,
            change="removed",
            affected_user_ids=[principal_id],
        )
        return
    if outcome.action is ActionKind.clear_snooze:
        emit_change(
            item_kind=outcome.item_kind,
            item_id=outcome.item_id,
            change="added",
            affected_user_ids=[principal_id],
        )
        return
    emit_item_change(
        item_kind=outcome.item_kind,
        item_id=outcome.item_id,
        change="removed" if outcome.action is ActionKind.complete else "updated",
        assignee_id=outcome.assigned_system_user_id,
        previous_assignee_id=outcome.previous_assigned_system_user_id,
        service_team_id=outcome.service_team_id,
    )


def execute_action(
    db: Session,
    command: WorkqueueActionCommand,
) -> WorkqueueActionOutcome:
    """Execute one scope-checked, replay-safe workqueue action."""

    db_session_adapter.release_read_transaction(db)

    def operation() -> WorkqueueActionOutcome:
        if _replay(db, command) is not None:
            return _replayed_outcome(db, command)
        _lock_target(db, command)
        item = _visible_item(db, command)
        if (
            command.action
            not in {
                ActionKind.snooze,
                ActionKind.clear_snooze,
            }
            and command.action not in item.actions
        ):
            raise _error(
                "action_unavailable",
                "That action is unavailable for the current item state.",
            )
        _validate_action_review(command, item)
        _reserve(db, command)
        outcome = _apply_action(db, command, item)
        _stage_evidence(db, command, outcome)
        return outcome

    outcome = execute_owner_command(
        db,
        definition=_ACTION_COMMAND,
        context=command.context,
        operation=operation,
    )
    if not outcome.replayed:
        _emit_realtime(outcome, command.principal.person_id)
    return outcome
