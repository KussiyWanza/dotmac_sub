"""Authoritative handoff from an inbox conversation to a support ticket.

An agent working a thread decides it needs a tracked incident. Ticket identity,
state and official timeline remain owned by ``support.ticket_lifecycle`` — this
service owns handoff eligibility, idempotency, and the conversation-to-ticket
provenance link. It is the only writer of ``Ticket.origin_conversation_id``.

Mirrors ``support.ticket_work_order_handoff``: the downstream owner creates the
row and accepts a keyword-only provenance argument, and this coordinator stages
the audit fact. One conversation may issue many tickets.

Deliberately *not* done here: resolving or otherwise transitioning the
conversation. Opening a ticket is not the same decision as closing a thread, and
conversation status belongs to ``communications.team_inbox``. A caller that
wants both makes both calls.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.audit import AuditActorType, AuditEvent
from app.models.support import Ticket
from app.models.team_inbox import InboxConversation, InboxConversationStatus
from app.schemas.support import TicketCreate
from app.services import support as support_service
from app.services.audit_adapter import stage_audit_event
from app.services.common import coerce_uuid
from app.services.domain_errors import DomainError
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

logger = logging.getLogger(__name__)

HandoffErrorKind = Literal["invalid", "forbidden", "not_found", "conflict"]

TICKET_ISSUE_SCOPE = "communications.conversation_ticket:issue"
_REQUIRED_PERMISSIONS = frozenset({"support:ticket:update"})
_ISSUE_DEFINITION = OwnerCommandDefinition(
    owner="communications.conversation_ticket_handoff",
    concern="conversation-to-ticket issuance eligibility",
    name="issue_conversation_ticket",
)
_AUDIT_ACTION = "conversation.ticket_issued"


class HandoffActorType(StrEnum):
    SYSTEM_USER = "system_user"
    API_KEY = "api_key"
    SERVICE = "service"


@dataclass(frozen=True)
class ConversationTicketIssueCommand:
    """Typed issuance request. Every field is an explicit control input."""

    conversation_id: UUID
    actor_id: object
    actor_type: HandoffActorType
    permission_keys: frozenset[str]
    title: str
    description: str | None = None
    priority: str | None = None
    reason: str | None = None
    request_id: str | None = None


class ConversationTicketHandoffError(DomainError):
    """``code`` is the machine contract; ``kind`` maps it to a response class."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        kind: HandoffErrorKind = "conflict",
    ) -> None:
        super().__init__(code=code, message=message, details={"kind": kind})
        self.kind = kind


@dataclass(frozen=True)
class ConversationTicketIssueResult:
    ticket: Ticket
    replayed: bool


def _actor_type(actor_type: HandoffActorType) -> AuditActorType:
    """Map the transport principal onto the audit actor vocabulary.

    Matches `ticket_work_order_handoff`: a staff principal audits as `user`;
    there is no `system_user` member.
    """
    if actor_type is HandoffActorType.API_KEY:
        return AuditActorType.api_key
    if actor_type is HandoffActorType.SERVICE:
        return AuditActorType.service
    return AuditActorType.user


def _normalize_actor(actor_id: object | None) -> UUID:
    actor_uuid = coerce_uuid(actor_id)
    if actor_uuid is None:
        raise ConversationTicketHandoffError(
            "actor_required",
            "A known actor must issue the ticket.",
            kind="forbidden",
        )
    return actor_uuid


def _active_conversation(db: Session, conversation_id: UUID) -> InboxConversation:
    conversation = db.get(InboxConversation, conversation_id)
    if conversation is None or not conversation.is_active:
        raise ConversationTicketHandoffError(
            "conversation_not_found",
            "Conversation not found.",
            kind="not_found",
        )
    return conversation


def _stable_request_id(command: ConversationTicketIssueCommand) -> str:
    """Derive a replay key from the issuance intent.

    A transport request id is not trustworthy for this: a double-submitted form
    sends a fresh one each time. Hashing the conversation, actor and title means
    the same intent replays instead of opening a second ticket.
    """
    digest = hashlib.sha256(
        "|".join(
            [
                str(command.conversation_id),
                str(command.actor_id),
                (command.title or "").strip().casefold(),
            ]
        ).encode("utf-8")
    ).hexdigest()
    return f"conversation-ticket:{digest[:32]}"


def _validate_issue_eligibility(
    db: Session, command: ConversationTicketIssueCommand
) -> InboxConversation:
    missing = _REQUIRED_PERMISSIONS - set(command.permission_keys)
    if missing:
        raise ConversationTicketHandoffError(
            "permission_denied",
            "Issuing a ticket from a conversation requires "
            + ", ".join(sorted(missing))
            + ".",
            kind="forbidden",
        )
    if not (command.title or "").strip():
        raise ConversationTicketHandoffError(
            "title_required",
            "A ticket title is required.",
            kind="invalid",
        )

    conversation = _active_conversation(db, command.conversation_id)
    if conversation.status == InboxConversationStatus.resolved.value:
        raise ConversationTicketHandoffError(
            "conversation_resolved",
            "Reopen the conversation before issuing a ticket from it.",
            kind="conflict",
        )
    return conversation


