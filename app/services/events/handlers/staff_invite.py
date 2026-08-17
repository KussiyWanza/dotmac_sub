"""Durable invitation consequence for newly provisioned staff accounts."""

from __future__ import annotations

import hashlib
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.notification import NotificationChannel
from app.models.system_user import SystemUser
from app.schemas.notification import NotificationDeliveryLatency
from app.services.communication_intents import (
    CommunicationClass,
    CommunicationIntent,
)
from app.services.communication_intents import submit as submit_communication_intent
from app.services.ephemeral_communication_actions import (
    EPHEMERAL_ACTION_METADATA_KEY,
    STAFF_ACCOUNT_INVITE_ACTION,
)
from app.services.ephemeral_communication_actions import (
    descriptor as ephemeral_action_descriptor,
)
from app.services.events.types import Event, EventType

HANDLED_EVENT_TYPES = frozenset({EventType.staff_account_provisioned})


class StaffInviteHandler:
    """Expand a PII-safe provision event into one deduplicated email intent."""

    def handle(self, db: Session, event: Event) -> None:
        if event.event_type not in HANDLED_EVENT_TYPES:
            return
        if not event.payload.get("invite_requested"):
            return
        if event.payload.get("schema_version") != 1:
            raise ValueError("Unsupported staff provisioning event schema")
        try:
            user_id = UUID(str(event.payload["user_id"]))
            email_digest = str(event.payload["email_sha256"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Invalid staff provisioning event context") from exc

        user = db.get(SystemUser, user_id)
        if user is None or not user.is_active:
            return
        canonical_digest = hashlib.sha256(
            user.email.strip().lower().encode()
        ).hexdigest()
        if canonical_digest != email_digest:
            raise ValueError("Staff provisioning event identity drifted")

        submit_communication_intent(
            db,
            CommunicationIntent(
                subscriber_id=None,
                event_type="auth.staff_account_invite",
                category="credentials",
                subject="Complete your staff access",
                body=None,
                communication_class=CommunicationClass.transactional,
                channels=(NotificationChannel.email,),
                include_reseller=False,
                persist_policy_suppressions=False,
                recipients={NotificationChannel.email: user.email},
                audience_type="system_user",
                audience_id=user.id,
                resolve_subscriber_identity=False,
                metadata={
                    EPHEMERAL_ACTION_METADATA_KEY: ephemeral_action_descriptor(
                        action_type=STAFF_ACCOUNT_INVITE_ACTION,
                        version=1,
                        context={
                            "user_id": str(user.id),
                            "email_sha256": canonical_digest,
                        },
                    ),
                    "source_event_id": str(event.event_id),
                    "command_id": event.payload.get("command_id"),
                },
                dedupe_key=f"auth:staff-invite:{event.event_id}",
                delivery_latency=NotificationDeliveryLatency.immediate,
            ),
        )
