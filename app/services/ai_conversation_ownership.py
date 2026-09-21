"""Authoritative AI ownership resolver for Team Inbox conversations.

``ai.intake`` owns the active session lifecycle.  Team Inbox consumes this
typed read contract when deciding whether a human mutation may proceed; the
denormalized ``InboxConversation.metadata["ai_handling"]`` flag is only a
repairable display projection and is never an admission input here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.models.ai_intake import AiIntakeSession
from app.models.team_inbox import InboxConversation, InboxConversationStatus
from app.services.domain_errors import DomainError


class AiOwnershipProvenance(StrEnum):
    active_ai_intake_session = "active_ai_intake_session"
    no_active_ai_intake_session = "no_active_ai_intake_session"


class ConversationControlOwner(StrEnum):
    ai = "ai"
    human = "human"


class ConversationOwnershipCohort(StrEnum):
    actionable = "actionable"
    ai_intake = "ai_intake"
    queue = "queue"
    history = "history"


class HumanConversationMutation(StrEnum):
    reply = "reply"
    scheduled_reply = "scheduled_reply"
    private_note = "private_note"
    assignment = "assignment"
    status = "status"
    workflow = "workflow"
    ticket = "ticket"
    macro = "macro"
    bulk = "bulk"
    automation = "automation"
    label = "label"
    contact = "contact"
    lead = "lead"
    comment = "comment"
    transcript = "transcript"


@dataclass(frozen=True, slots=True)
class AiConversationOwnership:
    conversation_id: UUID
    ai_owned: bool
    control_owner: ConversationControlOwner
    ai_session_id: UUID | None
    ai_session_state: str | None
    waiting_for_customer: bool
    handoff_requested: bool
    provenance: AiOwnershipProvenance
    can_take_over: bool


@dataclass(frozen=True, slots=True)
class AiOutboundDeliveryDecision:
    applicable: bool
    allowed: bool
    conversation_id: UUID | None
    ai_session_id: UUID | None
    reason: str | None


class AiConversationOwnedError(DomainError):
    """Stable conflict raised when a normal human mutation targets AI work."""

    def __init__(
        self,
        ownership: AiConversationOwnership,
        *,
        mutation: HumanConversationMutation,
    ) -> None:
        super().__init__(
            code="communications.team_inbox_commands.ai_owned",
            message=(
                "AI Intake currently owns this conversation. Use Take Over "
                "Conversation before performing human actions."
            ),
            details={
                "conversation_id": str(ownership.conversation_id),
                "ai_session_id": (
                    str(ownership.ai_session_id)
                    if ownership.ai_session_id is not None
                    else None
                ),
                "ai_session_state": ownership.ai_session_state,
                "control_owner": ownership.control_owner.value,
                "mutation": mutation.value,
            },
        )
        self.ownership = ownership
        self.mutation = mutation


class AiTakeoverConflictError(DomainError):
    def __init__(self, message: str, *, conversation_id: UUID, **details: object):
        super().__init__(
            code="communications.team_inbox_commands.takeover_conflict",
            message=message,
            details={"conversation_id": str(conversation_id), **details},
        )


class AiTakeoverPermissionError(DomainError):
    def __init__(self, *, missing_permissions: tuple[str, ...]):
        super().__init__(
            code="communications.team_inbox_commands.takeover_permission_denied",
            message="You do not have permission to take over this conversation.",
            details={"missing_permissions": missing_permissions},
        )


def ai_owned_conversation_clause(
    conversation_id: ColumnElement[UUID] | None = None,
) -> ColumnElement[bool]:
    """Correlated SQL predicate backed by the authoritative active session."""

    target = conversation_id if conversation_id is not None else InboxConversation.id
    return exists(
        select(AiIntakeSession.id).where(
            AiIntakeSession.conversation_id == target,
            AiIntakeSession.completed_at.is_(None),
        )
    )


def active_session(
    db: Session,
    *,
    conversation_id: UUID,
    for_update: bool = False,
) -> AiIntakeSession | None:
    query = (
        db.query(AiIntakeSession)
        .filter(AiIntakeSession.conversation_id == conversation_id)
        .filter(AiIntakeSession.completed_at.is_(None))
    )
    if for_update:
        query = query.with_for_update()
    return query.one_or_none()


def resolve_ai_conversation_ownership(
    db: Session,
    *,
    conversation_id: UUID,
    for_update: bool = False,
) -> AiConversationOwnership:
    session = active_session(
        db,
        conversation_id=conversation_id,
        for_update=for_update,
    )
    ai_owned = session is not None
    return AiConversationOwnership(
        conversation_id=conversation_id,
        ai_owned=ai_owned,
        control_owner=(
            ConversationControlOwner.ai if ai_owned else ConversationControlOwner.human
        ),
        ai_session_id=session.id if session is not None else None,
        ai_session_state=session.state if session is not None else None,
        waiting_for_customer=bool(
            session is not None and session.state == "awaiting_customer"
        ),
        handoff_requested=bool(
            session is not None and session.state == "handoff_requested"
        ),
        provenance=(
            AiOwnershipProvenance.active_ai_intake_session
            if ai_owned
            else AiOwnershipProvenance.no_active_ai_intake_session
        ),
        can_take_over=ai_owned,
    )


def ownership_by_conversation_ids(
    db: Session,
    conversation_ids: Sequence[UUID],
) -> dict[UUID, AiConversationOwnership]:
    ids = tuple(dict.fromkeys(conversation_ids))
    if not ids:
        return {}
    sessions = (
        db.query(AiIntakeSession)
        .filter(AiIntakeSession.conversation_id.in_(ids))
        .filter(AiIntakeSession.completed_at.is_(None))
        .all()
    )
    by_conversation = {session.conversation_id: session for session in sessions}
    outcomes: dict[UUID, AiConversationOwnership] = {}
    for conversation_id in ids:
        session = by_conversation.get(conversation_id)
        ai_owned = session is not None
        outcomes[conversation_id] = AiConversationOwnership(
            conversation_id=conversation_id,
            ai_owned=ai_owned,
            control_owner=(
                ConversationControlOwner.ai
                if ai_owned
                else ConversationControlOwner.human
            ),
            ai_session_id=session.id if session is not None else None,
            ai_session_state=session.state if session is not None else None,
            waiting_for_customer=bool(
                session is not None and session.state == "awaiting_customer"
            ),
            handoff_requested=bool(
                session is not None and session.state == "handoff_requested"
            ),
            provenance=(
                AiOwnershipProvenance.active_ai_intake_session
                if ai_owned
                else AiOwnershipProvenance.no_active_ai_intake_session
            ),
            can_take_over=ai_owned,
        )
    return outcomes


def require_human_control(
    db: Session,
    *,
    conversation_id: UUID,
    mutation: HumanConversationMutation,
    for_update: bool = True,
) -> AiConversationOwnership:
    ownership = resolve_ai_conversation_ownership(
        db,
        conversation_id=conversation_id,
        for_update=for_update,
    )
    if ownership.ai_owned:
        raise AiConversationOwnedError(ownership, mutation=mutation)
    return ownership


def is_ai_outbound_intent(metadata: Mapping[str, object]) -> bool:
    """Identify an AI-authored delivery intent without deciding ownership."""

    return bool(
        metadata.get("ai_intake_session_id")
        or str(metadata.get("sender_type") or "").strip().lower() == "ai"
        or str(metadata.get("author_type") or "").strip().lower() == "ai"
        or str(metadata.get("automation_kind") or "").strip().lower() == "ai_intake"
    )


def decide_ai_outbound_delivery(
    db: Session,
    *,
    conversation_id: UUID | None,
    metadata: Mapping[str, object],
) -> AiOutboundDeliveryDecision:
    """Fail closed for queued AI Intake messages whose authority has ended."""

    raw_session_id = metadata.get("ai_intake_session_id")
    applicable = is_ai_outbound_intent(metadata)
    if not applicable:
        return AiOutboundDeliveryDecision(
            applicable=False,
            allowed=True,
            conversation_id=conversation_id,
            ai_session_id=None,
            reason=None,
        )
    try:
        session_id = UUID(str(raw_session_id))
    except (TypeError, ValueError):
        return AiOutboundDeliveryDecision(
            applicable=True,
            allowed=False,
            conversation_id=conversation_id,
            ai_session_id=None,
            reason="ai_session_reference_missing",
        )
    session = db.get(AiIntakeSession, session_id)
    if session is None:
        return AiOutboundDeliveryDecision(
            applicable=True,
            allowed=False,
            conversation_id=conversation_id,
            ai_session_id=session_id,
            reason="ai_session_not_found",
        )
    if conversation_id is None or session.conversation_id != conversation_id:
        return AiOutboundDeliveryDecision(
            applicable=True,
            allowed=False,
            conversation_id=conversation_id,
            ai_session_id=session_id,
            reason="ai_session_conversation_mismatch",
        )
    conversation = db.get(InboxConversation, conversation_id)
    if (
        session.completed_at is not None
        or session.state == "stopped_human_takeover"
        or conversation is None
        or not conversation.is_active
        or conversation.status == InboxConversationStatus.resolved.value
    ):
        return AiOutboundDeliveryDecision(
            applicable=True,
            allowed=False,
            conversation_id=conversation_id,
            ai_session_id=session_id,
            reason="ai_ownership_ended",
        )
    return AiOutboundDeliveryDecision(
        applicable=True,
        allowed=True,
        conversation_id=conversation_id,
        ai_session_id=session_id,
        reason=None,
    )
