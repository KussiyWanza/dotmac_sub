"""Durable delivery consequence for password-recovery requests."""

from __future__ import annotations

import hashlib
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.notification import NotificationChannel
from app.schemas.notification import NotificationDeliveryLatency
from app.services import credential_recovery
from app.services.communication_intents import (
    CommunicationClass,
    CommunicationIntent,
)
from app.services.communication_intents import submit as submit_communication_intent
from app.services.ephemeral_communication_actions import (
    EPHEMERAL_ACTION_METADATA_KEY,
    PASSWORD_RECOVERY_ACTION,
)
from app.services.ephemeral_communication_actions import (
    descriptor as ephemeral_action_descriptor,
)
from app.services.events.types import Event, EventType

HANDLED_EVENT_TYPES = frozenset({EventType.password_recovery_requested})


class PasswordRecoveryHandler:
    """Expand one PII-safe request event into a deduplicated email intent."""

    def handle(self, db: Session, event: Event) -> None:
        if event.event_type not in HANDLED_EVENT_TYPES:
            return
        if event.payload.get("schema_version") != 1:
            raise ValueError("Unsupported password recovery event schema")
        try:
            principal_type = str(event.payload["principal_type"])
            principal_id = UUID(str(event.payload["principal_id"]))
            email_digest = str(event.payload["email_sha256"])
            raw_next_login = event.payload.get("next_login_path")
            next_login_path = None if raw_next_login is None else str(raw_next_login)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Invalid password recovery event context") from exc
        target = credential_recovery.resolve_exact_recovery_target(
            db,
            principal_type=principal_type,
            principal_id=principal_id,
        )
        if target is None:
            return
        canonical_digest = hashlib.sha256(
            target.email.strip().lower().encode()
        ).hexdigest()
        if canonical_digest != email_digest:
            raise ValueError("Password recovery event identity drifted")

        submit_communication_intent(
            db,
            CommunicationIntent(
                subscriber_id=(
                    principal_id if principal_type == "subscriber" else None
                ),
                event_type="auth.password_recovery",
                category="credentials",
                subject="Password reset request",
                body=None,
                communication_class=CommunicationClass.transactional,
                channels=(NotificationChannel.email,),
                include_reseller=False,
                persist_policy_suppressions=False,
                recipients={NotificationChannel.email: target.email},
                audience_type=principal_type,
                audience_id=principal_id,
                resolve_subscriber_identity=False,
                metadata={
                    EPHEMERAL_ACTION_METADATA_KEY: ephemeral_action_descriptor(
                        action_type=PASSWORD_RECOVERY_ACTION,
                        version=1,
                        context={
                            "principal_type": principal_type,
                            "principal_id": str(principal_id),
                            "email_sha256": canonical_digest,
                            "next_login_path": next_login_path,
                        },
                    ),
                    "source_event_id": str(event.event_id),
                    "command_id": event.payload.get("command_id"),
                },
                dedupe_key=f"auth:password-recovery:{event.event_id}",
                delivery_latency=NotificationDeliveryLatency.immediate,
            ),
        )
