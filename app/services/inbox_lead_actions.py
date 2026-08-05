"""Typed server-side resolver and coordinator for Inbox Party/Lead actions."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.lead_intake import LeadIntakeInvitation
from app.models.party import Party
from app.models.sales import (
    Lead,
    LeadCaptureMethod,
    LeadSourcePlatform,
    LeadStatus,
    Pipeline,
    PipelineStage,
)
from app.models.team_inbox import InboxConversation
from app.services import conversation_lead_relationships, team_inbox_contact_links
from app.services.audit_adapter import stage_audit_event
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.sales import lifecycle

OWNER = "communications.inbox_lead_actions"
_ACTION_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="identity-aware Inbox profile and Lead action resolution",
    name="execute_inbox_lead_action",
)
_OPEN_STATUSES = tuple(
    status.value
    for status in LeadStatus
    if status not in {LeadStatus.won, LeadStatus.lost}
)


class InboxActionIntent(StrEnum):
    profile = "profile"
    lead = "lead"


class InboxResolvedActionType(StrEnum):
    edit_party_profile = "edit_party_profile"
    view_party_profile = "view_party_profile"
    view_lead = "view_lead"
    edit_lead = "edit_lead"
    select_pipeline = "select_pipeline"
    select_lead = "select_lead"
    create_lead_for_party = "create_lead_for_party"
    create_party_and_lead = "create_party_and_lead"
    identity_review_required = "identity_review_required"
    unauthorized = "unauthorized"
    unavailable = "unavailable"


class InboxLeadActionError(DomainError):
    pass


@dataclass(frozen=True, slots=True)
class InboxActionPermissions:
    can_read_profile: bool
    can_edit_profile: bool
    can_read_leads: bool
    can_write_leads: bool


@dataclass(frozen=True, slots=True)
class PipelineOption:
    id: UUID
    name: str


@dataclass(frozen=True, slots=True)
class LeadOption:
    id: UUID
    title: str
    status: str
    pipeline_id: UUID | None
    pipeline_name: str | None
    stage_name: str | None
    owner_person_id: UUID | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class InboxResolvedAction:
    intent: InboxActionIntent
    action_type: InboxResolvedActionType
    label: str
    reason: str | None
    conversation_id: UUID
    party_id: UUID | None = None
    subscriber_id: UUID | None = None
    lead_id: UUID | None = None
    pipeline_id: UUID | None = None
    destination: str | None = None
    pipelines: tuple[PipelineOption, ...] = ()
    leads: tuple[LeadOption, ...] = ()
    requires_link: bool = False


@dataclass(frozen=True, slots=True)
class LinkExistingLeadCommand:
    context: CommandContext
    conversation_id: UUID
    party_id: UUID
    lead_id: UUID
    actor_person_id: UUID | None
    source: conversation_lead_relationships.ConversationLeadLinkSource


@dataclass(frozen=True, slots=True)
class CreateLeadForPartyCommand:
    context: CommandContext
    conversation_id: UUID
    party_id: UUID
    pipeline_id: UUID
    stage_id: UUID | None
    actor_system_user_id: UUID
    actor_person_id: UUID | None
    title: str


@dataclass(frozen=True, slots=True)
class InboxLeadActionOutcome:
    conversation_id: UUID
    party_id: UUID
    lead_id: UUID
    link_id: UUID
    created: bool
    replayed: bool


def _error(suffix: str, message: str, **details: object) -> DomainError:
    return InboxLeadActionError(
        code=f"communications.inbox_lead_actions.{suffix}",
        message=message,
        details=details,
    )


def _pipeline_options(db: Session) -> tuple[PipelineOption, ...]:
    return tuple(
        PipelineOption(row.id, row.name)
        for row in db.scalars(
            select(Pipeline)
            .where(Pipeline.is_active.is_(True))
            .order_by(Pipeline.name, Pipeline.id)
        ).all()
    )


def _lead_options(db: Session, leads: tuple[Lead, ...]) -> tuple[LeadOption, ...]:
    pipeline_ids = {lead.pipeline_id for lead in leads if lead.pipeline_id is not None}
    stage_ids = {lead.stage_id for lead in leads if lead.stage_id is not None}
    pipelines = (
        {
            row.id: row.name
            for row in db.scalars(
                select(Pipeline).where(Pipeline.id.in_(pipeline_ids))
            ).all()
        }
        if pipeline_ids
        else {}
    )
    stages = (
        {
            row.id: row.name
            for row in db.scalars(
                select(PipelineStage).where(PipelineStage.id.in_(stage_ids))
            ).all()
        }
        if stage_ids
        else {}
    )
    return tuple(
        LeadOption(
            id=lead.id,
            title=lead.title or "Untitled Lead",
            status=lead.status,
            pipeline_id=lead.pipeline_id,
            pipeline_name=(
                pipelines.get(lead.pipeline_id)
                if lead.pipeline_id is not None
                else None
            ),
            stage_name=stages.get(lead.stage_id) if lead.stage_id is not None else None,
            owner_person_id=lead.owner_agent_id,
            updated_at=lead.updated_at,
        )
        for lead in leads
    )


def _exact_identity(
    db: Session, conversation: InboxConversation
) -> tuple[UUID | None, UUID | None, bool]:
    subscriber_id = conversation.subscriber_id
    party_ids = conversation_lead_relationships.exact_party_ids(db, conversation)
    return (
        party_ids[0] if len(party_ids) == 1 else None,
        subscriber_id,
        len(party_ids) > 1,
    )


def _structural_lead_id(db: Session, conversation_id: UUID) -> UUID | None:
    direct = conversation_lead_relationships.active_link(db, conversation_id)
    if direct is not None:
        return direct.lead_id
    completed = db.scalar(
        select(LeadIntakeInvitation)
        .where(
            LeadIntakeInvitation.conversation_id == conversation_id,
            LeadIntakeInvitation.status == "completed",
            LeadIntakeInvitation.lead_id.is_not(None),
        )
        .order_by(LeadIntakeInvitation.completed_at.desc())
        .limit(1)
    )
    return completed.lead_id if completed is not None else None


def resolve_action(
    db: Session,
    *,
    conversation_id: UUID,
    intent: InboxActionIntent,
    permissions: InboxActionPermissions,
    selected_pipeline_id: UUID | None = None,
) -> InboxResolvedAction:
    """Resolve presentation and routing without writing business state."""

    conversation = db.get(InboxConversation, conversation_id)
    if conversation is None or not conversation.is_active:
        return InboxResolvedAction(
            intent,
            InboxResolvedActionType.unavailable,
            "Unavailable",
            "Conversation context is unavailable.",
            conversation_id,
        )
    party_id, subscriber_id, ambiguous_exact = _exact_identity(db, conversation)
    candidates = team_inbox_contact_links.contact_link_candidates(
        db, [str(conversation.contact_address or "")]
    )
    potential_matches = bool(
        candidates.get("subscribers") or candidates.get("resellers")
    )

    if intent == InboxActionIntent.profile:
        if ambiguous_exact or (party_id is None and potential_matches):
            return InboxResolvedAction(
                intent,
                InboxResolvedActionType.identity_review_required,
                "Review Identity",
                "Potential or conflicting identities require reviewed selection.",
                conversation_id,
            )
        if subscriber_id is not None:
            if permissions.can_edit_profile:
                return InboxResolvedAction(
                    intent,
                    InboxResolvedActionType.edit_party_profile,
                    "Complete Profile",
                    None,
                    conversation_id,
                    party_id=party_id,
                    subscriber_id=subscriber_id,
                    destination=(
                        f"/admin/customers/person/{subscriber_id}/edit"
                        f"?inbox_conversation_id={conversation_id}"
                    ),
                )
            if permissions.can_read_profile:
                return InboxResolvedAction(
                    intent,
                    InboxResolvedActionType.view_party_profile,
                    "View Profile",
                    None,
                    conversation_id,
                    party_id=party_id,
                    subscriber_id=subscriber_id,
                    destination=f"/admin/customers/person/{subscriber_id}",
                )
            return InboxResolvedAction(
                intent,
                InboxResolvedActionType.unauthorized,
                "Profile Restricted",
                None,
                conversation_id,
            )
        if party_id is not None:
            return InboxResolvedAction(
                intent,
                InboxResolvedActionType.unavailable,
                "Profile Unavailable",
                "The Party has no supported profile editor yet.",
                conversation_id,
                party_id=party_id,
            )
        if not permissions.can_write_leads:
            return InboxResolvedAction(
                intent,
                InboxResolvedActionType.unauthorized,
                "Profile Restricted",
                None,
                conversation_id,
            )
        return InboxResolvedAction(
            intent,
            InboxResolvedActionType.create_party_and_lead,
            "Create Profile & Lead",
            "No authoritative Party is linked. Submitted observations remain unverified.",
            conversation_id,
            destination=f"/admin/sales/leads/new?inbox_conversation_id={conversation_id}",
        )

    structural_lead_id = _structural_lead_id(db, conversation_id)
    if structural_lead_id is not None:
        if not permissions.can_read_leads:
            return InboxResolvedAction(
                intent,
                InboxResolvedActionType.unauthorized,
                "Lead Restricted",
                None,
                conversation_id,
            )
        action_type = (
            InboxResolvedActionType.edit_lead
            if permissions.can_write_leads
            else InboxResolvedActionType.view_lead
        )
        suffix = "/edit" if action_type == InboxResolvedActionType.edit_lead else ""
        return InboxResolvedAction(
            intent,
            action_type,
            "Edit Lead" if suffix else "Open Lead",
            None,
            conversation_id,
            party_id=party_id,
            subscriber_id=subscriber_id,
            lead_id=structural_lead_id,
            destination=(
                f"/admin/sales/leads/{structural_lead_id}{suffix}"
                f"?inbox_conversation_id={conversation_id}"
            ),
        )
    if ambiguous_exact or (party_id is None and potential_matches):
        return InboxResolvedAction(
            intent,
            InboxResolvedActionType.identity_review_required,
            "Review Identity",
            "Potential or conflicting identities require reviewed selection.",
            conversation_id,
        )
    if party_id is None:
        if not permissions.can_write_leads:
            return InboxResolvedAction(
                intent,
                InboxResolvedActionType.unauthorized,
                "Lead Restricted",
                None,
                conversation_id,
            )
        return InboxResolvedAction(
            intent,
            InboxResolvedActionType.create_party_and_lead,
            "Create Profile & Lead",
            "No authoritative Party is linked. Submitted observations remain unverified.",
            conversation_id,
            destination=f"/admin/sales/leads/new?inbox_conversation_id={conversation_id}",
        )
    if not permissions.can_read_leads:
        return InboxResolvedAction(
            intent,
            InboxResolvedActionType.unauthorized,
            "Lead Restricted",
            None,
            conversation_id,
            party_id=party_id,
        )

    pipelines = _pipeline_options(db)
    if selected_pipeline_id is None:
        return InboxResolvedAction(
            intent,
            InboxResolvedActionType.select_pipeline,
            "Select Pipeline",
            "Choose an authoritative active pipeline before resolving Party Leads.",
            conversation_id,
            party_id=party_id,
            subscriber_id=subscriber_id,
            pipelines=pipelines,
        )
    pipeline = db.get(Pipeline, selected_pipeline_id)
    if pipeline is None or not pipeline.is_active:
        return InboxResolvedAction(
            intent,
            InboxResolvedActionType.unavailable,
            "Pipeline Unavailable",
            "The selected pipeline is not active.",
            conversation_id,
            party_id=party_id,
            pipelines=pipelines,
        )
    leads = tuple(
        db.scalars(
            select(Lead)
            .where(
                Lead.party_id == party_id,
                Lead.pipeline_id == pipeline.id,
                Lead.is_active.is_(True),
                Lead.status.in_(_OPEN_STATUSES),
            )
            .order_by(Lead.updated_at.desc(), Lead.id)
        ).all()
    )
    options = _lead_options(db, leads)
    if len(leads) > 1:
        return InboxResolvedAction(
            intent,
            InboxResolvedActionType.select_lead,
            "Select Lead",
            "Multiple eligible Leads require explicit selection.",
            conversation_id,
            party_id=party_id,
            subscriber_id=subscriber_id,
            pipeline_id=pipeline.id,
            pipelines=pipelines,
            leads=options,
        )
    if len(leads) == 1:
        lead = leads[0]
        return InboxResolvedAction(
            intent,
            InboxResolvedActionType.edit_lead
            if permissions.can_write_leads
            else InboxResolvedActionType.view_lead,
            "Open Lead",
            "The exact Party has one eligible Lead in this pipeline.",
            conversation_id,
            party_id=party_id,
            subscriber_id=subscriber_id,
            lead_id=lead.id,
            pipeline_id=pipeline.id,
            destination=f"/admin/sales/leads/{lead.id}",
            pipelines=pipelines,
            leads=options,
            requires_link=True,
        )
    if not permissions.can_write_leads:
        return InboxResolvedAction(
            intent,
            InboxResolvedActionType.unauthorized,
            "Lead Creation Restricted",
            None,
            conversation_id,
            party_id=party_id,
            pipeline_id=pipeline.id,
        )
    return InboxResolvedAction(
        intent,
        InboxResolvedActionType.create_lead_for_party,
        "Create Lead",
        "No active Lead exists for this Party in the selected pipeline.",
        conversation_id,
        party_id=party_id,
        subscriber_id=subscriber_id,
        pipeline_id=pipeline.id,
        pipelines=pipelines,
    )


def link_existing_lead(
    db: Session, command: LinkExistingLeadCommand
) -> InboxLeadActionOutcome:
    def operation() -> InboxLeadActionOutcome:
        conversation = db.scalar(
            select(InboxConversation)
            .where(InboxConversation.id == command.conversation_id)
            .with_for_update()
        )
        if conversation is None or not conversation.is_active:
            raise _error("conversation_not_found", "Inbox conversation was not found.")
        conversation_lead_relationships.require_exact_party(
            db, conversation, command.party_id
        )
        result = conversation_lead_relationships.link_conversation_lead_participant(
            db,
            conversation_lead_relationships.ConversationLeadLinkCommand(
                context=command.context,
                conversation_id=conversation.id,
                lead_id=command.lead_id,
                party_id=command.party_id,
                actor_person_id=command.actor_person_id,
                source=command.source,
                reason="Authorized Inbox operator selected or reused this exact Party Lead",
            ),
        )
        return InboxLeadActionOutcome(
            conversation.id,
            command.party_id,
            command.lead_id,
            result.link_id,
            False,
            result.replayed,
        )

    return execute_owner_command(
        db,
        definition=_ACTION_COMMAND,
        context=command.context,
        operation=operation,
    )


def create_lead_for_party(
    db: Session, command: CreateLeadForPartyCommand
) -> InboxLeadActionOutcome:
    def operation() -> InboxLeadActionOutcome:
        conversation = db.scalar(
            select(InboxConversation)
            .where(InboxConversation.id == command.conversation_id)
            .with_for_update()
        )
        if conversation is None or not conversation.is_active:
            raise _error("conversation_not_found", "Inbox conversation was not found.")
        conversation_lead_relationships.require_exact_party(
            db, conversation, command.party_id
        )
        db.execute(
            select(Party.id).where(Party.id == command.party_id).with_for_update()
        ).scalar_one()
        pipeline = db.scalar(
            select(Pipeline).where(Pipeline.id == command.pipeline_id).with_for_update()
        )
        if pipeline is None or not pipeline.is_active:
            raise _error("pipeline_unavailable", "The selected pipeline is not active.")
        stage = None
        if command.stage_id is not None:
            stage = db.get(PipelineStage, command.stage_id)
            if stage is None or not stage.is_active or stage.pipeline_id != pipeline.id:
                raise _error(
                    "stage_pipeline_mismatch",
                    "Select an active stage from the chosen pipeline.",
                )
        existing = tuple(
            db.scalars(
                select(Lead)
                .where(
                    Lead.party_id == command.party_id,
                    Lead.pipeline_id == pipeline.id,
                    Lead.is_active.is_(True),
                    Lead.status.in_(_OPEN_STATUSES),
                )
                .order_by(Lead.updated_at.desc(), Lead.id)
                .with_for_update()
            ).all()
        )
        if len(existing) > 1:
            raise _error(
                "lead_selection_required",
                "Multiple eligible Leads require explicit selection.",
            )
        created = not existing
        if existing:
            lead = existing[0]
        else:
            lead_id = uuid5(command.conversation_id, f"inbox-party-lead:{pipeline.id}")
            fingerprint = hashlib.sha256(
                f"{command.conversation_id}:{command.party_id}:{pipeline.id}".encode()
            ).hexdigest()
            source = {
                "email": "Email",
                "whatsapp": "Whatsapp",
                "facebook_messenger": "Facebook",
                "instagram_dm": "Instagram",
            }.get(conversation.channel_type, "Website")
            lead = lifecycle.create_party_lead(
                db,
                lead_id=lead_id,
                party_id=command.party_id,
                title=" ".join(command.title.split()) or "Inbox prospect",
                lead_source=source,
                binding_source=OWNER,
                binding_reason="Existing authoritative Inbox Party reused for Lead creation",
                origin_capture={
                    "capture_method": LeadCaptureMethod.agent_declared.value,
                    "source_platform": LeadSourcePlatform.team_inbox.value,
                    "source_interaction_id": f"inbox-conversation:{conversation.id}",
                    "capture_fingerprint": fingerprint,
                    "capture_source": OWNER,
                    "capture_reason": "Authorized operator created a Lead from an exact Inbox Party relationship",
                },
                metadata={"origin_conversation_id": str(conversation.id)},
                owner_agent_id=command.actor_system_user_id,
                pipeline_id=pipeline.id,
                stage_id=stage.id if stage else None,
            )
            emit_event(
                db,
                EventType.lead_created,
                {
                    "lead_id": str(lead.id),
                    "party_id": str(command.party_id),
                    "origin_conversation_id": str(conversation.id),
                },
                actor=command.context.actor,
            )
            stage_audit_event(
                db,
                action="inbox.lead_created_for_party",
                entity_type="lead",
                entity_id=str(lead.id),
                actor_id=str(command.actor_system_user_id),
                request_id=str(command.context.command_id),
                metadata={
                    "party_id": str(command.party_id),
                    "conversation_id": str(conversation.id),
                    "pipeline_id": str(pipeline.id),
                },
            )
        link = conversation_lead_relationships.link_conversation_lead_participant(
            db,
            conversation_lead_relationships.ConversationLeadLinkCommand(
                context=command.context,
                conversation_id=conversation.id,
                lead_id=lead.id,
                party_id=command.party_id,
                actor_person_id=command.actor_person_id,
                source=(
                    conversation_lead_relationships.ConversationLeadLinkSource.inbox_lead_authoring
                    if created
                    else conversation_lead_relationships.ConversationLeadLinkSource.exact_party_lead
                ),
                reason="Inbox Lead workflow reused the exact Party and selected pipeline",
            ),
        )
        return InboxLeadActionOutcome(
            conversation.id,
            command.party_id,
            lead.id,
            link.link_id,
            created,
            link.replayed,
        )

    return execute_owner_command(
        db,
        definition=_ACTION_COMMAND,
        context=command.context,
        operation=operation,
    )
