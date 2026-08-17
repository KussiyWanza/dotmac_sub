"""Durable invitation consequence for provisioned reseller portal users."""

from __future__ import annotations

import hashlib
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.notification import NotificationChannel
from app.models.subscriber import ResellerUser, Subscriber, UserType
from app.schemas.notification import NotificationDeliveryLatency
from app.services.communication_intents import (
    CommunicationClass,
    CommunicationIntent,
)
from app.services.communication_intents import submit as submit_communication_intent
from app.services.ephemeral_communication_actions import (
    EPHEMERAL_ACTION_METADATA_KEY,
    RESELLER_USER_INVITE_ACTION,
)
from app.services.ephemeral_communication_actions import (
    descriptor as ephemeral_action_descriptor,
)
from app.services.events.types import Event, EventType

HANDLED_EVENT_TYPES = frozenset({EventType.reseller_user_provisioned})


class ResellerInviteHandler:
    """Expand one PII-safe provision event into a deduplicated email intent."""

    def handle(self, db: Session, event: Event) -> None:
        if event.event_type not in HANDLED_EVENT_TYPES:
            return
        if not event.payload.get("invite_requested"):
            return
        if event.payload.get("schema_version") != 1:
            raise ValueError("Unsupported reseller provisioning event schema")
        try:
            reseller_id = UUID(str(event.payload["reseller_id"]))
            principal_type = str(event.payload["principal_type"])
            principal_id = UUID(str(event.payload["principal_id"]))
            email_digest = str(event.payload["email_sha256"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Invalid reseller provisioning event context") from exc
        if principal_type == "subscriber":
            subscriber = db.get(Subscriber, principal_id)
            valid = bool(
                subscriber
                and subscriber.is_active
                and subscriber.user_type == UserType.reseller
                and subscriber.reseller_id == reseller_id
            )
            email = subscriber.email if subscriber else ""
            subscriber_id = principal_id
        elif principal_type == "reseller_user":
            reseller_user = db.get(ResellerUser, principal_id)
            valid = bool(
                reseller_user
                and reseller_user.is_active
                and reseller_user.reseller_id == reseller_id
                and reseller_user.subscriber_id is None
            )
            email = reseller_user.email or "" if reseller_user else ""
            subscriber_id = None
        else:
            raise ValueError("Unsupported reseller provisioning principal type")
        canonical_digest = hashlib.sha256(email.strip().lower().encode()).hexdigest()
        if not valid or not email or canonical_digest != email_digest:
            raise ValueError("Reseller provisioning event identity drifted")

        submit_communication_intent(
            db,
            CommunicationIntent(
                subscriber_id=subscriber_id,
                event_type="auth.reseller_user_invite",
                category="credentials",
                subject="Complete your reseller portal access",
                body=None,
                communication_class=CommunicationClass.transactional,
                channels=(NotificationChannel.email,),
                include_reseller=False,
                persist_policy_suppressions=False,
                recipients={NotificationChannel.email: email},
                audience_type=principal_type,
                audience_id=principal_id,
                resolve_subscriber_identity=False,
                metadata={
                    EPHEMERAL_ACTION_METADATA_KEY: ephemeral_action_descriptor(
                        action_type=RESELLER_USER_INVITE_ACTION,
                        version=1,
                        context={
                            "reseller_id": str(reseller_id),
                            "principal_type": principal_type,
                            "principal_id": str(principal_id),
                            "email_sha256": canonical_digest,
                        },
                    ),
                    "source_event_id": str(event.event_id),
                    "command_id": event.payload.get("command_id"),
                },
                dedupe_key=f"auth:reseller-invite:{event.event_id}",
                delivery_latency=NotificationDeliveryLatency.immediate,
            ),
        )
