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


@dataclass(frozen=True, slots=True)
class InboxStatusTransitionOutcome:
    conversation_id: UUID
    previous_status: InboxConversationStatus
    status: InboxConversationStatus
    event_id: UUID | None
    already_set: bool


class InboxStatusTransitionError(RuntimeError):
    pass


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
        ),
    )
