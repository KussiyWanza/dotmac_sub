"""Truthful, permission-scoped customer context projection for Team Inbox."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Generic, TypeVar
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.models.lead_intake import LeadIntakeInvitation
from app.models.party import Party, PartyContactPoint
from app.models.project import Project, ProjectTask
from app.models.sales import Lead, LeadStatus
from app.models.subscriber import Reseller, Subscriber
from app.models.support import Ticket
from app.models.team_inbox import (
    InboxContactLink,
    InboxConversation,
    InboxConversationParticipant,
    InboxParticipantAdmissionSource,
)
from app.services import (
    conversation_lead_relationships,
    inbox_lead_actions,
    projects,
    support,
)

logger = logging.getLogger(__name__)
T = TypeVar("T")


class ContextAvailability(StrEnum):
    available = "available"
    empty = "empty"
    unavailable = "unavailable"
    not_calculated = "not_calculated"
    not_applicable = "not_applicable"
    restricted = "restricted"


class InboxIdentityState(StrEnum):
    linked_party = "linked_party"
    identity_review_required = "identity_review_required"
    unresolved = "unresolved"
    unavailable = "unavailable"


class ConversationHistoryMatchKind(StrEnum):
    subscriber = "subscriber"
    party = "party"
    reseller = "reseller"
    exact_endpoint = "exact_endpoint"
    ambiguous = "ambiguous"
    unavailable = "unavailable"


@dataclass(frozen=True, slots=True)
class ConversationHistoryScope:
    kind: ConversationHistoryMatchKind
    subscriber_id: UUID | None = None
    party_id: UUID | None = None
    reseller_id: UUID | None = None
    channel_type: str | None = None
    normalized_endpoint: str | None = None
    provider_account_scope: str | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ContextSection(Generic[T]):
    availability: ContextAvailability
    items: tuple[T, ...] = ()
    total_count: int | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True)
class PartyProfileSummary:
    party_id: UUID
    display_name: str
    status: str
    email: str | None
    phone: str | None
    subscriber_id: UUID | None
    subscriber_url: str | None


@dataclass(frozen=True, slots=True)
class LeadSummary:
    id: UUID
    title: str
    status: str
    pipeline_name: str | None
    stage_name: str | None
    is_conversation_lead: bool
    url: str


@dataclass(frozen=True, slots=True)
class TicketSummary:
    id: UUID
    number: str | None
    title: str
    status: str
    priority: str
    updated_at: datetime
    issued_from_conversation: bool
    url: str


@dataclass(frozen=True, slots=True)
class ConversationSummary:
    id: UUID
    subject: str
    channel_type: str
    status: str
    last_message_at: datetime | None
    contact_address: str | None
    url: str


@dataclass(frozen=True, slots=True)
class ProjectSummary:
    id: UUID
    reference: str
    name: str
    status: str
    updated_at: datetime
    url: str


@dataclass(frozen=True, slots=True)
class ProjectTaskSummary:
    id: UUID
    project_id: UUID
    reference: str
    title: str
    status: str
    updated_at: datetime
    url: str


@dataclass(frozen=True, slots=True)
class InboxContactContextPermissions:
    can_read_profile: bool
    can_edit_profile: bool
    can_read_leads: bool
    can_write_leads: bool
    can_read_tickets: bool
    can_read_projects: bool
    can_read_project_tasks: bool

    def action_permissions(self) -> inbox_lead_actions.InboxActionPermissions:
        return inbox_lead_actions.InboxActionPermissions(
            can_read_profile=self.can_read_profile,
            can_edit_profile=self.can_edit_profile,
            can_read_leads=self.can_read_leads,
            can_write_leads=self.can_write_leads,
        )


@dataclass(frozen=True, slots=True)
class InboxContactContext:
    conversation_id: UUID
    observed_at: datetime
    identity_state: InboxIdentityState
    party_id: UUID | None
    subscriber_id: UUID | None
    conversation_history_scope: ConversationHistoryScope
    profile: ContextSection[PartyProfileSummary]
    leads: ContextSection[LeadSummary]
    tickets: ContextSection[TicketSummary]
    recent_conversations: ContextSection[ConversationSummary]
    projects: ContextSection[ProjectSummary]
    project_tasks: ContextSection[ProjectTaskSummary]
    profile_action: inbox_lead_actions.InboxResolvedAction
    lead_action: inbox_lead_actions.InboxResolvedAction


def _not_applicable(message: str) -> ContextSection[T]:
    return ContextSection(ContextAvailability.not_applicable, message=message)


def _restricted() -> ContextSection[T]:
    return ContextSection(ContextAvailability.restricted, message="Restricted")


def _unavailable() -> ContextSection[T]:
    return ContextSection(ContextAvailability.unavailable, message="Unavailable")


def _identity(
    db: Session, conversation: InboxConversation
) -> tuple[InboxIdentityState, UUID | None, UUID | None]:
    party_ids = conversation_lead_relationships.exact_party_ids(db, conversation)
    if len(party_ids) == 1:
        return InboxIdentityState.linked_party, party_ids[0], conversation.subscriber_id
    if len(party_ids) > 1:
        return (
            InboxIdentityState.identity_review_required,
            None,
            conversation.subscriber_id,
        )
    if conversation.subscriber_id is not None:
        # A Subscriber without its reviewed Party projection is not a new
        # prospect and must never fall through to Party creation.
        return InboxIdentityState.unavailable, None, conversation.subscriber_id
    return InboxIdentityState.unresolved, None, None


def _profile(
    db: Session,
    *,
    party_id: UUID | None,
    subscriber_id: UUID | None,
    permitted: bool,
) -> ContextSection[PartyProfileSummary]:
    if not permitted:
        return _restricted()
    if party_id is None:
        return _not_applicable("No authoritative Party is linked.")
    party = db.get(Party, party_id)
    if party is None:
        return _unavailable()
    points = tuple(
        db.scalars(
            select(PartyContactPoint)
            .where(
                PartyContactPoint.party_id == party.id,
                PartyContactPoint.is_active.is_(True),
                PartyContactPoint.channel_type.in_(("email", "phone")),
            )
            .order_by(PartyContactPoint.is_primary.desc(), PartyContactPoint.created_at)
        ).all()
    )
    email = next(
        (
            point.display_value or point.normalized_value
            for point in points
            if point.channel_type == "email"
        ),
        None,
    )
    phone = next(
        (
            point.display_value or point.normalized_value
            for point in points
            if point.channel_type == "phone"
        ),
        None,
    )
    return ContextSection(
        ContextAvailability.available,
        items=(
            PartyProfileSummary(
                party.id,
                party.display_name,
                party.status,
                email,
                phone,
                subscriber_id,
                f"/admin/customers/person/{subscriber_id}"
                if subscriber_id is not None
                else None,
            ),
        ),
    )


def _leads(
    db: Session,
    *,
    conversation_id: UUID,
    party_id: UUID | None,
    permitted: bool,
) -> ContextSection[LeadSummary]:
    if not permitted:
        return _restricted()
    if party_id is None:
        return _not_applicable("Lead context requires an authoritative Party.")
    direct = conversation_lead_relationships.active_link(db, conversation_id)
    query = (
        select(Lead)
        .where(Lead.party_id == party_id)
        .order_by(Lead.is_active.desc(), Lead.updated_at.desc(), Lead.id)
    )
    rows = tuple(db.scalars(query.limit(5)).all())
    count = int(
        db.scalar(select(func.count(Lead.id)).where(Lead.party_id == party_id)) or 0
    )
    if count == 0:
        return ContextSection(
            ContextAvailability.empty,
            total_count=0,
            message="No Leads for this Party.",
        )
    pipeline_ids = {row.pipeline_id for row in rows if row.pipeline_id is not None}
    stage_ids = {row.stage_id for row in rows if row.stage_id is not None}
    from app.models.sales import Pipeline, PipelineStage

    pipeline_names = (
        {
            row.id: row.name
            for row in db.scalars(
                select(Pipeline).where(Pipeline.id.in_(pipeline_ids))
            ).all()
        }
        if pipeline_ids
        else {}
    )
    stage_names = (
        {
            row.id: row.name
            for row in db.scalars(
                select(PipelineStage).where(PipelineStage.id.in_(stage_ids))
            ).all()
        }
        if stage_ids
        else {}
    )
    return ContextSection(
        ContextAvailability.available,
        items=tuple(
            LeadSummary(
                row.id,
                row.title or "Untitled Lead",
                row.status,
                (
                    pipeline_names.get(row.pipeline_id)
                    if row.pipeline_id is not None
                    else None
                ),
                stage_names.get(row.stage_id) if row.stage_id is not None else None,
                direct is not None and direct.lead_id == row.id,
                f"/admin/sales/leads/{row.id}",
            )
            for row in rows
        ),
        total_count=count,
    )


def _tickets(
    db: Session,
    *,
    conversation_id: UUID,
    subscriber_id: UUID | None,
    permitted: bool,
) -> ContextSection[TicketSummary]:
    if not permitted:
        return _restricted()
    scope = Ticket.origin_conversation_id == conversation_id
    if subscriber_id is not None:
        scope = or_(
            scope,
            Ticket.subscriber_id == subscriber_id,
            Ticket.customer_account_id == subscriber_id,
            Ticket.customer_person_id == subscriber_id,
        )
    query = select(Ticket).where(
        scope,
        Ticket.is_active.is_(True),
        Ticket.status.in_(support.active_ticket_status_values()),
    )
    count = int(db.scalar(select(func.count()).select_from(query.subquery())) or 0)
    if count == 0:
        return ContextSection(
            ContextAvailability.empty,
            total_count=0,
            message="No active Tickets.",
        )
    rows = tuple(
        db.scalars(query.order_by(Ticket.updated_at.desc(), Ticket.id).limit(5)).all()
    )
    return ContextSection(
        ContextAvailability.available,
        items=tuple(
            TicketSummary(
                row.id,
                row.number,
                row.title,
                row.status,
                row.priority,
                row.updated_at,
                row.origin_conversation_id == conversation_id,
                f"/admin/support/tickets/{row.id}",
            )
            for row in rows
        ),
        total_count=count,
    )


def _uuid_or_none(value: object) -> UUID | None:
    try:
        return UUID(str(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def _contact_resolution(conversation: InboxConversation) -> dict[str, object]:
    metadata = (
        conversation.metadata_ if isinstance(conversation.metadata_, dict) else {}
    )
    value = metadata.get("contact_resolution")
    return dict(value) if isinstance(value, dict) else {}


def _conversation_history_scope(
    db: Session,
    *,
    conversation: InboxConversation,
    identity_state: InboxIdentityState,
    party_id: UUID | None,
    subscriber_id: UUID | None,
) -> ConversationHistoryScope:
    if subscriber_id is not None:
        return ConversationHistoryScope(
            ConversationHistoryMatchKind.subscriber,
            subscriber_id=subscriber_id,
            reason="Exact reviewed Subscriber relationship.",
        )
    if identity_state is InboxIdentityState.identity_review_required:
        return ConversationHistoryScope(
            ConversationHistoryMatchKind.ambiguous,
            reason=(
                "Several reviewed Party identities participate in this conversation."
            ),
        )
    if party_id is not None:
        return ConversationHistoryScope(
            ConversationHistoryMatchKind.party,
            party_id=party_id,
            reason="Exact reviewed Party relationship.",
        )

    resolution = _contact_resolution(conversation)
    normalized_endpoint = str(
        resolution.get("normalized_contact") or conversation.contact_address or ""
    ).strip()
    active_link = None
    if normalized_endpoint and conversation.channel_type:
        active_link = (
            db.query(InboxContactLink)
            .filter(InboxContactLink.channel_type == conversation.channel_type)
            .filter(InboxContactLink.normalized_contact == normalized_endpoint)
            .filter(InboxContactLink.is_active.is_(True))
            .one_or_none()
        )
    if active_link is not None:
        if active_link.subscriber_id is not None:
            return ConversationHistoryScope(
                ConversationHistoryMatchKind.subscriber,
                subscriber_id=active_link.subscriber_id,
                reason="Reviewed Inbox endpoint-to-Subscriber link.",
            )
        if active_link.party_contact_point_id is not None:
            point = db.get(PartyContactPoint, active_link.party_contact_point_id)
            if point is not None and point.is_active:
                return ConversationHistoryScope(
                    ConversationHistoryMatchKind.party,
                    party_id=point.party_id,
                    reason="Reviewed canonical Party contact-point link.",
                )
        if active_link.reseller_id is not None:
            reseller = db.get(Reseller, active_link.reseller_id)
            if reseller is not None and reseller.party_id is not None:
                return ConversationHistoryScope(
                    ConversationHistoryMatchKind.party,
                    party_id=reseller.party_id,
                    reason="Reviewed reseller Party relationship.",
                )
            return ConversationHistoryScope(
                ConversationHistoryMatchKind.reseller,
                reseller_id=active_link.reseller_id,
                reason="Reviewed Inbox endpoint-to-Reseller link.",
            )

    if str(resolution.get("status") or "") == "ambiguous":
        return ConversationHistoryScope(
            ConversationHistoryMatchKind.ambiguous,
            reason="The conversation endpoint matches more than one identity.",
        )

    reseller_id = _uuid_or_none(resolution.get("reseller_id"))
    if str(resolution.get("status") or "") == "linked_reseller" and reseller_id:
        reseller = db.get(Reseller, reseller_id)
        if reseller is not None and reseller.party_id is not None:
            return ConversationHistoryScope(
                ConversationHistoryMatchKind.party,
                party_id=reseller.party_id,
                reason="Exact resolved reseller Party relationship.",
            )
        return ConversationHistoryScope(
            ConversationHistoryMatchKind.reseller,
            reseller_id=reseller_id,
            reason="Exact resolved Reseller relationship.",
        )

    if not normalized_endpoint or not conversation.channel_type:
        return ConversationHistoryScope(
            ConversationHistoryMatchKind.unavailable,
            reason="The conversation has no exact external endpoint.",
        )
    participant_scopes = tuple(
        db.scalars(
            select(InboxConversationParticipant.provider_account_scope)
            .where(
                InboxConversationParticipant.conversation_id == conversation.id,
                InboxConversationParticipant.channel_type == conversation.channel_type,
                InboxConversationParticipant.normalized_endpoint == normalized_endpoint,
                InboxConversationParticipant.admission_source
                == InboxParticipantAdmissionSource.inbound_from.value,
                InboxConversationParticipant.is_active.is_(True),
            )
            .distinct()
            .order_by(InboxConversationParticipant.provider_account_scope)
        ).all()
    )
    if len(participant_scopes) > 1 and conversation.channel_type in {
        "facebook_messenger",
        "instagram_dm",
    }:
        return ConversationHistoryScope(
            ConversationHistoryMatchKind.ambiguous,
            reason="The endpoint appears under several provider account scopes.",
        )
    return ConversationHistoryScope(
        ConversationHistoryMatchKind.exact_endpoint,
        channel_type=conversation.channel_type,
        normalized_endpoint=normalized_endpoint,
        provider_account_scope=participant_scopes[0]
        if participant_scopes
        else "default",
        reason="Exact normalized endpoint; no broader identity was inferred.",
    )


def _history_filter(
    scope: ConversationHistoryScope,
) -> ColumnElement[bool]:
    if scope.kind is ConversationHistoryMatchKind.subscriber:
        assert scope.subscriber_id is not None
        return InboxConversation.subscriber_id == scope.subscriber_id
    if scope.kind is ConversationHistoryMatchKind.party:
        assert scope.party_id is not None
        subscriber_ids = select(Subscriber.id).where(
            Subscriber.party_id == scope.party_id
        )
        participant_conversation_ids = (
            select(InboxConversationParticipant.conversation_id)
            .join(
                PartyContactPoint,
                PartyContactPoint.id
                == InboxConversationParticipant.party_contact_point_id,
            )
            .where(
                PartyContactPoint.party_id == scope.party_id,
                PartyContactPoint.is_active.is_(True),
                InboxConversationParticipant.is_active.is_(True),
            )
        )
        completed_intake_conversation_ids = select(
            LeadIntakeInvitation.conversation_id
        ).where(
            LeadIntakeInvitation.party_id == scope.party_id,
            LeadIntakeInvitation.status == "completed",
        )
        canonical_point_ids = select(PartyContactPoint.id).where(
            PartyContactPoint.party_id == scope.party_id,
            PartyContactPoint.is_active.is_(True),
        )
        reseller_ids = select(Reseller.id).where(Reseller.party_id == scope.party_id)
        reviewed_scalar_link = (
            select(InboxContactLink.id)
            .where(
                InboxContactLink.channel_type == InboxConversation.channel_type,
                InboxContactLink.normalized_contact
                == InboxConversation.contact_address,
                InboxContactLink.party_contact_point_id.in_(canonical_point_ids),
                InboxContactLink.is_active.is_(True),
            )
            .exists()
        )
        reviewed_reseller_link = (
            select(InboxContactLink.id)
            .where(
                InboxContactLink.channel_type == InboxConversation.channel_type,
                InboxContactLink.normalized_contact
                == InboxConversation.contact_address,
                InboxContactLink.reseller_id.in_(reseller_ids),
                InboxContactLink.is_active.is_(True),
            )
            .exists()
        )
        return or_(
            InboxConversation.subscriber_id.in_(subscriber_ids),
            InboxConversation.id.in_(participant_conversation_ids),
            InboxConversation.id.in_(completed_intake_conversation_ids),
            reviewed_scalar_link,
            reviewed_reseller_link,
        )
    if scope.kind is ConversationHistoryMatchKind.reseller:
        assert scope.reseller_id is not None
        reviewed_link = (
            select(InboxContactLink.id)
            .where(
                InboxContactLink.channel_type == InboxConversation.channel_type,
                InboxContactLink.normalized_contact
                == InboxConversation.contact_address,
                InboxContactLink.reseller_id == scope.reseller_id,
                InboxContactLink.is_active.is_(True),
            )
            .exists()
        )
        recorded_reseller = InboxConversation.metadata_["contact_resolution"][
            "reseller_id"
        ].as_string() == str(scope.reseller_id)
        return or_(reviewed_link, recorded_reseller)
    if scope.kind is ConversationHistoryMatchKind.exact_endpoint:
        assert scope.channel_type is not None
        assert scope.normalized_endpoint is not None
        participant_conversation_ids = select(
            InboxConversationParticipant.conversation_id
        ).where(
            InboxConversationParticipant.channel_type == scope.channel_type,
            InboxConversationParticipant.normalized_endpoint
            == scope.normalized_endpoint,
            InboxConversationParticipant.provider_account_scope
            == (scope.provider_account_scope or "default"),
            InboxConversationParticipant.admission_source
            == InboxParticipantAdmissionSource.inbound_from.value,
            InboxConversationParticipant.is_active.is_(True),
        )
        participant_match = InboxConversation.id.in_(participant_conversation_ids)
        if scope.channel_type in {"facebook_messenger", "instagram_dm"}:
            return participant_match
        return or_(
            participant_match,
            and_(
                InboxConversation.channel_type == scope.channel_type,
                InboxConversation.contact_address == scope.normalized_endpoint,
            ),
        )
    raise ValueError(f"Unsupported conversation history scope: {scope.kind}")


def _recent_conversations(
    db: Session,
    *,
    conversation: InboxConversation,
    scope: ConversationHistoryScope,
    permitted: bool,
) -> ContextSection[ConversationSummary]:
    if not permitted:
        return _restricted()
    if scope.kind is ConversationHistoryMatchKind.ambiguous:
        return ContextSection(
            ContextAvailability.not_calculated,
            message=scope.reason or "Identity review is required.",
        )
    if scope.kind is ConversationHistoryMatchKind.unavailable:
        return _not_applicable(scope.reason or "Conversation identity is unavailable.")
    query = (
        db.query(InboxConversation)
        .filter(InboxConversation.is_active.is_(True))
        .filter(InboxConversation.id != conversation.id)
        .filter(_history_filter(scope))
    )
    count = query.count()
    rows = tuple(
        query.order_by(
            func.coalesce(
                InboxConversation.last_message_at, InboxConversation.created_at
            ).desc(),
            InboxConversation.id.asc(),
        )
        .limit(5)
        .all()
    )
    if count == 0:
        return ContextSection(
            ContextAvailability.empty,
            total_count=0,
            message="No previous conversations.",
        )
    return ContextSection(
        ContextAvailability.available,
        items=tuple(
            ConversationSummary(
                row.id,
                row.subject or row.contact_address or "Conversation",
                row.channel_type,
                row.status,
                row.last_message_at,
                row.contact_address,
                f"/admin/inbox?c={row.id}",
            )
            for row in rows
        ),
        total_count=count,
    )


def _projects(
    db: Session,
    *,
    subscriber_id: UUID | None,
    lead_ids: tuple[UUID, ...],
    permitted: bool,
) -> ContextSection[ProjectSummary]:
    if not permitted:
        return _restricted()
    scopes = []
    if subscriber_id is not None:
        scopes.append(Project.subscriber_id == subscriber_id)
    if lead_ids:
        scopes.append(Project.lead_id.in_(lead_ids))
    if not scopes:
        return _not_applicable(
            "Project context requires exact customer or Lead identity."
        )
    query = select(Project).where(
        or_(*scopes),
        Project.is_active.is_(True),
        Project.status.in_(projects.active_project_status_values()),
    )
    count = int(db.scalar(select(func.count()).select_from(query.subquery())) or 0)
    if count == 0:
        return ContextSection(
            ContextAvailability.empty,
            total_count=0,
            message="No active Projects.",
        )
    rows = tuple(
        db.scalars(query.order_by(Project.updated_at.desc(), Project.id).limit(5)).all()
    )
    return ContextSection(
        ContextAvailability.available,
        items=tuple(
            ProjectSummary(
                row.id,
                row.number or row.code or str(row.id)[:8],
                row.name,
                row.status,
                row.updated_at,
                f"/admin/projects/{row.id}",
            )
            for row in rows
        ),
        total_count=count,
    )


def _project_tasks(
    db: Session,
    *,
    subscriber_id: UUID | None,
    lead_ids: tuple[UUID, ...],
    permitted: bool,
) -> ContextSection[ProjectTaskSummary]:
    if not permitted:
        return _restricted()
    scopes = []
    if subscriber_id is not None:
        scopes.append(Project.subscriber_id == subscriber_id)
    if lead_ids:
        scopes.append(Project.lead_id.in_(lead_ids))
    if not scopes:
        return _not_applicable("Task context requires exact customer or Lead identity.")
    query = (
        select(ProjectTask)
        .join(Project, Project.id == ProjectTask.project_id)
        .where(
            or_(*scopes),
            Project.is_active.is_(True),
            ProjectTask.is_active.is_(True),
            ProjectTask.status.in_(projects.active_project_task_status_values()),
        )
    )
    count = int(db.scalar(select(func.count()).select_from(query.subquery())) or 0)
    if count == 0:
        return ContextSection(
            ContextAvailability.empty,
            total_count=0,
            message="No active Project Tasks.",
        )
    rows = tuple(
        db.scalars(
            query.order_by(ProjectTask.updated_at.desc(), ProjectTask.id).limit(5)
        ).all()
    )
    return ContextSection(
        ContextAvailability.available,
        items=tuple(
            ProjectTaskSummary(
                row.id,
                row.project_id,
                row.number or str(row.id)[:8],
                row.title,
                row.status,
                row.updated_at,
                f"/admin/projects/tasks/{row.id}",
            )
            for row in rows
        ),
        total_count=count,
    )


def _safe_section(name: str, project):
    try:
        return project()
    except SQLAlchemyError:
        logger.exception(
            "inbox_contact_context_section_unavailable", extra={"section": name}
        )
        return _unavailable()


def build_contact_context(
    db: Session,
    *,
    conversation_id: UUID,
    permissions: InboxContactContextPermissions,
) -> InboxContactContext | None:
    conversation = db.get(InboxConversation, conversation_id)
    if conversation is None or not conversation.is_active:
        return None
    identity_state, party_id, subscriber_id = _identity(db, conversation)
    conversation_history_scope = _conversation_history_scope(
        db,
        conversation=conversation,
        identity_state=identity_state,
        party_id=party_id,
        subscriber_id=subscriber_id,
    )
    lead_ids = (
        tuple(
            db.scalars(
                select(Lead.id).where(
                    Lead.party_id == party_id,
                    Lead.is_active.is_(True),
                    Lead.status.not_in((LeadStatus.won.value, LeadStatus.lost.value)),
                )
            ).all()
        )
        if party_id is not None
        else ()
    )
    action_permissions = permissions.action_permissions()
    try:
        profile_action = inbox_lead_actions.resolve_action(
            db,
            conversation_id=conversation_id,
            intent=inbox_lead_actions.InboxActionIntent.profile,
            permissions=action_permissions,
        )
        lead_action = inbox_lead_actions.resolve_action(
            db,
            conversation_id=conversation_id,
            intent=inbox_lead_actions.InboxActionIntent.lead,
            permissions=action_permissions,
        )
    except SQLAlchemyError:
        logger.exception("inbox_contact_context_actions_unavailable")
        profile_action = inbox_lead_actions.InboxResolvedAction(
            inbox_lead_actions.InboxActionIntent.profile,
            inbox_lead_actions.InboxResolvedActionType.unavailable,
            "Unavailable",
            "Action resolver is unavailable.",
            conversation_id,
        )
        lead_action = inbox_lead_actions.InboxResolvedAction(
            inbox_lead_actions.InboxActionIntent.lead,
            inbox_lead_actions.InboxResolvedActionType.unavailable,
            "Unavailable",
            "Action resolver is unavailable.",
            conversation_id,
        )
    return InboxContactContext(
        conversation_id=conversation_id,
        observed_at=datetime.now(UTC),
        identity_state=identity_state,
        party_id=party_id,
        subscriber_id=subscriber_id,
        conversation_history_scope=conversation_history_scope,
        profile=_safe_section(
            "profile",
            lambda: _profile(
                db,
                party_id=party_id,
                subscriber_id=subscriber_id,
                permitted=permissions.can_read_profile,
            ),
        ),
        leads=_safe_section(
            "leads",
            lambda: _leads(
                db,
                conversation_id=conversation_id,
                party_id=party_id,
                permitted=permissions.can_read_leads,
            ),
        ),
        tickets=_safe_section(
            "tickets",
            lambda: _tickets(
                db,
                conversation_id=conversation_id,
                subscriber_id=subscriber_id,
                permitted=permissions.can_read_tickets,
            ),
        ),
        recent_conversations=_safe_section(
            "recent_conversations",
            lambda: _recent_conversations(
                db,
                conversation=conversation,
                scope=conversation_history_scope,
                permitted=permissions.can_read_tickets,
            ),
        ),
        projects=_safe_section(
            "projects",
            lambda: _projects(
                db,
                subscriber_id=subscriber_id,
                lead_ids=lead_ids,
                permitted=permissions.can_read_projects,
            ),
        ),
        project_tasks=_safe_section(
            "project_tasks",
            lambda: _project_tasks(
                db,
                subscriber_id=subscriber_id,
                lead_ids=lead_ids,
                permitted=permissions.can_read_project_tasks,
            ),
        ),
        profile_action=profile_action,
        lead_action=lead_action,
    )