def list_for_conversation(db: Session, conversation_id: str | UUID) -> list[Ticket]:
    """Tickets issued from this conversation, newest first."""
    conversation_uuid = coerce_uuid(conversation_id)
    if conversation_uuid is None:
        return []
    return (
        db.query(Ticket)
        .filter(Ticket.origin_conversation_id == conversation_uuid)
        .filter(Ticket.is_active.is_(True))
        .order_by(Ticket.created_at.desc())
        .all()
    )


def issue_ticket(
    db: Session, command: ConversationTicketIssueCommand
) -> ConversationTicketIssueResult:
    """Public entrypoint — enters the canonical handoff transaction."""
    context = CommandContext.system(
        actor=f"{command.actor_type.value}:{command.actor_id}"[:80],
        scope=TICKET_ISSUE_SCOPE,
        reason=command.reason or "issue ticket from inbox conversation",
        idempotency_key=_stable_request_id(command),
    )
    # The invalidation payload is captured inside the transaction, not read off
    # the Ticket afterwards: attributes expire on commit, so a post-commit read
    # would re-open a transaction and leave the session unusable for the next
    # owner command.
    invalidation: dict[str, Any] = {}

    def run() -> ConversationTicketIssueResult:
        outcome = _issue_ticket(db, command)
        if not outcome.replayed:
            invalidation.update(
                item_id=outcome.ticket.id,
                assignee_id=outcome.ticket.assigned_to_person_id,
                service_team_id=outcome.ticket.service_team_id,
            )
        return outcome

    result = execute_owner_command(
        db,
        definition=_ISSUE_DEFINITION,
        context=context,
        operation=run,
    )
    # The Ticket create ran as a participant, so its own post-commit workqueue
    # invalidation was skipped. Emit it here, now the transaction has landed.
    if invalidation:
        _emit_workqueue_invalidation(**invalidation)
    return result


def _emit_workqueue_invalidation(
    *,
    item_id: UUID,
    assignee_id: UUID | None,
    service_team_id: UUID | None,
) -> None:
    """Best-effort realtime ping. Workqueue holds no authority."""
    try:
        from app.services.workqueue.events import emit_item_change
        from app.services.workqueue.types import ItemKind

        emit_item_change(
            item_kind=ItemKind.ticket,
            item_id=item_id,
            change="added",
            assignee_id=assignee_id,
            service_team_id=service_team_id,
        )
    except Exception:  # pragma: no cover — realtime must never fail a write
        logger.debug(
            "workqueue_notify_failed conversation_ticket_id=%s", item_id, exc_info=True
        )


def _issue_ticket(
    db: Session, command: ConversationTicketIssueCommand
) -> ConversationTicketIssueResult:
    conversation = _validate_issue_eligibility(db, command)
    actor_uuid = _normalize_actor(command.actor_id)
    stable_request_id = _stable_request_id(command)

    existing_audit = (
        db.query(AuditEvent)
        .filter(AuditEvent.action == _AUDIT_ACTION)
        .filter(AuditEvent.entity_type == "inbox_conversation")
        .filter(AuditEvent.entity_id == str(conversation.id))
        .filter(AuditEvent.request_id == stable_request_id)
        .one_or_none()
    )
    if existing_audit is not None:
        replayed = list_for_conversation(db, conversation.id)
        if replayed:
            return ConversationTicketIssueResult(ticket=replayed[0], replayed=True)

    ticket = support_service.Tickets.create(
        db,
        TicketCreate(
            title=command.title.strip()[:255],
            description=command.description,
            # The conversation already carries the resolved subscriber; the
            # ticket inherits it rather than re-deriving identity.
            subscriber_id=conversation.subscriber_id,
            service_team_id=conversation.primary_service_team_id,
            priority=command.priority or "normal",
            created_by_person_id=actor_uuid,
        ),
        actor_id=str(actor_uuid),
        origin_conversation_id=conversation.id,
    )

    stage_audit_event(
        db,
        action=_AUDIT_ACTION,
        entity_type="inbox_conversation",
        entity_id=str(conversation.id),
        actor_type=_actor_type(command.actor_type),
        actor_id=str(actor_uuid),
        request_id=stable_request_id,
        metadata={
            "owner": "communications.conversation_ticket_handoff",
            "ticket_id": str(ticket.id),
            "ticket_number": ticket.number,
            "channel_type": conversation.channel_type,
            "subscriber_id": str(conversation.subscriber_id)
            if conversation.subscriber_id
            else None,
            "reason": command.reason,
            "transport_request_id": command.request_id,
        },
    )
    return ConversationTicketIssueResult(ticket=ticket, replayed=False)
