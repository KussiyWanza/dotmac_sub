"""Owner for versioned Inbox lead-intake forms, invitations, and submission."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from uuid import UUID, uuid5

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.ai_intake import AiIntakeConfig
from app.models.domain_settings import SettingDomain
from app.models.lead_intake import (
    LeadIntakeAssessment,
    LeadIntakeAssessmentDecision,
    LeadIntakeInvitation,
    LeadIntakeInvitationStatus,
    LeadIntakePartyType,
    LeadIntakeTemplate,
    LeadIntakeTemplateStatus,
)
from app.models.party import (
    PartyContactConsentStatus,
    PartyContactPointType,
    PartyContactVerificationStatus,
    PartyRelationshipType,
    PartyRoleStatus,
    PartyRoleType,
    PartyType,
)
from app.models.sales import (
    LeadCaptureMethod,
    LeadSourcePlatform,
    Pipeline,
    PipelineStage,
)
from app.models.service_team import ServiceTeam
from app.models.system_user import SystemUser
from app.models.team_inbox import InboxConversation, InboxMessage
from app.schemas.lead_intake import (
    AiLeadIntakeClassification,
    LeadIntakeSubmission,
    LeadIntakeTemplateDraft,
    ResolvedLeadIntakeAddress,
)
from app.services import party as party_service
from app.services import team_inbox_operations, team_inbox_participants
from app.services.audit_adapter import stage_audit_event
from app.services.domain_errors import DomainError
from app.services.events import EventType, emit_event
from app.services.ncc_subscriber_report import normalize_state
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.sales import lifecycle
from app.services.settings_spec import resolve_value

OWNER = "sales.lead_intake"
META_CHANNELS = frozenset({"whatsapp", "facebook_messenger", "instagram_dm"})
QUALIFYING_INTENTS = frozenset({"new_connection", "coverage_request"})
_TEMPLATE = OwnerCommandDefinition(
    owner=OWNER,
    concern="versioned lead-intake template lifecycle",
    name="mutate_lead_intake_template",
)
_ASSESS = OwnerCommandDefinition(
    owner=OWNER,
    concern="sales lead eligibility and invitation lifecycle",
    name="assess_inbound_lead_intake",
)
_INVITATION = OwnerCommandDefinition(
    owner=OWNER,
    concern="sales lead eligibility and invitation lifecycle",
    name="mutate_lead_intake_invitation",
)
_SUBMISSION = OwnerCommandDefinition(
    owner=OWNER,
    concern="atomic Inbox form to Party and Lead conversion",
    name="submit_lead_intake_form",
)


class LeadIntakeError(DomainError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        kind: str = "conflict",
        field: str | None = None,
    ) -> None:
        details: dict[str, object] = {"kind": kind}
        if field:
            details["field"] = field
        super().__init__(code=f"{OWNER}.{code}", message=message, details=details)
        self.kind = kind
        self.field = field


class TemplateAction(StrEnum):
    create = "create"
    update = "update"
    publish = "publish"
    retire = "retire"


@dataclass(frozen=True, slots=True)
class TemplateCommand:
    context: CommandContext
    action: TemplateAction
    actor_system_user_id: UUID
    template_id: UUID
    draft: LeadIntakeTemplateDraft | None = None


@dataclass(frozen=True, slots=True)
class TemplateOutcome:
    template_id: UUID
    party_type: LeadIntakePartyType
    version: int
    status: LeadIntakeTemplateStatus


@dataclass(frozen=True, slots=True)
class AssessInboundCommand:
    context: CommandContext
    conversation_id: UUID
    message_id: UUID
    classification: AiLeadIntakeClassification
    provider_label: str | None = None
    model_label: str | None = None


@dataclass(frozen=True, slots=True)
class ManualInvitationCommand:
    context: CommandContext
    conversation_id: UUID
    trigger_message_id: UUID
    party_type: LeadIntakePartyType
    actor_system_user_id: UUID


@dataclass(frozen=True, slots=True)
class InvitationOutcome:
    action: str
    conversation_id: UUID
    invitation_id: UUID | None = None
    token: str | None = None
    invitation_message: str | None = None
    clarification_question: str | None = None
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class InvitationDeliveryCommand:
    context: CommandContext
    invitation_id: UUID
    message_id: UUID | None
    delivery_status: str
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class RevokeInvitationCommand:
    context: CommandContext
    invitation_id: UUID
    conversation_id: UUID
    actor_system_user_id: UUID
    reason: str


@dataclass(frozen=True, slots=True)
class SubmitLeadIntakeCommand:
    context: CommandContext
    token: str
    submission: LeadIntakeSubmission
    resolved_address: ResolvedLeadIntakeAddress


@dataclass(frozen=True, slots=True)
class SubmitLeadIntakeOutcome:
    invitation_id: UUID
    conversation_id: UUID
    lead_id: UUID
    party_id: UUID
    thank_you_message: str
    confirmation_message: str
    replayed: bool


@dataclass(frozen=True, slots=True)
class PublicLeadIntakeForm:
    invitation_id: UUID
    party_type: LeadIntakePartyType
    heading: str
    introduction: str | None
    privacy_notice: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class LeadIntakeInvitationSummary:
    invitation_id: UUID
    party_type: LeadIntakePartyType
    effective_status: LeadIntakeInvitationStatus
    issued_at: datetime
    delivery_status: str
    lead_id: UUID | None
    can_revoke: bool


@dataclass(frozen=True, slots=True)
class LeadIntakeRolloutStatus:
    configured_enabled: bool
    templates_ready: bool
    automatic_sends_active: bool


@dataclass(frozen=True, slots=True)
class LeadIntakeTemplateFormOptions:
    teams: tuple[ServiceTeam, ...]
    owners: tuple[SystemUser, ...]
    pipelines: tuple[Pipeline, ...]
    stages: tuple[PipelineStage, ...]


def _error(
    code: str, message: str, *, kind: str = "conflict", field: str | None = None
) -> LeadIntakeError:
    return LeadIntakeError(code, message, kind=kind, field=field)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def token_hash(token: str) -> str:
    return hashlib.sha256(str(token or "").encode()).hexdigest()


def _clean(value: str | None, field: str, maximum: int) -> str:
    result = " ".join(str(value or "").split())
    if not result:
        raise _error(
            f"{field}_required",
            f"{field.replace('_', ' ').title()} is required.",
            kind="invalid",
            field=field,
        )
    if len(result) > maximum:
        raise _error(
            f"{field}_too_long",
            f"{field.replace('_', ' ').title()} is too long.",
            kind="invalid",
            field=field,
        )
    return result


def _active_actor(db: Session, actor_id: UUID) -> SystemUser:
    actor = db.scalars(
        select(SystemUser)
        .where(SystemUser.id == actor_id, SystemUser.is_active.is_(True))
        .with_for_update()
    ).one_or_none()
    if actor is None:
        raise _error(
            "actor_not_eligible", "An active staff user is required.", kind="forbidden"
        )
    return actor


def _validate_template_refs(db: Session, draft: LeadIntakeTemplateDraft) -> None:
    team = db.get(ServiceTeam, draft.target_service_team_id)
    if team is None or not team.is_active:
        raise _error(
            "team_not_active",
            "Select an active Sales service team.",
            kind="invalid",
            field="target_service_team_id",
        )
    if draft.owner_system_user_id:
        owner = db.get(SystemUser, draft.owner_system_user_id)
        if owner is None or not owner.is_active:
            raise _error(
                "owner_not_active",
                "Select an active Lead owner.",
                kind="invalid",
                field="owner_system_user_id",
            )
    if draft.pipeline_id:
        pipeline = db.get(Pipeline, draft.pipeline_id)
        stage = db.get(PipelineStage, draft.stage_id)
        if pipeline is None or not pipeline.is_active:
            raise _error(
                "pipeline_not_active",
                "Select an active Pipeline.",
                kind="invalid",
                field="pipeline_id",
            )
        if stage is None or not stage.is_active or stage.pipeline_id != pipeline.id:
            raise _error(
                "stage_pipeline_mismatch",
                "Select an active Stage in the chosen Pipeline.",
                kind="invalid",
                field="stage_id",
            )


def mutate_template(db: Session, command: TemplateCommand) -> TemplateOutcome:
    def operation() -> TemplateOutcome:
        _active_actor(db, command.actor_system_user_id)
        row = db.scalars(
            select(LeadIntakeTemplate)
            .where(LeadIntakeTemplate.id == command.template_id)
            .with_for_update()
        ).one_or_none()
        now = datetime.now(UTC)
        if command.action is TemplateAction.create:
            if row is not None:
                return TemplateOutcome(
                    row.id,
                    LeadIntakePartyType(row.party_type),
                    row.version,
                    LeadIntakeTemplateStatus(row.status),
                )
            if command.draft is None:
                raise _error(
                    "template_required", "Template values are required.", kind="invalid"
                )
            _validate_template_refs(db, command.draft)
            version = (
                int(
                    db.scalar(
                        select(func.max(LeadIntakeTemplate.version)).where(
                            LeadIntakeTemplate.party_type
                            == command.draft.party_type.value
                        )
                    )
                    or 0
                )
                + 1
            )
            row = LeadIntakeTemplate(
                id=command.template_id,
                party_type=command.draft.party_type.value,
                version=version,
                status="draft",
                created_by_system_user_id=command.actor_system_user_id,
            )
            db.add(row)
        elif row is None:
            raise _error(
                "template_not_found",
                "Lead intake template was not found.",
                kind="not_found",
            )
        assert row is not None
        if command.action in {TemplateAction.create, TemplateAction.update}:
            if row.status != "draft":
                raise _error(
                    "published_template_immutable",
                    "Published template versions cannot be edited.",
                )
            if command.draft is None:
                raise _error(
                    "template_required", "Template values are required.", kind="invalid"
                )
            if row.party_type != command.draft.party_type.value:
                raise _error(
                    "party_type_immutable",
                    "A template version cannot change party type.",
                )
            _validate_template_refs(db, command.draft)
            row.name = command.draft.name.strip()
            row.heading = command.draft.heading.strip()
            row.introduction = (command.draft.introduction or "").strip() or None
            row.privacy_notice = command.draft.privacy_notice.strip()
            row.invitation_message = command.draft.invitation_message.strip()
            row.confirmation_message = command.draft.confirmation_message.strip()
            row.thank_you_message = command.draft.thank_you_message.strip()
            row.target_service_team_id = command.draft.target_service_team_id
            row.owner_system_user_id = command.draft.owner_system_user_id
            row.pipeline_id = command.draft.pipeline_id
            row.stage_id = command.draft.stage_id
        elif command.action is TemplateAction.publish and row.status != "published":
            if row.status != "draft":
                raise _error(
                    "template_not_publishable",
                    "Only a draft template can be published.",
                )
            for current in db.scalars(
                select(LeadIntakeTemplate)
                .where(
                    LeadIntakeTemplate.party_type == row.party_type,
                    LeadIntakeTemplate.status == "published",
                )
                .with_for_update()
            ).all():
                current.status = "retired"
                current.retired_at = now
            row.status = "published"
            row.published_at = now
            row.retired_at = None
        elif command.action is TemplateAction.retire and row.status != "retired":
            if row.status != "published":
                raise _error(
                    "template_not_retirable",
                    "Only a published template can be retired.",
                )
            row.status = "retired"
            row.retired_at = now
        db.flush()
        stage_audit_event(
            db,
            action=f"lead_intake.template_{command.action.value}",
            entity_type="lead_intake_template",
            entity_id=str(row.id),
            actor_id=str(command.actor_system_user_id),
            request_id=str(command.context.command_id),
            metadata={
                "party_type": row.party_type,
                "version": row.version,
                "status": row.status,
            },
        )
        return TemplateOutcome(
            row.id,
            LeadIntakePartyType(row.party_type),
            row.version,
            LeadIntakeTemplateStatus(row.status),
        )

    return execute_owner_command(
        db, definition=_TEMPLATE, context=command.context, operation=operation
    )


def list_templates(db: Session) -> list[LeadIntakeTemplate]:
    return list(
        db.scalars(
            select(LeadIntakeTemplate).order_by(
                LeadIntakeTemplate.party_type, LeadIntakeTemplate.version.desc()
            )
        ).all()
    )


def get_template(db: Session, template_id: UUID) -> LeadIntakeTemplate | None:
    return db.get(LeadIntakeTemplate, template_id)


def template_form_options(db: Session) -> LeadIntakeTemplateFormOptions:
    return LeadIntakeTemplateFormOptions(
        teams=tuple(
            db.scalars(
                select(ServiceTeam)
                .where(ServiceTeam.is_active.is_(True))
                .order_by(ServiceTeam.name)
            ).all()
        ),
        owners=tuple(
            db.scalars(
                select(SystemUser)
                .where(SystemUser.is_active.is_(True))
                .order_by(SystemUser.email)
            ).all()
        ),
        pipelines=tuple(
            db.scalars(
                select(Pipeline)
                .where(Pipeline.is_active.is_(True))
                .order_by(Pipeline.name)
            ).all()
        ),
        stages=tuple(
            db.scalars(
                select(PipelineStage)
                .where(PipelineStage.is_active.is_(True))
                .order_by(PipelineStage.pipeline_id, PipelineStage.order_index)
            ).all()
        ),
    )


def _bool_setting(db: Session, key: str, default: bool) -> bool:
    value = resolve_value(db, SettingDomain.integration, key)
    if value is None:
        return default
    return (
        value
        if isinstance(value, bool)
        else str(value).strip().lower() in {"1", "true", "yes", "on"}
    )


def _ttl_hours(db: Session) -> int:
    value = resolve_value(
        db, SettingDomain.integration, "lead_intake_invitation_ttl_hours"
    )
    try:
        return min(24, max(1, int(value or 24)))
    except (TypeError, ValueError):
        return 24


def _ai_config(db: Session, channel_type: str) -> AiIntakeConfig | None:
    return db.scalars(
        select(AiIntakeConfig)
        .where(
            AiIntakeConfig.channel_type.in_((channel_type, "any")),
            AiIntakeConfig.is_enabled.is_(True),
        )
        .order_by((AiIntakeConfig.channel_type == channel_type).desc())
    ).first()


def ai_intake_enabled(db: Session, channel_type: str) -> bool:
    return (
        channel_type in META_CHANNELS
        and _bool_setting(db, "lead_intake_auto_send_enabled", False)
        and _ai_config(db, channel_type) is not None
    )


def _unknown_conversation(db: Session, conversation_id: UUID) -> InboxConversation:
    conversation = db.scalars(
        select(InboxConversation)
        .where(
            InboxConversation.id == conversation_id,
            InboxConversation.is_active.is_(True),
        )
        .with_for_update()
    ).one_or_none()
    if conversation is None:
        raise _error(
            "conversation_not_found",
            "Inbox conversation was not found.",
            kind="not_found",
        )
    resolution = dict((conversation.metadata_ or {}).get("contact_resolution") or {})
    if (
        conversation.subscriber_id is not None
        or resolution.get("status") != "unmatched"
    ):
        raise _error(
            "conversation_not_unknown", "Lead intake is limited to unknown prospects."
        )
    if conversation.channel_type not in META_CHANNELS:
        raise _error(
            "channel_not_supported",
            "Lead intake is limited to Meta messaging channels.",
        )
    return conversation


def _published_templates_ready(db: Session) -> bool:
    rows = set(
        db.scalars(
            select(LeadIntakeTemplate.party_type).where(
                LeadIntakeTemplate.status == "published"
            )
        ).all()
    )
    return rows == {"individual", "organization"}


def automatic_rollout_status(db: Session) -> LeadIntakeRolloutStatus:
    configured = _bool_setting(db, "lead_intake_auto_send_enabled", False)
    templates_ready = _published_templates_ready(db)
    return LeadIntakeRolloutStatus(
        configured_enabled=configured,
        templates_ready=templates_ready,
        automatic_sends_active=configured and templates_ready,
    )


def _published_template(
    db: Session, party_type: LeadIntakePartyType
) -> LeadIntakeTemplate:
    row = db.scalars(
        select(LeadIntakeTemplate).where(
            LeadIntakeTemplate.party_type == party_type.value,
            LeadIntakeTemplate.status == "published",
        )
    ).one_or_none()
    if row is None:
        raise _error(
            "template_not_published", "The requested Lead intake form is not available."
        )
    return row


def _message_context(message: InboxMessage) -> tuple[str, str, str]:
    metadata = dict(message.metadata_ or {})
    provider = str(metadata.get("provider") or "meta_social").strip()[:80]
    scope = str(
        metadata.get("provider_account_scope")
        or metadata.get("page_or_account_id")
        or metadata.get("phone_number_id")
        or "default"
    ).strip()[:200]
    endpoint = str(message.from_address or "").strip()[:320]
    if not endpoint:
        raise _error("endpoint_missing", "The source message has no reply endpoint.")
    if (
        message.channel_type in {"facebook_messenger", "instagram_dm"}
        and scope == "default"
    ):
        raise _error(
            "provider_scope_missing",
            "The Meta conversation has no provider account scope.",
        )
    return provider, scope, endpoint


def _new_invitation(
    db: Session,
    *,
    conversation: InboxConversation,
    message: InboxMessage,
    template: LeadIntakeTemplate,
    assessment_id: UUID | None,
    auto_issued: bool,
    actor_id: UUID | None,
    intent_key: str | None,
    intent_confidence: float | None,
    party_type_confidence: float | None,
) -> tuple[LeadIntakeInvitation, str]:
    provider, scope, endpoint = _message_context(message)
    token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    row = LeadIntakeInvitation(
        template_id=template.id,
        assessment_id=assessment_id,
        conversation_id=conversation.id,
        trigger_message_id=message.id,
        token_hash=token_hash(token),
        status="issued",
        auto_issued=auto_issued,
        channel_type=conversation.channel_type,
        provider=provider,
        provider_account_scope=scope,
        normalized_endpoint=endpoint,
        intent_key=intent_key,
        intent_confidence=intent_confidence,
        party_type_confidence=party_type_confidence,
        issued_by_system_user_id=actor_id,
        issued_at=now,
        expires_at=now + timedelta(hours=_ttl_hours(db)),
        delivery_status="pending",
    )
    db.add(row)
    db.flush()
    return row, token


def assess_inbound(db: Session, command: AssessInboundCommand) -> InvitationOutcome:
    def operation() -> InvitationOutcome:
        conversation = _unknown_conversation(db, command.conversation_id)
        message = db.scalars(
            select(InboxMessage)
            .where(InboxMessage.id == command.message_id)
            .with_for_update()
        ).one_or_none()
        if (
            message is None
            or message.conversation_id != conversation.id
            or message.direction != "inbound"
        ):
            raise _error(
                "message_not_eligible",
                "The intake trigger must be an inbound message.",
                kind="invalid",
            )
        existing = db.scalars(
            select(LeadIntakeAssessment).where(
                LeadIntakeAssessment.message_id == message.id
            )
        ).one_or_none()
        if existing:
            return InvitationOutcome(
                action=existing.decision, conversation_id=conversation.id, replayed=True
            )
        config = _ai_config(db, conversation.channel_type)
        item = command.classification
        threshold = float(config.confidence_threshold) if config else 1.0
        decision = LeadIntakeAssessmentDecision.not_eligible
        question = None
        if (
            not _bool_setting(db, "lead_intake_auto_send_enabled", False)
            or config is None
            or not _published_templates_ready(db)
        ):
            decision = LeadIntakeAssessmentDecision.not_eligible
        elif (
            item.intent.value not in QUALIFYING_INTENTS
            or item.intent_confidence < threshold
        ):
            decision = LeadIntakeAssessmentDecision.not_eligible
        elif (
            item.party_type.value == "unknown" or item.party_type_confidence < threshold
        ):
            previous = int(
                db.scalar(
                    select(func.count(LeadIntakeAssessment.id)).where(
                        LeadIntakeAssessment.conversation_id == conversation.id,
                        LeadIntakeAssessment.decision == "clarification_required",
                    )
                )
                or 0
            )
            if config.allow_followup_questions and previous < min(
                1, config.max_clarification_turns
            ):
                decision = LeadIntakeAssessmentDecision.clarification_required
                question = (
                    item.clarification_question
                    or "Is this request for you personally or for an organization?"
                )
            else:
                decision = LeadIntakeAssessmentDecision.staff_review
        else:
            decision = LeadIntakeAssessmentDecision.invite_issued
        assessment = LeadIntakeAssessment(
            conversation_id=conversation.id,
            message_id=message.id,
            intent_key=item.intent.value,
            intent_confidence=item.intent_confidence,
            party_type=item.party_type.value,
            party_type_confidence=item.party_type_confidence,
            decision=decision.value,
            provider_label=(command.provider_label or "")[:80] or None,
            model_label=(command.model_label or "")[:160] or None,
            clarification_question=question,
        )
        db.add(assessment)
        db.flush()
        if decision is not LeadIntakeAssessmentDecision.invite_issued:
            if decision is LeadIntakeAssessmentDecision.staff_review:
                team_inbox_operations.create_internal_note(
                    db,
                    conversation=conversation,
                    body="Lead intake automation needs staff review because the customer type remained ambiguous.",
                    actor_person_id=None,
                )
            return InvitationOutcome(
                action=decision.value,
                conversation_id=conversation.id,
                clarification_question=question,
            )
        existing_invite = db.scalars(
            select(LeadIntakeInvitation).where(
                LeadIntakeInvitation.conversation_id == conversation.id,
                LeadIntakeInvitation.auto_issued.is_(True),
            )
        ).one_or_none()
        if existing_invite:
            assessment.decision = "not_eligible"
            return InvitationOutcome(
                action="already_invited",
                conversation_id=conversation.id,
                invitation_id=existing_invite.id,
            )
        party_type = LeadIntakePartyType(item.party_type.value)
        template = _published_template(db, party_type)
        invitation, token = _new_invitation(
            db,
            conversation=conversation,
            message=message,
            template=template,
            assessment_id=assessment.id,
            auto_issued=True,
            actor_id=None,
            intent_key=item.intent.value,
            intent_confidence=item.intent_confidence,
            party_type_confidence=item.party_type_confidence,
        )
        return InvitationOutcome(
            action="invite_issued",
            conversation_id=conversation.id,
            invitation_id=invitation.id,
            token=token,
            invitation_message=template.invitation_message,
        )

    return execute_owner_command(
        db, definition=_ASSESS, context=command.context, operation=operation
    )


def record_provider_failure(
    db: Session, *, context: CommandContext, conversation_id: UUID, message_id: UUID
) -> None:
    def operation() -> None:
        conversation = _unknown_conversation(db, conversation_id)
        message = db.get(InboxMessage, message_id)
        if message is None or message.conversation_id != conversation.id:
            raise _error(
                "message_not_eligible",
                "The intake trigger message was not found.",
                kind="invalid",
            )
        if db.scalars(
            select(LeadIntakeAssessment.id).where(
                LeadIntakeAssessment.message_id == message.id
            )
        ).first():
            return
        db.add(
            LeadIntakeAssessment(
                conversation_id=conversation.id,
                message_id=message.id,
                intent_key="other",
                intent_confidence=0.0,
                party_type="unknown",
                party_type_confidence=0.0,
                decision="provider_failed",
            )
        )
        team_inbox_operations.create_internal_note(
            db,
            conversation=conversation,
            body="Lead intake automation could not classify the latest message; staff review is required.",
            actor_person_id=None,
        )
        db.flush()

    execute_owner_command(db, definition=_ASSESS, context=context, operation=operation)


def issue_manual_invitation(
    db: Session, command: ManualInvitationCommand
) -> InvitationOutcome:
    def operation() -> InvitationOutcome:
        _active_actor(db, command.actor_system_user_id)
        conversation = _unknown_conversation(db, command.conversation_id)
        message = db.get(InboxMessage, command.trigger_message_id)
        if message is None or message.conversation_id != conversation.id:
            raise _error(
                "message_not_eligible",
                "Select a message from this conversation.",
                kind="invalid",
            )
        template = _published_template(db, command.party_type)
        invitation, token = _new_invitation(
            db,
            conversation=conversation,
            message=message,
            template=template,
            assessment_id=None,
            auto_issued=False,
            actor_id=command.actor_system_user_id,
            intent_key=None,
            intent_confidence=None,
            party_type_confidence=None,
        )
        stage_audit_event(
            db,
            action="lead_intake.invitation_issued",
            entity_type="lead_intake_invitation",
            entity_id=str(invitation.id),
            actor_id=str(command.actor_system_user_id),
            request_id=str(command.context.command_id),
            metadata={
                "conversation_id": str(conversation.id),
                "party_type": command.party_type.value,
                "auto_issued": False,
            },
        )
        return InvitationOutcome(
            action="invite_issued",
            conversation_id=conversation.id,
            invitation_id=invitation.id,
            token=token,
            invitation_message=template.invitation_message,
        )

    return execute_owner_command(
        db, definition=_INVITATION, context=command.context, operation=operation
    )


def record_invitation_delivery(db: Session, command: InvitationDeliveryCommand) -> None:
    def operation() -> None:
        row = db.scalars(
            select(LeadIntakeInvitation)
            .where(LeadIntakeInvitation.id == command.invitation_id)
            .with_for_update()
        ).one_or_none()
        if row is None:
            raise _error(
                "invitation_not_found",
                "Lead intake invitation was not found.",
                kind="not_found",
            )
        row.outbound_message_id = command.message_id
        row.delivery_status = command.delivery_status[:40]
        row.delivery_error_code = (command.error_code or "")[:120] or None
        db.flush()

    execute_owner_command(
        db, definition=_INVITATION, context=command.context, operation=operation
    )


def revoke_invitation(db: Session, command: RevokeInvitationCommand) -> None:
    def operation() -> None:
        _active_actor(db, command.actor_system_user_id)
        row = db.scalars(
            select(LeadIntakeInvitation)
            .where(LeadIntakeInvitation.id == command.invitation_id)
            .with_for_update()
        ).one_or_none()
        if row is None:
            raise _error(
                "invitation_not_found",
                "Lead intake invitation was not found.",
                kind="not_found",
            )
        if row.conversation_id != command.conversation_id:
            raise _error(
                "invitation_not_found",
                "Lead intake invitation was not found.",
                kind="not_found",
            )
        if row.status == "completed":
            raise _error(
                "invitation_completed", "A completed invitation cannot be revoked."
            )
        if row.status != "revoked":
            row.status = "revoked"
            row.revoked_at = datetime.now(UTC)
            row.revoked_reason = _clean(command.reason, "reason", 240)
        db.flush()

    execute_owner_command(
        db, definition=_INVITATION, context=command.context, operation=operation
    )


def invitation_for_conversation(
    db: Session, conversation_id: UUID
) -> list[LeadIntakeInvitationSummary]:
    now = datetime.now(UTC)
    rows = db.scalars(
        select(LeadIntakeInvitation)
        .where(LeadIntakeInvitation.conversation_id == conversation_id)
        .order_by(LeadIntakeInvitation.issued_at.desc())
    ).all()
    result: list[LeadIntakeInvitationSummary] = []
    for row in rows:
        expired = row.status == "issued" and _as_utc(row.expires_at) <= now
        effective_status = LeadIntakeInvitationStatus(
            "expired" if expired else row.status
        )
        result.append(
            LeadIntakeInvitationSummary(
                invitation_id=row.id,
                party_type=LeadIntakePartyType(row.template.party_type),
                effective_status=effective_status,
                issued_at=row.issued_at,
                delivery_status=row.delivery_status or "pending",
                lead_id=row.lead_id,
                can_revoke=effective_status is LeadIntakeInvitationStatus.issued,
            )
        )
    return result


def latest_inbound_message_id(db: Session, conversation_id: UUID) -> UUID | None:
    return db.scalar(
        select(InboxMessage.id)
        .where(
            InboxMessage.conversation_id == conversation_id,
            InboxMessage.direction == "inbound",
        )
        .order_by(InboxMessage.created_at.desc())
        .limit(1)
    )


def get_public_form(
    db: Session, token: str, *, now: datetime | None = None
) -> PublicLeadIntakeForm:
    row = db.scalars(
        select(LeadIntakeInvitation).where(
            LeadIntakeInvitation.token_hash == token_hash(token)
        )
    ).one_or_none()
    if (
        row is None
        or row.status != "issued"
        or _as_utc(row.expires_at) <= _as_utc(now or datetime.now(UTC))
    ):
        raise _error(
            "invitation_unavailable",
            "This Lead intake link is invalid or expired.",
            kind="not_found",
        )
    template = row.template
    return PublicLeadIntakeForm(
        row.id,
        LeadIntakePartyType(template.party_type),
        template.heading,
        template.introduction,
        template.privacy_notice,
        row.expires_at,
    )


def _lead_source(channel_type: str) -> str:
    return {
        "whatsapp": "Whatsapp",
        "facebook_messenger": "Facebook",
        "instagram_dm": "Instagram",
    }[channel_type]


def _validated_address(command: SubmitLeadIntakeCommand) -> ResolvedLeadIntakeAddress:
    address = command.resolved_address
    if not command.submission.address_confirmation:
        raise _error(
            "address_not_confirmed",
            "Confirm the selected service address.",
            kind="invalid",
            field="address_confirmation",
        )
    if address.country_code.casefold() != "ng":
        raise _error(
            "address_outside_nigeria",
            "Select a service address in Nigeria.",
            kind="invalid",
            field="address",
        )
    state = normalize_state(address.state)
    if state == "Unknown":
        raise _error(
            "state_unresolved",
            "The selected address could not be matched to a Nigerian state or the FCT.",
            kind="invalid",
            field="address",
        )
    return address.model_copy(update={"state": state, "country_code": "NG"})


def _contact_point(db: Session, invitation: LeadIntakeInvitation, party_id: UUID):
    channel = PartyContactPointType(invitation.channel_type)
    social = channel in {
        PartyContactPointType.facebook_messenger,
        PartyContactPointType.instagram_dm,
    }
    return party_service.add_contact_point(
        db,
        party_id=party_id,
        channel_type=channel,
        normalized_value=invitation.normalized_endpoint,
        display_value=invitation.normalized_endpoint,
        scope_key=f"{invitation.provider}:{invitation.provider_account_scope}"
        if social
        else "default",
        provider=invitation.provider if social else None,
        provider_account_id=invitation.provider_account_scope if social else None,
        external_subject_id=invitation.normalized_endpoint if social else None,
        is_primary=True,
        verification_status=PartyContactVerificationStatus.unverified,
        consent_status=PartyContactConsentStatus.unknown,
        metadata={
            "captured_by": OWNER,
            "conversation_id": str(invitation.conversation_id),
        },
    )


def submit_form(
    db: Session, command: SubmitLeadIntakeCommand
) -> SubmitLeadIntakeOutcome:
    def operation() -> SubmitLeadIntakeOutcome:
        invitation = db.scalars(
            select(LeadIntakeInvitation)
            .where(LeadIntakeInvitation.token_hash == token_hash(command.token))
            .with_for_update()
        ).one_or_none()
        if invitation is None:
            raise _error(
                "invitation_unavailable",
                "This Lead intake link is invalid or expired.",
                kind="not_found",
            )
        template = invitation.template
        if invitation.status == "completed":
            assert invitation.lead_id and invitation.party_id
            return SubmitLeadIntakeOutcome(
                invitation.id,
                invitation.conversation_id,
                invitation.lead_id,
                invitation.party_id,
                template.thank_you_message,
                template.confirmation_message,
                True,
            )
        now = datetime.now(UTC)
        if invitation.status != "issued" or _as_utc(invitation.expires_at) <= now:
            raise _error(
                "invitation_unavailable",
                "This Lead intake link is invalid or expired.",
                kind="not_found",
            )
        if not command.submission.privacy_acknowledged:
            raise _error(
                "privacy_acknowledgement_required",
                "Acknowledge the privacy notice before saving.",
                kind="invalid",
                field="privacy_acknowledged",
            )
        address = _validated_address(command)
        party_type = LeadIntakePartyType(template.party_type)
        representative_party_id = None
        if party_type is LeadIntakePartyType.individual:
            title = _clean(command.submission.full_name, "full_name", 200)
            gender = _clean(command.submission.gender, "gender", 24).lower()
            if gender not in {"female", "male", "non_binary", "other"}:
                raise _error(
                    "gender_invalid",
                    "Select a valid gender.",
                    kind="invalid",
                    field="gender",
                )
            dob = command.submission.date_of_birth
            if dob is None or dob > date.today():
                raise _error(
                    "date_of_birth_invalid",
                    "Enter a valid date of birth.",
                    kind="invalid",
                    field="date_of_birth",
                )
            lead_party_id = uuid5(invitation.id, "individual-party")
            lead_party = party_service.create_party(
                db,
                party_id=lead_party_id,
                party_type=PartyType.person,
                display_name=title,
                metadata={
                    "profile_version": 1,
                    "gender": gender,
                    "date_of_birth": dob.isoformat(),
                    "address": address.display_name,
                    "latitude": address.latitude,
                    "longitude": address.longitude,
                    "state": address.state,
                    "country_code": address.country_code,
                    "identity_managed_by": "sub",
                },
            )
            contact_owner_id = lead_party.id
        else:
            title = _clean(
                command.submission.organization_name, "organization_name", 200
            )
            representative_name = _clean(
                command.submission.representative_name, "representative_name", 200
            )
            representative_role = _clean(
                command.submission.representative_role, "representative_role", 120
            )
            lead_party_id = uuid5(invitation.id, "organization-party")
            lead_party = party_service.create_party(
                db,
                party_id=lead_party_id,
                party_type=PartyType.organization,
                display_name=title,
                metadata={
                    "profile_version": 1,
                    "business_address": address.display_name,
                    "latitude": address.latitude,
                    "longitude": address.longitude,
                    "state": address.state,
                    "country_code": address.country_code,
                    "identity_managed_by": "sub",
                },
            )
            representative_party_id = uuid5(invitation.id, "representative-party")
            representative = party_service.create_party(
                db,
                party_id=representative_party_id,
                party_type=PartyType.person,
                display_name=representative_name,
                metadata={
                    "profile_version": 1,
                    "representative_role": representative_role,
                    "identity_managed_by": "sub",
                },
            )
            party_service.relate_parties(
                db,
                subject_party_id=representative.id,
                object_party_id=lead_party.id,
                relationship_type=PartyRelationshipType.contact_for,
                source=OWNER,
                metadata={"representative_role": representative_role},
            )
            contact_owner_id = representative.id
        party_service.ensure_role(
            db,
            party_id=lead_party.id,
            role_type=PartyRoleType.prospect,
            status=PartyRoleStatus.active,
            source=OWNER,
        )
        contact_point = _contact_point(db, invitation, contact_owner_id)
        lead_id = uuid5(invitation.id, "lead")
        fingerprint = hashlib.sha256(
            f"{invitation.id}:{party_type.value}:{address.latitude:.7f}:{address.longitude:.7f}".encode()
        ).hexdigest()
        lead = lifecycle.create_party_lead(
            db,
            lead_id=lead_id,
            party_id=lead_party.id,
            title=title,
            lead_source=_lead_source(invitation.channel_type),
            binding_source=OWNER,
            binding_reason="Party created atomically from a completed Inbox lead-intake invitation",
            origin_capture={
                "capture_method": LeadCaptureMethod.inbox_form.value,
                "source_platform": LeadSourcePlatform.team_inbox.value,
                "source_interaction_id": f"lead-intake:{invitation.id}",
                "capture_fingerprint": fingerprint,
                "capture_source": "public_inbox_lead_intake_form",
                "capture_reason": "Customer saved the single-use form issued from an unknown Meta Inbox conversation",
            },
            region=address.state,
            address=address.display_name,
            metadata={
                "lead_intake_invitation_id": str(invitation.id),
                "origin_conversation_id": str(invitation.conversation_id),
                "origin_message_id": str(invitation.trigger_message_id),
                "channel_type": invitation.channel_type,
                "latitude": address.latitude,
                "longitude": address.longitude,
                "privacy_acknowledged_at": now.isoformat(),
                "marketing_consent_inferred": False,
                "representative_party_id": str(representative_party_id)
                if representative_party_id
                else None,
            },
            owner_agent_id=template.owner_system_user_id,
            pipeline_id=template.pipeline_id,
            stage_id=template.stage_id,
        )
        conversation = _unknown_conversation(db, invitation.conversation_id)
        team_inbox_participants.bind_endpoint_to_contact_point(
            db,
            conversation_id=conversation.id,
            channel_type=invitation.channel_type,
            normalized_endpoint=invitation.normalized_endpoint,
            provider_account_scope=invitation.provider_account_scope,
            party_contact_point_id=contact_point.id,
        )
        team_inbox_operations.route_to_service_team(
            db,
            conversation=conversation,
            service_team_id=template.target_service_team_id,
            source=OWNER,
        )
        team_inbox_operations.create_internal_note(
            db,
            conversation=conversation,
            body=f"Lead intake completed. Lead {lead.id} was created and routed to Sales.",
            actor_person_id=None,
        )
        invitation.status = "completed"
        invitation.completed_at = now
        invitation.lead_id = lead.id
        invitation.party_id = lead_party.id
        invitation.representative_party_id = representative_party_id
        invitation.party_contact_point_id = contact_point.id
        emit_event(
            db,
            EventType.lead_created,
            {
                "lead_id": str(lead.id),
                "party_id": str(lead_party.id),
                "status": lead.status,
                "lead_source": lead.lead_source,
                "origin_conversation_id": str(conversation.id),
            },
            actor=command.context.actor,
        )
        stage_audit_event(
            db,
            action="lead_intake.completed",
            entity_type="lead_intake_invitation",
            entity_id=str(invitation.id),
            actor_id=None,
            request_id=str(command.context.command_id),
            metadata={
                "lead_id": str(lead.id),
                "party_id": str(lead_party.id),
                "conversation_id": str(conversation.id),
                "party_type": party_type.value,
                "state": address.state,
            },
        )
        db.flush()
        return SubmitLeadIntakeOutcome(
            invitation.id,
            conversation.id,
            lead.id,
            lead_party.id,
            template.thank_you_message,
            template.confirmation_message,
            False,
        )

    return execute_owner_command(
        db, definition=_SUBMISSION, context=command.context, operation=operation
    )
