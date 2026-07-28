"""Deliver identity/onboarding timer triggers to receipted consumers.

The handler is a thin delivery adapter for the invitation lifecycle
(docs/designs/IDENTITY_ONBOARDING_CHAIN.md): a fired invitation-expiry
timer reaches the receipted expiry consumer, whose effect and unique
``(consumer, event_id)`` receipt commit atomically. Expiry is affirmative
evidence only — the capability's redeem-time TTL check remains the
fail-closed access gate.

A consequence that cannot be applied raises so the event delivery stays
failed and retryable instead of a warning log.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.services.events.handlers.owner_session import owner_session as _owner_session
from app.services.events.owner_outputs import require_output_text
from app.services.events.types import Event, EventType

HANDLED_EVENT_TYPES = frozenset({EventType.custom})

_INVITATION_EXPIRY_TRIGGER = "auth.access_invitation_expiry_due"


class IdentityLifecycleProjectionHandler:
    """Route identity timer triggers to their receipted consumers."""

    def handle(self, db: Session, event: Event) -> None:
        if event.event_type != EventType.custom:
            return
        if event.payload.get("trigger") != _INVITATION_EXPIRY_TRIGGER:
            # Every other custom payload belongs to other adapters.
            return
        invitation_id = require_output_text(
            event.payload,
            "entity_id",
            consumer="auth.access_invitations",
            event_id=event.event_id,
            event_type=_INVITATION_EXPIRY_TRIGGER,
        )
        from app.services import access_invitations
        from app.services.owner_commands import CommandContext

        with _owner_session(db) as owner_db:
            access_invitations.consume_invitation_expiry(
                owner_db,
                invitation_id=str(invitation_id),
                event_id=event.event_id,
                context=CommandContext.system(
                    actor=str(event.actor or "auth.access_invitations"),
                    scope=str(invitation_id),
                    reason=_INVITATION_EXPIRY_TRIGGER,
                    command_id=event.event_id,
                    correlation_id=event.event_id,
                    causation_id=event.event_id,
                    idempotency_key=f"event:{event.event_id}",
                ),
            )
