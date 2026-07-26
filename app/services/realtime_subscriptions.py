"""Authorization owner for client-selected real-time topics."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.dispatch import TechnicianProfile
from app.models.network_operation import NetworkOperation, NetworkOperationTargetType
from app.models.team_inbox import InboxConversation
from app.services.auth_dependencies import has_permission
from app.services.realtime_platform import conversation_topic, operation_topic


@dataclass(frozen=True)
class RealtimeSubscriptionError(ValueError):
    code: str
    message: str

    def __str__(self) -> str:
        return self.message


_OPERATION_READ_PERMISSION = {
    NetworkOperationTargetType.olt: "network:olt:read",
    NetworkOperationTargetType.ont: "network:ont:read",
    NetworkOperationTargetType.cpe: "network:cpe:read",
    NetworkOperationTargetType.router: "network:device:read",
    NetworkOperationTargetType.nas: "network:nas:read",
    NetworkOperationTargetType.system: "network:device:read",
}


def _topic_parts(requested_topic: str) -> tuple[str | None, UUID]:
    value = requested_topic.strip()
    kind: str | None = None
    raw_id = value
    if ":" in value:
        kind, raw_id = value.split(":", 1)
        if kind not in {"conversation", "operation"}:
            raise RealtimeSubscriptionError(
                "topic_not_client_subscribable",
                "That real-time topic cannot be selected by a client",
            )
    try:
        return kind, UUID(raw_id)
    except ValueError as exc:
        raise RealtimeSubscriptionError(
            "invalid_topic", "A valid conversation or operation topic is required"
        ) from exc


def _can_read_conversation(auth: dict, conversation: InboxConversation) -> bool:
    if auth.get("principal_type", "subscriber") == "subscriber":
        return str(conversation.subscriber_id or "") == str(auth.get("principal_id"))
    return False


def _person_id_for_principal(db: Session, auth: dict) -> UUID | None:
    """Map a staff principal to the neutral person identity assignments use."""
    try:
        principal_uuid = UUID(str(auth.get("principal_id")))
    except (TypeError, ValueError):
        return None
    profile = (
        db.query(TechnicianProfile)
        .filter(TechnicianProfile.system_user_id == principal_uuid)
        .one_or_none()
    )
    if profile is not None:
        return profile.person_id
    # Native staff hold the same id for both identities.
    return principal_uuid


def _holds_assignment(db: Session, auth: dict, conversation: InboxConversation) -> bool:
    """True for the staff member who actually holds this conversation.

    A technician on a job chat is authorized by holding the assignment rather
    than by a support permission: the field role does not necessarily carry
    ``support:ticket:read``, and a 1:1 job chat should be readable by the
    technician on that job, not by anyone with inbox access.
    """
    person_id = _person_id_for_principal(db, auth)
    if person_id is None:
        return False
    return any(
        assignment.is_active and str(assignment.person_id) == str(person_id)
        for assignment in conversation.assignments
    )


def authorize_topic(db: Session, auth: dict, requested_topic: str) -> str:
    """Resolve a legacy/explicit topic and enforce its object-level read gate."""
    kind, object_id = _topic_parts(requested_topic)

    if kind in {None, "conversation"}:
        conversation = db.get(InboxConversation, object_id)
        if conversation is not None:
            widget_conversation_id = auth.get("conversation_id")
            if widget_conversation_id:
                if str(conversation.id) == str(widget_conversation_id):
                    return conversation_topic(object_id)
                raise RealtimeSubscriptionError(
                    "topic_forbidden",
                    "Chat sessions can only subscribe to their conversation",
                )
            if (
                _can_read_conversation(auth, conversation)
                or _holds_assignment(db, auth, conversation)
                or has_permission(auth, db, "support:ticket:read")
            ):
                return conversation_topic(object_id)
            raise RealtimeSubscriptionError(
                "topic_forbidden", "You cannot subscribe to this conversation"
            )
        if kind == "conversation":
            raise RealtimeSubscriptionError("topic_not_found", "Conversation not found")

    if auth.get("conversation_id"):
        raise RealtimeSubscriptionError(
            "topic_forbidden", "Chat sessions can only subscribe to their conversation"
        )

    if kind in {None, "operation"}:
        operation = db.get(NetworkOperation, object_id)
        if operation is not None:
            permission = _OPERATION_READ_PERMISSION[operation.target_type]
            if has_permission(auth, db, permission):
                return operation_topic(object_id)
            raise RealtimeSubscriptionError(
                "topic_forbidden", "You cannot subscribe to this operation"
            )

    raise RealtimeSubscriptionError("topic_not_found", "Real-time topic not found")
