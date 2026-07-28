"""Deliver committed support-chain outputs to their receipted consumers.

The handler is a thin delivery adapter for the Team Inbox → Ticket →
WorkOrder chain (docs/designs/TICKET_WORK_ORDER_HANDOFF_SOT.md): a committed
field outcome reaches the handoff owner's receipted consumer, and fired
durable-timer triggers reach their owning consumers (resolution-confirmation
grace, conversation snooze wake). Each effect commits atomically with its
unique ``(consumer, event_id)`` receipt, so a redelivery is an exact no-op.
Field completion never changes ticket status — support verifies and
resolves separately.

A consequence that cannot be applied raises so the event delivery stays
failed and retryable instead of a warning log.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.services.common import coerce_uuid
from app.services.events.handlers.owner_session import owner_session as _owner_session
from app.services.events.owner_outputs import require_output_text
from app.services.events.types import Event, EventType

HANDLED_EVENT_TYPES = frozenset(
    {
        EventType.work_order_field_outcome_recorded,
        EventType.custom,
    }
)

# Durable-timer triggers this adapter routes (fire_due_timers emits them as
# EventType.custom with the trigger in the payload).
_RESOLUTION_GRACE_TRIGGER = "support.resolution_confirmation_due"
_SNOOZE_WAKE_TRIGGER = "team_inbox.snooze_wake"
_SLA_BREACH_TRIGGER = "support.ticket_sla_breach_due"


class SupportLifecycleProjectionHandler:
    """Route support-chain outputs and timer triggers to receipted owners."""

    def handle(self, db: Session, event: Event) -> None:
        if event.event_type == EventType.work_order_field_outcome_recorded:
            self._apply_field_outcome(db, event)
            return
        if event.event_type == EventType.custom:
            trigger = event.payload.get("trigger")
            if trigger == _RESOLUTION_GRACE_TRIGGER:
                self._auto_confirm_resolution(db, event)
            elif trigger == _SNOOZE_WAKE_TRIGGER:
                self._wake_snoozed_conversation(db, event)
            elif trigger == _SLA_BREACH_TRIGGER:
                self._evaluate_sla_breach(db, event)
            # Any other custom payload belongs to other adapters.

    @staticmethod
    def _context(event: Event, scope: str):
        from app.services.owner_commands import CommandContext

        return CommandContext.system(
            actor=str(event.actor or "support.lifecycle_projection"),
            scope=scope,
            reason=str(event.payload.get("trigger") or event.event_type.value),
            command_id=event.event_id,
            correlation_id=event.event_id,
            causation_id=event.event_id,
            idempotency_key=f"event:{event.event_id}",
        )

    def _apply_field_outcome(self, db: Session, event: Event) -> None:
        # Work orders without an origin ticket have no ticket projection.
        if not event.payload.get("origin_ticket_id"):
            return
        work_order_id = require_output_text(
            event.payload,
            "work_order_id",
            consumer="support.ticket_work_order_handoff",
            event_id=event.event_id,
            event_type=event.event_type.value,
        )
        field_event_id = require_output_text(
            event.payload,
            "field_event_id",
            consumer="support.ticket_work_order_handoff",
            event_id=event.event_id,
            event_type=event.event_type.value,
        )
        occurred_at_text = require_output_text(
            event.payload,
            "occurred_at",
            consumer="support.ticket_work_order_handoff",
            event_id=event.event_id,
            event_type=event.event_type.value,
        )
        outcome = require_output_text(
            event.payload,
            "outcome",
            consumer="support.ticket_work_order_handoff",
            event_id=event.event_id,
            event_type=event.event_type.value,
        )
        from app.services import ticket_work_order_handoff

        occurred_at = datetime.fromisoformat(occurred_at_text)
        with _owner_session(db) as owner_db:
            ticket_work_order_handoff.consume_field_outcome(
                owner_db,
                work_order_id=coerce_uuid(work_order_id),
                field_event_id=coerce_uuid(field_event_id),
                outcome=outcome,
                occurred_at=occurred_at,
                note=event.payload.get("note"),
                actor_id=event.payload.get("actor_id"),
                event_id=event.event_id,
                context=self._context(event, str(work_order_id)),
            )

    def _auto_confirm_resolution(self, db: Session, event: Event) -> None:
        ticket_id = require_output_text(
            event.payload,
            "entity_id",
            consumer="support.ticket_lifecycle",
            event_id=event.event_id,
            event_type=_RESOLUTION_GRACE_TRIGGER,
        )
        from app.services.support import Tickets

        with _owner_session(db) as owner_db:
            Tickets.consume_resolution_confirmation_due(
                owner_db,
                ticket_id=str(ticket_id),
                event_id=event.event_id,
                context=self._context(event, str(ticket_id)),
            )

    def _wake_snoozed_conversation(self, db: Session, event: Event) -> None:
        conversation_id = require_output_text(
            event.payload,
            "entity_id",
            consumer="communications.team_inbox",
            event_id=event.event_id,
            event_type=_SNOOZE_WAKE_TRIGGER,
        )
        from app.services import team_inbox_commands

        with _owner_session(db) as owner_db:
            team_inbox_commands.consume_snooze_wake(
                owner_db,
                conversation_id=coerce_uuid(conversation_id),
                event_id=event.event_id,
                context=self._context(event, str(conversation_id)),
            )

    def _evaluate_sla_breach(self, db: Session, event: Event) -> None:
        clock_id = require_output_text(
            event.payload,
            "entity_id",
            consumer="support.ticket_sla_clock",
            event_id=event.event_id,
            event_type=_SLA_BREACH_TRIGGER,
        )
        from app.services.support import Tickets

        with _owner_session(db) as owner_db:
            Tickets.consume_sla_breach_due(
                owner_db,
                clock_id=str(clock_id),
                event_id=event.event_id,
                context=self._context(event, str(clock_id)),
            )
