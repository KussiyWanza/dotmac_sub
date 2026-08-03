"""Route authoritative ticket/work-order outcomes into Survey invitations."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.comms import SurveyTriggerType
from app.services.common import coerce_uuid
from app.services.events.handlers.owner_session import owner_session as _owner_session
from app.services.events.types import Event, EventType
from app.services.owner_commands import CommandContext

HANDLED_EVENT_TYPES = frozenset(
    {
        EventType.ticket_resolution_confirmed,
        EventType.work_order_field_outcome_recorded,
    }
)


class SurveyTriggerHandler:
    """Create idempotent invitations only for currently active Surveys."""

    def handle(self, db: Session, event: Event) -> None:
        trigger_type: SurveyTriggerType | None = None
        source_entity_id = None
        if event.event_type is EventType.ticket_resolution_confirmed:
            trigger_type = SurveyTriggerType.ticket_closed
            source_entity_id = coerce_uuid(event.payload.get("ticket_id"))
        elif event.event_type is EventType.work_order_field_outcome_recorded:
            if event.payload.get("outcome") != "complete":
                return
            trigger_type = SurveyTriggerType.work_order_completed
            source_entity_id = coerce_uuid(event.payload.get("work_order_id"))
        if trigger_type is None:
            return

        from app.services import surveys

        context = CommandContext.system(
            actor=str(event.actor or "events.survey_trigger"),
            scope="communications.surveys:automatic-trigger",
            reason=event.event_type.value,
            command_id=event.event_id,
            correlation_id=event.event_id,
            causation_id=event.event_id,
            idempotency_key=f"survey-trigger:{event.event_id}",
        )
        with _owner_session(db) as owner_db:
            surveys.create_trigger_invitations(
                owner_db,
                surveys.TriggerSurveyInvitationsCommand(
                    trigger_type=trigger_type,
                    source_event_id=event.event_id,
                    source_entity_id=source_entity_id,
                    subscriber_id=event.subscriber_id,
                    context=context,
                ),
            )
