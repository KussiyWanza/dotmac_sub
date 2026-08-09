"""Canonical Inbox conversation-to-Lead relationship participant and query."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.lead_intake import LeadIntakeInvitation
from app.models.party import PartyContactPoint
from app.models.sales import Lead
from app.models.subscriber import Subscriber
from app.models.team_inbox import (
    InboxConversation,
    InboxConversationLeadLink,
    InboxConversationParticipant,
)
from app.services.audit_adapter import stage_audit_event
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.owner_commands import CommandContext, owner_command_active

OWNER = "communications.conversation_lead_relationships"


class ConversationLeadLinkSource(StrEnum):
    existing_link = "existing_link"
    exact_party_lead = "exact_party_lead"
    reviewed_selection = "reviewed_selection"
    inbox_lead_intake = "inbox_lead_intake"
    inbox_lead_authoring = "inbox_lead_authoring"
    fiber_website_inquiry = "fiber_website_inquiry"
    fiber_website_chat = "fiber_website_chat"
    reviewed_repair = "reviewed_repair"


class ConversationLeadRelationshipError(DomainError):
    pass


class ConversationLeadDriftKind(StrEnum):
    completed_intake_missing_link = "completed_intake_missing_link"
    link_party_mismatch = "link_party_mismatch"
    conversation_party_mismatch = "conversation_party_mismatch"


@dataclass(frozen=True, slots=True)
class ConversationLeadLinkCommand:
    context: CommandContext
    conversation_id: UUID
    lead_id: UUID
    party_id: UUID
    actor_person_id: UUID | None
    source: ConversationLeadLinkSource
    reason: str


@dataclass(frozen=True, slots=True)
class ConversationLeadLinkOutcome:
    link_id: UUID
    conversation_id: UUID
    lead_id: UUID
    party_id: UUID
    replayed: bool


@dataclass(frozen=True, slots=True)
class ConversationLeadDriftFinding:
    conversation_id: UUID
    lead_id: UUID
    party_id: UUID
    kind: ConversationLeadDriftKind


def _error(suffix: str, message: str, **details: object) -> DomainError:
    return ConversationLeadRelationshipError(
        code=f"communications.conversation_lead_relationships.{suffix}",
        message=message,
        details=details,
    )


def active_link(db: Session, conversation_id: UUID) -> InboxConversationLeadLink | None:
    return db.scalar(
        select(InboxConversationLeadLink).where(
            InboxConversationLeadLink.conversation_id == conversation_id,
            InboxConversationLeadLink.is_active.is_(True),
        )
    )


def conversation_links_lead(
    db: Session, *, conversation_id: UUID, lead_id: UUID
) -> bool:
    link = active_link(db, conversation_id)
    if link is not None:
        return link.lead_id == lead_id
    return (
        db.scalar(
            select(LeadIntakeInvitation.id)
            .where(
                LeadIntakeInvitation.conversation_id == conversation_id,
                LeadIntakeInvitation.status == "completed",
                LeadIntakeInvitation.lead_id == lead_id,
            )
            .limit(1)
        )
        is not None
    )


def conversation_links_subscriber(
    db: Session, *, conversation_id: UUID, subscriber_id: UUID
) -> bool:
    return (
        db.scalar(
            select(InboxConversation.id).where(
                InboxConversation.id == conversation_id,
                InboxConversation.subscriber_id == subscriber_id,
                InboxConversation.is_active.is_(True),
            )
        )
        is not None
    )


def drift_report(
    db: Session, *, limit: int = 500
) -> tuple[ConversationLeadDriftFinding, ...]:
    """Report only structural drift; never infer provenance from contact values."""

    bounded_limit = max(1, min(limit, 2000))
    findings: list[ConversationLeadDriftFinding] = []
    completed = tuple(
        db.scalars(
            select(LeadIntakeInvitation)
            .where(
                LeadIntakeInvitation.status == "completed",
                LeadIntakeInvitation.lead_id.is_not(None),
                LeadIntakeInvitation.party_id.is_not(None),
            )
            .order_by(LeadIntakeInvitation.completed_at, LeadIntakeInvitation.id)
            .limit(bounded_limit)
        ).all()
    )
    for invitation in completed:
        if invitation.lead_id is None or invitation.party_id is None:
            continue
        if active_link(db, invitation.conversation_id) is None:
            findings.append(
                ConversationLeadDriftFinding(
                    invitation.conversation_id,
                    invitation.lead_id,
                    invitation.party_id,
                    ConversationLeadDriftKind.completed_intake_missing_link,
                )
            )
    remaining = bounded_limit - len(findings)
    if remaining <= 0:
        return tuple(findings)
    links = tuple(
        db.scalars(
            select(InboxConversationLeadLink)
            .where(InboxConversationLeadLink.is_active.is_(True))
            .order_by(InboxConversationLeadLink.linked_at, InboxConversationLeadLink.id)
            .limit(remaining)
        ).all()
    )
    for link in links:
        lead = db.get(Lead, link.lead_id)
        if lead is None or lead.party_id != link.party_id:
            findings.append(
                ConversationLeadDriftFinding(
                    link.conversation_id,
                    link.lead_id,
                    link.party_id,
                    ConversationLeadDriftKind.link_party_mismatch,
                )
            )
            continue
        conversation = db.get(InboxConversation, link.conversation_id)
        if conversation is None:
            continue
        exact_ids = exact_party_ids(db, conversation)
        if exact_ids and (len(exact_ids) != 1 or exact_ids[0] != link.party_id):
            findings.append(
                ConversationLeadDriftFinding(
                    link.conversation_id,
                    link.lead_id,
                    link.party_id,
                    ConversationLeadDriftKind.conversation_party_mismatch,
                )
            )
    return tuple(findings[:bounded_limit])


def exact_party_ids(db: Session, conversation: InboxConversation) -> tuple[UUID, ...]:
    """Return only structurally reviewed Party identities for a conversation."""

    if conversation.subscriber_id is not None:
        subscriber = db.get(Subscriber, conversation.subscriber_id)
        if subscriber is None or subscriber.party_id is None:
            return ()
        return (subscriber.party_id,)

    participant_party_ids = tuple(
        db.scalars(
            select(PartyContactPoint.party_id)
            .join(
                InboxConversationParticipant,
                InboxConversationParticipant.party_contact_point_id
                == PartyContactPoint.id,
            )
            .where(
                InboxConversationParticipant.conversation_id == conversation.id,
                InboxConversationParticipant.is_active.is_(True),
                PartyContactPoint.is_active.is_(True),
            )
            .distinct()
            .order_by(PartyContactPoint.party_id)
        ).all()
    )
    if participant_party_ids:
        return participant_party_ids

    completed_party_ids = tuple(
        db.scalars(
            select(LeadIntakeInvitation.party_id)
            .where(
                LeadIntakeInvitation.conversation_id == conversation.id,
                LeadIntakeInvitation.status == "completed",
                LeadIntakeInvitation.party_id.is_not(None),
            )
            .distinct()
            .order_by(LeadIntakeInvitation.party_id)
        ).all()
    )
    return tuple(party_id for party_id in completed_party_ids if party_id is not None)


def require_exact_party(
    db: Session, conversation: InboxConversation, expected_party_id: UUID
) -> None:
    party_ids = exact_party_ids(db, conversation)
    if len(party_ids) != 1 or party_ids[0] != expected_party_id:
        raise _error(
            "party_mismatch",
            "The conversation no longer resolves to the selected Party.",
            conversation_id=str(conversation.id),
            expected_party_id=str(expected_party_id),
            exact_party_count=len(party_ids),
        )


def require_new_prospect_conversation(
    db: Session, conversation_id: UUID
) -> InboxConversation:
    """Lock and validate the fail-closed new-prospect authoring precondition."""

    conversation = db.scalar(
        select(InboxConversation)
        .where(
            InboxConversation.id == conversation_id,
            InboxConversation.is_active.is_(True),
        )
        .with_for_update()
    )
    if conversation is None:
        raise _error("conversation_not_found", "Inbox conversation was not found.")
    if active_link(db, conversation.id) is not None:
        raise _error(
            "lead_already_linked",
            "This conversation is already linked to a Lead.",
        )
    if conversation.subscriber_id is not None:
        raise _error(
            "party_already_linked",
            "This conversation already has authoritative customer identity.",
        )
    if exact_party_ids(db, conversation):
        raise _error(
            "party_already_linked",
            "This conversation already resolves to an authoritative Party.",
        )

    # Candidate equality is review evidence only. It blocks automatic identity
    # creation but never establishes or merges identity.
    from app.services import team_inbox_contact_links

    candidates = team_inbox_contact_links.contact_link_candidates(
        db, [str(conversation.contact_address or "")]
    )
    if candidates.get("subscribers") or candidates.get("resellers"):
        raise _error(
            "identity_review_required",
            "Potential customer matches require reviewed identity selection.",
        )
    return conversation


def link_conversation_lead_participant(
    db: Session, command: ConversationLeadLinkCommand
) -> ConversationLeadLinkOutcome:
    """Stage one immutable active link inside the caller's owner transaction."""

    if not owner_command_active(db):
        raise _error(
            "owner_command_required",
            "Conversation-to-Lead links require an active owner command.",
        )
    reason = command.reason.strip()
    if not reason:
        raise _error("reason_required", "A relationship reason is required.")

    conversation = db.scalar(
        select(InboxConversation)
        .where(
            InboxConversation.id == command.conversation_id,
            InboxConversation.is_active.is_(True),
        )
        .with_for_update()
    )
    if conversation is None:
        raise _error("conversation_not_found", "Inbox conversation was not found.")
    lead = db.scalar(select(Lead).where(Lead.id == command.lead_id).with_for_update())
    if lead is None or lead.party_id is None:
        raise _error("lead_not_found", "The selected Party-bound Lead was not found.")
    if lead.party_id != command.party_id:
        raise _error(
            "lead_party_mismatch",
            "The selected Lead does not belong to the conversation Party.",
        )

    existing = active_link(db, conversation.id)
    if existing is not None:
        if existing.lead_id != lead.id or existing.party_id != command.party_id:
            raise _error(
                "active_link_conflict",
                "This conversation is already linked to another Lead; reviewed relinking is required.",
                existing_lead_id=str(existing.lead_id),
            )
        return ConversationLeadLinkOutcome(
            existing.id,
            existing.conversation_id,
            existing.lead_id,
            existing.party_id,
            True,
        )

    row = InboxConversationLeadLink(
        id=uuid5(command.context.command_id, "conversation-lead-link"),
        conversation_id=conversation.id,
        lead_id=lead.id,
        party_id=command.party_id,
        link_source=command.source.value,
        link_reason=reason,
        linked_by_person_id=command.actor_person_id,
        command_id=command.context.command_id,
        is_active=True,
    )
    db.add(row)
    emit_event(
        db,
        EventType.custom,
        {
            "name": "team_inbox.conversation_lead_linked.v1",
            "conversation_id": str(conversation.id),
            "lead_id": str(lead.id),
            "party_id": str(command.party_id),
            "link_source": command.source.value,
        },
        actor=command.context.actor,
    )
    stage_audit_event(
        db,
        action="inbox.conversation_lead_linked",
        entity_type="inbox_conversation_lead_link",
        entity_id=str(row.id),
        actor_id=str(command.actor_person_id) if command.actor_person_id else None,
        request_id=str(command.context.command_id),
        metadata={
            "conversation_id": str(conversation.id),
            "lead_id": str(lead.id),
            "party_id": str(command.party_id),
            "link_source": command.source.value,
        },
    )
    db.flush()
    return ConversationLeadLinkOutcome(
        row.id, conversation.id, lead.id, command.party_id, False
    )
