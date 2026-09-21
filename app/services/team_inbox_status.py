"""Canonical Team Inbox conversation-status transition owner."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.models.team_inbox import (
    InboxAuditEvidenceGrade,
    InboxAuditSource,
    InboxConversation,
    InboxConversationStatus,
    InboxStatusTransitionEvent,
)
from app.services import team_inbox_completion_override, team_inbox_customer_completion
from app.services.owner_commands import execute_owner_savepoint, owner_command_active

OWNER = "communications.team_inbox_status"
logger = logging.getLogger(__name__)


class InboxStatusReason(StrEnum):
    operator_change = "operator_change"
    bulk_change = "bulk_change"
    macro = "macro"
    auto_resolve = "auto_resolve"
    snooze = "snooze"
    snooze_expired = "snooze_expired"
    campaign_reopen = "campaign_reopen"
    widget_reopen = "widget_reopen"
    field_job_open = "field_job_open"
    field_job_complete = "field_job_complete"
    field_job_queue = "field_job_queue"
    ai_intake_started = "ai_intake_started"
    ai_awaiting_clarification = "ai_awaiting_clarification"
    ai_handoff_accepted = "ai_handoff_accepted"
    ai_fallback_escalation = "ai_fallback_escalation"
    ai_human_takeover = "ai_human_takeover"
    ai_intake_expired = "ai_intake_expired"
    ai_intake_resolved = "ai_intake_resolved"
    ai_intake_failed = "ai_intake_failed"
    historical_reconstruction = "historical_reconstruction"


@dataclass(frozen=True, slots=True)
class InboxStatusTransitionCommand:
    conversation_id: UUID
    status: InboxConversationStatus
    actor_person_id: UUID | None
    reason: InboxStatusReason
    source_id: str
    occurred_at: datetime
    compatibility_source: str
    macro_id: UUID | None = None
    completion_override_grant_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class InboxStatusTransitionOutcome:
    conversation_id: UUID
    previous_status: InboxConversationStatus
    status: InboxConversationStatus
    event_id: UUID | None
    already_set: bool


class InboxStatusTransitionError(RuntimeError):
    pass


_AGENT_RESOLUTION_REASONS = frozenset(
    {
        InboxStatusReason.operator_change,
        InboxStatusReason.bulk_change,
        InboxStatusReason.macro,
    }
)


def _apply_status_transition(
    db: Session,
    *,
    conversation: InboxConversation,
    command: InboxStatusTransitionCommand,
) -> InboxStatusTransitionOutcome:
    """Flush-only participant used inside an owning command transaction."""

    if conversation.id != command.conversation_id:
        raise InboxStatusTransitionError("Command conversation does not match target")
    previous = InboxConversationStatus(conversation.status)
    if previous is command.status:
        return InboxStatusTransitionOutcome(
            conversation_id=conversation.id,
            previous_status=previous,
            status=command.status,
            event_id=None,
            already_set=True,
        )
    effective_at = command.occurred_at
    gate_applies = (
        command.status is InboxConversationStatus.resolved
        and command.reason in _AGENT_RESOLUTION_REASONS
        and not (
            conversation.customer_completion_policy_version_id is None
            and db.get_bind().dialect.name == "sqlite"
        )
    )
    readiness = None
    if gate_applies:
        readiness = team_inbox_customer_completion.resolution_readiness(
            db, conversation
        )
        if (
            not readiness.can_agent_resolve
            and command.completion_override_grant_id is None
        ):
            # No override was offered: raise BEFORE any event row exists, so
            # a blocked resolution never commits a phantom `resolved`
            # transition event for a conversation whose status never
            # actually changed. This is the exact pre-existing behavior and
            # the exact rich `resolution_blocked` contract every caller
            # (bulk's graceful skip, macro's per-action failure capture,
            # direct adapters) already depends on.
            team_inbox_customer_completion.require_agent_resolution_ready(
                db, conversation
            )

    event = InboxStatusTransitionEvent(
        conversation_id=conversation.id,
        previous_status=previous.value,
        status=command.status.value,
        actor_person_id=command.actor_person_id,
        reason_code=command.reason.value,
        source=InboxAuditSource.status_command,
        source_id=command.source_id,
        evidence_grade=InboxAuditEvidenceGrade.native,
        occurred_at=effective_at,
    )

    if gate_applies and readiness is not None and not readiness.can_agent_resolve:
        # A grant was offered -- the no-grant case above already raised and
        # never reaches here. Consuming it needs `event.id` for the grant's
        # FK, so the event is created here, but ONLY inside a savepoint
        # together with the consumption itself: a caller (bulk/macro) that
        # catches a failed consumption and continues to the next
        # conversation must never commit a phantom `resolved` event for
        # this one.
        def _create_event_and_consume_grant() -> None:
            db.add(event)
            db.flush()
            team_inbox_completion_override.consume_override_for_resolution(
                db,
                conversation=conversation,
                readiness=readiness,
                actor_person_id=command.actor_person_id,
                resolution_reason=command.reason.value,
                override_grant_id=command.completion_override_grant_id,
                transition_event_id=event.id,
                occurred_at=effective_at,
            )

        if not owner_command_active(db):
            # Every production entry point (direct, bulk, macro) reaches
            # here only through `_commit`'s `execute_owner_command`, so a
            # grant id supplied outside an active owner command is a
            # programming error, not a degraded-but-acceptable path (unlike
            # the CSAT-request best-effort step below): there would be no
            # transaction boundary to savepoint the event-creation and
            # consumption against, and silently running them un-isolated is
            # exactly the phantom-event risk this savepoint exists to
            # close. Fail closed instead.
            raise InboxStatusTransitionError(
                "A completion-override grant can only be consumed inside an "
                "active owner command."
            )
        execute_owner_savepoint(db, _create_event_and_consume_grant)
    else:
        db.add(event)

    metadata = dict(conversation.metadata_ or {})
    history = metadata.get("status_history")
    if not isinstance(history, list):
        history = []
    compatibility_entry: dict[str, str | None] = {
        "from": previous.value,
        "to": command.status.value,
        "at": effective_at.isoformat(),
        "actor_id": (str(command.actor_person_id) if command.actor_person_id else None),
        "source": command.compatibility_source,
    }
    if command.macro_id is not None:
        compatibility_entry["macro_id"] = str(command.macro_id)
    history.append(compatibility_entry)
    metadata["status_history"] = history[-50:]
    conversation.metadata_ = metadata
    conversation.status = command.status.value
    db.flush()
    if command.status is InboxConversationStatus.resolved:
        from app.services import team_inbox_assignment

        team_inbox_assignment.cancel_queued_conversation(
            db,
            conversation=conversation,
            now=effective_at,
            reason="conversation_resolved",
        )
        team_inbox_assignment.schedule_queue_promotion_after_commit(
            db,
            reason="conversation_resolved_opened_capacity",
            service_team_id=conversation.primary_service_team_id,
        )

        def create_csat_request():
            from app.services import support_csat

            return support_csat.ensure_inbox_request(
                db,
                conversation,
                transition_event_id=event.id,
                resolution_at=effective_at,
                actor_person_id=command.actor_person_id,
            )

        try:
            if owner_command_active(db):
                execute_owner_savepoint(db, create_csat_request)
            else:
                logger.warning(
                    "inbox_csat_request_skipped_no_owner_command "
                    "conversation_id=%s event_id=%s",
                    conversation.id,
                    event.id,
                )
        except Exception as exc:  # noqa: BLE001 - status transition must persist
            logger.warning(
                "inbox_csat_request_failed conversation_id=%s event_id=%s error=%s",
                conversation.id,
                event.id,
                exc,
            )
    return InboxStatusTransitionOutcome(
        conversation_id=conversation.id,
        previous_status=previous,
        status=command.status,
        event_id=event.id,
        already_set=False,
    )


def apply_status_transition(
    db: Session,
    *,
    conversation: InboxConversation,
    status: InboxConversationStatus,
    actor_person_id: UUID | None,
    reason: InboxStatusReason,
    source_id: str | None = None,
    occurred_at: datetime | None = None,
    compatibility_source: str | None = None,
    macro_id: UUID | None = None,
    completion_override_grant_id: UUID | None = None,
) -> InboxStatusTransitionOutcome:
    """Normalize callers into the one typed, flush-only command contract."""

    return _apply_status_transition(
        db,
        conversation=conversation,
        command=InboxStatusTransitionCommand(
            conversation_id=conversation.id,
            status=status,
            actor_person_id=actor_person_id,
            reason=reason,
            source_id=source_id or f"status:{uuid4()}",
            occurred_at=occurred_at or datetime.now(UTC),
            compatibility_source=compatibility_source or reason.value,
            macro_id=macro_id,
            completion_override_grant_id=completion_override_grant_id,
        ),
    )
