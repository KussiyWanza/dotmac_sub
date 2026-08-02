"""Project committed outage lifecycle facts into downstream owners.

The handler is a thin delivery adapter: incident transitions remain facts
owned by ``network.outage_lifecycle``, and each consequence runs through
that owner's receipted consumer commands (``consume_outage_activation`` /
``consume_outage_termination``) on a fresh owner-command session — the
effect and its unique ``(consumer, event_id)`` receipt commit atomically,
so a redelivery is an exact no-op. Detection and recovery remain
observation loops; outage resolution never closes support Tickets or
WorkOrders (Support and Field owners transition their own cases from
recovery evidence).

A consequence that cannot be applied raises so the event delivery stays
failed and retryable instead of a warning log.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.services.events.handlers.owner_session import owner_session as _owner_session
from app.services.events.owner_outputs import require_output_text
from app.services.events.types import Event, EventType

HANDLED_EVENT_TYPES = frozenset(
    {
        EventType.outage_created,
        EventType.outage_confirmed,
        EventType.outage_clearing,
        EventType.outage_reopened,
        EventType.outage_discarded,
        EventType.outage_resolved,
    }
)


class OutageLifecycleProjectionHandler:
    """Deliver committed outage outputs to their receipted consumers."""

    def handle(self, db: Session, event: Event) -> None:
        incident_id = require_output_text(
            event.payload,
            "incident_id",
            consumer="network.outage_lifecycle",
            event_id=event.event_id,
            event_type=event.event_type.value,
        )
        if event.event_type in {
            EventType.outage_created,
            EventType.outage_confirmed,
        }:
            self._apply_activation_consequences(db, event, incident_id)
        elif event.event_type in {
            EventType.outage_discarded,
            EventType.outage_resolved,
        }:
            self._cancel_escalations(db, event, incident_id)
        # Every handled transition also reconciles the downtime ledger via
        # the accrual owner's receipted consumer (clearing/reopened exist
        # for the recovery-hold continuity rules).
        self._apply_accrual(db, event, incident_id)

    def _apply_accrual(self, db: Session, event: Event, incident_id: str) -> None:
        from app.services.common import coerce_uuid
        from app.services.network import customer_outage_accrual
        from app.services.owner_commands import CommandContext

        # A distinct idempotency key per consumer: the lifecycle consumer and
        # the accrual consumer both receipt the same committed event.
        context = CommandContext.system(
            actor=str(event.actor or "system:outage_lifecycle_projection"),
            scope=str(incident_id),
            reason=event.event_type.value,
            command_id=event.event_id,
            correlation_id=event.event_id,
            causation_id=event.event_id,
            idempotency_key=f"event:{event.event_id}:accrual",
        )
        with _owner_session(db) as owner_db:
            customer_outage_accrual.consume_accrual_event(
                owner_db,
                incident_id=coerce_uuid(incident_id),
                event_id=event.event_id,
                event_type=event.event_type.value,
                context=context,
            )

    @staticmethod
    def _context(event: Event, incident_id: str):
        from app.services.owner_commands import CommandContext

        return CommandContext.system(
            actor=str(event.actor or "system:outage_lifecycle_projection"),
            scope=str(incident_id),
            reason=event.event_type.value,
            command_id=event.event_id,
            correlation_id=event.event_id,
            causation_id=event.event_id,
            idempotency_key=f"event:{event.event_id}",
        )

    def _apply_activation_consequences(
        self, db: Session, event: Event, incident_id: str
    ) -> None:
        from app.services.common import coerce_uuid
        from app.services.topology import outage

        with _owner_session(db) as owner_db:
            outage.consume_outage_activation(
                owner_db,
                incident_id=coerce_uuid(incident_id),
                event_id=event.event_id,
                event_type=event.event_type.value,
                context=self._context(event, incident_id),
            )

    def _cancel_escalations(self, db: Session, event: Event, incident_id: str) -> None:
        from app.services.common import coerce_uuid
        from app.services.topology import outage

        canceled_at: datetime | None = None
        resolved_at = event.payload.get("resolved_at")
        if resolved_at:
            canceled_at = datetime.fromisoformat(resolved_at)
        with _owner_session(db) as owner_db:
            outage.consume_outage_termination(
                owner_db,
                incident_id=coerce_uuid(incident_id),
                event_id=event.event_id,
                event_type=event.event_type.value,
                resolved_at=canceled_at,
                context=self._context(event, incident_id),
            )
