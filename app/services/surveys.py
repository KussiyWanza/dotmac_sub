"""Authoritative Survey lifecycle, invitations, and response validation.

Web, API, public-response, and event-handler modules are adapters. This owner
validates typed Survey content, controls lifecycle eligibility, and completes
each mutation through the repository owner-command boundary.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Query, Session

from app.db import finish_read_transaction
from app.models.audit import AuditActorType
from app.models.comms import (
    Survey,
    SurveyInvitation,
    SurveyInvitationStatus,
    SurveyResponse,
    SurveyStatus,
    SurveyTriggerType,
)
from app.models.notification import NotificationChannel
from app.models.subscriber import Subscriber
from app.models.system_user import SystemUser
from app.schemas.comms import (
    SurveyCreate,
    SurveyQuestion,
    SurveyQuestionType,
    SurveyUpdate,
    normalize_public_slug,
)
from app.services.audit_adapter import stage_audit_event
from app.services.common import apply_ordering, apply_pagination, coerce_uuid
from app.services.domain_errors import DomainError
from app.services.events import EventType, emit_event
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

SurveyErrorKind = Literal["invalid", "forbidden", "not_found", "conflict"]

OWNER = "communications.surveys"
_LIFECYCLE_DEFINITION = OwnerCommandDefinition(
    owner=OWNER,
    concern="survey lifecycle and content",
    name="mutate_survey_lifecycle",
)
_INVITATION_DEFINITION = OwnerCommandDefinition(
    owner=OWNER,
    concern="survey invitation records",
    name="create_survey_invitations",
)
_RESPONSE_DEFINITION = OwnerCommandDefinition(
    owner=OWNER,
    concern="survey response records",
    name="record_survey_response",
)
_REPAIR_DEFINITION = OwnerCommandDefinition(
    owner=OWNER,
    concern="survey lifecycle and content",
    name="rebuild_survey_projections",
)
_QUESTION_LIST_ADAPTER = TypeAdapter(list[SurveyQuestion])


class SurveyDomainError(DomainError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        kind: SurveyErrorKind = "conflict",
        field: str | None = None,
    ) -> None:
        details: dict[str, object] = {"kind": kind}
        if field is not None:
            details["field"] = field
        super().__init__(code=f"{OWNER}.{code}", message=message, details=details)
        self.kind = kind
        self.field = field


@dataclass(frozen=True)
class CreateSurveyCommand:
    payload: SurveyCreate
    principal_id: UUID
    principal_type: str
    context: CommandContext


@dataclass(frozen=True)
class UpdateSurveyCommand:
    survey_id: UUID
    payload: SurveyUpdate
    context: CommandContext


class SurveyLifecycleAction(StrEnum):
    activate = "activate"
    pause = "pause"
    close = "close"
    archive = "archive"


@dataclass(frozen=True)
class SurveyLifecycleCommand:
    survey_id: UUID
    action: SurveyLifecycleAction
    context: CommandContext


@dataclass(frozen=True)
class SendSurveyCommand:
    survey_id: UUID
    subscriber_ids: tuple[UUID, ...]
    context: CommandContext


@dataclass(frozen=True)
class TriggerSurveyInvitationsCommand:
    trigger_type: SurveyTriggerType
    source_event_id: UUID
    source_entity_id: UUID | None
    subscriber_id: UUID | None
    context: CommandContext


@dataclass(frozen=True)
class SurveyAnswer:
    key: str
    value: str


@dataclass(frozen=True)
class SubmitSurveyResponseCommand:
    public_reference: str | None
    invitation_token: str | None
    answers: tuple[SurveyAnswer, ...]
    work_order_id: UUID | None
    ticket_id: UUID | None
    context: CommandContext
    legacy_rating: int | None = None
    legacy_nps_value: int | None = None


@dataclass(frozen=True)
class SurveyMutationOutcome:
    survey_id: UUID
    status: SurveyStatus
    replayed: bool = False


@dataclass(frozen=True)
class SurveyInvitationOutcome:
    survey_ids: tuple[UUID, ...]
    invitation_ids: tuple[UUID, ...]
    created_count: int


@dataclass(frozen=True)
class SurveyResponseOutcome:
    survey_id: UUID
    response_id: UUID
    survey_name: str
    thank_you_message: str | None


@dataclass(frozen=True)
class RebuildSurveyProjectionsCommand:
    survey_id: UUID
    context: CommandContext


@dataclass(frozen=True)
class SurveyProjectionOutcome:
    survey_id: UUID
    total_invited: int
    total_responses: int
    avg_rating: Decimal | None
    nps_score: Decimal | None


@dataclass(frozen=True)
class SurveyFormValues:
    name: str = ""
    description: str = ""
    trigger_type: str = SurveyTriggerType.manual.value
    public_slug: str = ""
    thank_you_message: str = ""
    questions_json: str = "[]"
    idempotency_key: str = ""


@dataclass(frozen=True)
class SurveyFormValidation:
    values: SurveyFormValues
    questions_seed: tuple[object, ...]
    payload: SurveyCreate | None
    errors: tuple[str, ...]
    field_errors: dict[str, str]


def form_values_for_survey(
    survey: Survey | None, *, idempotency_key: str
) -> SurveyFormValues:
    if survey is None:
        return SurveyFormValues(idempotency_key=idempotency_key)
    return SurveyFormValues(
        name=survey.name,
        description=survey.description or "",
        trigger_type=survey.trigger_type.value,
        public_slug=survey.public_slug or "",
        thank_you_message=survey.thank_you_message or "",
        questions_json=json.dumps(survey.questions or []),
        idempotency_key=idempotency_key,
    )


def validate_form(values: SurveyFormValues) -> SurveyFormValidation:
    """Parse untrusted form state without allowing FastAPI's JSON 422 path."""

    normalized_slug = normalize_public_slug(values.public_slug) or ""
    normalized_values = SurveyFormValues(
        name=values.name,
        description=values.description,
        trigger_type=values.trigger_type,
        public_slug=normalized_slug,
        thank_you_message=values.thank_you_message,
        questions_json=values.questions_json,
        idempotency_key=values.idempotency_key,
    )
    try:
        parsed = json.loads(values.questions_json or "[]")
    except json.JSONDecodeError:
        return SurveyFormValidation(
            values=normalized_values,
            questions_seed=(),
            payload=None,
            errors=("Questions must be valid JSON.",),
            field_errors={"questions_json": "Questions must be valid JSON."},
        )
    if not isinstance(parsed, list):
        message = "Questions must be submitted as a JSON array."
        return SurveyFormValidation(
            values=normalized_values,
            questions_seed=(),
            payload=None,
            errors=(message,),
            field_errors={"questions_json": message},
        )
    try:
        payload = SurveyCreate.model_validate(
            {
                "name": values.name,
                "description": values.description,
                "trigger_type": values.trigger_type,
                "public_slug": values.public_slug,
                "thank_you_message": values.thank_you_message,
                "questions": parsed,
            }
        )
    except ValidationError as exc:
        errors: list[str] = []
        field_errors: dict[str, str] = {}
        labels = {
            "name": "Name",
            "description": "Description",
            "trigger_type": "Trigger Type",
            "public_slug": "Public Slug",
            "thank_you_message": "Thank You Message",
            "questions": "Questions",
        }
        for item in exc.errors(include_url=False):
            location = item.get("loc") or ()
            field = str(location[0]) if location else "form"
            display_field = "questions_json" if field == "questions" else field
            location_text = ""
            if (
                field == "questions"
                and len(location) > 1
                and isinstance(location[1], int)
            ):
                location_text = f" question {int(location[1]) + 1}"
            message = f"{labels.get(field, 'Survey')}{location_text}: {item['msg']}"
            errors.append(message)
            field_errors.setdefault(display_field, message)
        return SurveyFormValidation(
            values=normalized_values,
            questions_seed=tuple(parsed),
            payload=None,
            errors=tuple(errors),
            field_errors=field_errors,
        )
    clean_questions = tuple(
        question.model_dump(mode="json") for question in payload.questions
    )
    normalized_values = SurveyFormValues(
        name=payload.name,
        description=payload.description or "",
        trigger_type=payload.trigger_type.value,
        public_slug=payload.public_slug or "",
        thank_you_message=payload.thank_you_message or "",
        questions_json=json.dumps(clean_questions),
        idempotency_key=values.idempotency_key,
    )
    return SurveyFormValidation(
        values=normalized_values,
        questions_seed=clean_questions,
        payload=payload,
        errors=(),
        field_errors={},
    )


def _now() -> datetime:
    return datetime.now(UTC)


def _question_rows(questions: list[SurveyQuestion]) -> list[dict[str, object]]:
    return [question.model_dump(mode="json") for question in questions]


def _validated_questions(survey: Survey) -> list[SurveyQuestion]:
    try:
        questions = _QUESTION_LIST_ADAPTER.validate_python(survey.questions or [])
    except ValidationError as exc:
        raise SurveyDomainError(
            "invalid_questions",
            "The Survey contains invalid questions and cannot be used.",
            kind="invalid",
            field="questions_json",
        ) from exc
    keys = [question.key for question in questions]
    if len(keys) != len(set(keys)):
        raise SurveyDomainError(
            "duplicate_question_key",
            "Question keys must be unique within the Survey.",
            kind="invalid",
            field="questions_json",
        )
    return questions


def _require_usable_questions(survey: Survey) -> list[SurveyQuestion]:
    questions = _validated_questions(survey)
    if not questions:
        raise SurveyDomainError(
            "questions_required",
            "Add at least one valid question before activating or sending this Survey.",
            kind="invalid",
            field="questions_json",
        )
    if survey.expires_at is not None and survey.expires_at <= _now():
        raise SurveyDomainError(
            "survey_expired",
            "This Survey has expired and cannot be activated or distributed.",
            kind="conflict",
        )
    return questions


def _require_distributable(survey: Survey) -> list[SurveyQuestion]:
    questions = _require_usable_questions(survey)
    if not survey.is_active:
        raise SurveyDomainError(
            "survey_inactive",
            "This Survey is inactive and cannot be distributed.",
            kind="conflict",
        )
    return questions


def _creation_fingerprint(payload: SurveyCreate, creator_id: UUID) -> str:
    canonical = json.dumps(
        {
            "creator_id": str(creator_id),
            **payload.model_dump(mode="json"),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _creator_person_id(db: Session, command: CreateSurveyCommand) -> UUID:
    if command.principal_type != "system_user":
        raise SurveyDomainError(
            "creator_not_authorized",
            "A staff administrator is required to create a Survey.",
            kind="forbidden",
        )
    user = db.get(SystemUser, command.principal_id)
    if user is None or not user.is_active or user.person_party_id is None:
        raise SurveyDomainError(
            "creator_person_unresolved",
            "Your administrator account is not linked to a Person record.",
            kind="forbidden",
        )
    return user.person_party_id


def _constraint_name(exc: IntegrityError) -> str:
    diagnostic = getattr(exc.orig, "diag", None)
    named = getattr(diagnostic, "constraint_name", None)
    return str(named or exc.orig).lower()


def _public_slug_available(
    db: Session, public_slug: str | None, *, exclude_survey_id: UUID | None = None
) -> bool:
    if public_slug is None:
        return True
    query = db.query(Survey.id).filter(Survey.public_slug == public_slug)
    if exclude_survey_id is not None:
        query = query.filter(Survey.id != exclude_survey_id)
    return query.first() is None


def create_survey(db: Session, command: CreateSurveyCommand) -> SurveyMutationOutcome:
    if not command.context.idempotency_key:
        raise SurveyDomainError(
            "idempotency_key_required",
            "Survey creation requires an idempotency key.",
            kind="invalid",
        )
    if len(command.context.idempotency_key) > 80:
        raise SurveyDomainError(
            "idempotency_key_invalid",
            "The Survey creation idempotency key is invalid.",
            kind="invalid",
        )

    def operation() -> SurveyMutationOutcome:
        creator_id = _creator_person_id(db, command)
        fingerprint = _creation_fingerprint(command.payload, creator_id)
        existing = (
            db.query(Survey)
            .filter(Survey.creation_idempotency_key == command.context.idempotency_key)
            .one_or_none()
        )
        if existing is not None:
            if existing.creation_fingerprint != fingerprint:
                raise SurveyDomainError(
                    "idempotency_conflict",
                    "This form submission key was already used for different Survey data.",
                    kind="conflict",
                )
            return SurveyMutationOutcome(
                survey_id=existing.id,
                status=existing.status,
                replayed=True,
            )
        if not _public_slug_available(db, command.payload.public_slug):
            raise SurveyDomainError(
                "public_slug_duplicate",
                "That public slug is already in use. Choose another slug.",
                kind="invalid",
                field="public_slug",
            )

        survey = Survey(
            name=command.payload.name,
            description=command.payload.description,
            questions=_question_rows(command.payload.questions),
            trigger_type=command.payload.trigger_type,
            public_slug=command.payload.public_slug,
            thank_you_message=command.payload.thank_you_message,
            status=SurveyStatus.draft,
            is_active=True,
            created_by_id=creator_id,
            expires_at=None,
            segment_filter=None,
            total_invited=0,
            total_responses=0,
            avg_rating=None,
            nps_score=None,
            creation_idempotency_key=command.context.idempotency_key,
            creation_fingerprint=fingerprint,
        )
        db.add(survey)
        db.flush()
        stage_audit_event(
            db,
            action="survey.created",
            entity_type="survey",
            entity_id=str(survey.id),
            actor_type=AuditActorType.user,
            actor_id=str(creator_id),
            request_id=command.context.idempotency_key,
            status_code=201,
            metadata={
                "owner": OWNER,
                "status": SurveyStatus.draft.value,
                "trigger_type": survey.trigger_type.value,
                "question_count": len(survey.questions),
            },
        )
        emit_event(
            db,
            EventType.custom,
            {
                "name": "survey.created",
                "survey_id": str(survey.id),
                "status": survey.status.value,
                "trigger_type": survey.trigger_type.value,
            },
            actor=str(creator_id),
        )
        return SurveyMutationOutcome(survey.id, survey.status)

    try:
        return execute_owner_command(
            db,
            definition=_LIFECYCLE_DEFINITION,
            context=command.context,
            operation=operation,
        )
    except IntegrityError as exc:
        constraint = _constraint_name(exc)
        if "public_slug" in constraint:
            raise SurveyDomainError(
                "public_slug_duplicate",
                "That public slug is already in use. Choose another slug.",
                kind="invalid",
                field="public_slug",
            ) from exc
        if "creation_idempotency" in constraint:
            replay: SurveyMutationOutcome | None = None
            try:
                existing = (
                    db.query(Survey)
                    .filter(
                        Survey.creation_idempotency_key
                        == command.context.idempotency_key
                    )
                    .one_or_none()
                )
                if existing is not None:
                    creator_id = _creator_person_id(db, command)
                    if existing.creation_fingerprint == _creation_fingerprint(
                        command.payload, creator_id
                    ):
                        replay = SurveyMutationOutcome(
                            existing.id, existing.status, replayed=True
                        )
            finally:
                finish_read_transaction(db)
            if replay is not None:
                return replay
            raise SurveyDomainError(
                "idempotency_conflict",
                "This form submission was already used for different Survey data.",
                kind="conflict",
            ) from exc
        raise


def update_survey(db: Session, command: UpdateSurveyCommand) -> SurveyMutationOutcome:
    def operation() -> SurveyMutationOutcome:
        survey = (
            db.query(Survey)
            .filter(Survey.id == command.survey_id)
            .with_for_update()
            .one_or_none()
        )
        if survey is None:
            raise SurveyDomainError(
                "survey_not_found", "Survey not found.", kind="not_found"
            )
        fields = command.payload.model_fields_set
        if "public_slug" in fields and not _public_slug_available(
            db,
            command.payload.public_slug,
            exclude_survey_id=survey.id,
        ):
            raise SurveyDomainError(
                "public_slug_duplicate",
                "That public slug is already in use. Choose another slug.",
                kind="invalid",
                field="public_slug",
            )
        if "name" in fields and command.payload.name is not None:
            survey.name = command.payload.name
        if "description" in fields:
            survey.description = command.payload.description
        if "trigger_type" in fields and command.payload.trigger_type is not None:
            survey.trigger_type = command.payload.trigger_type
        if "public_slug" in fields:
            survey.public_slug = command.payload.public_slug
        if "thank_you_message" in fields:
            survey.thank_you_message = command.payload.thank_you_message
        if "questions" in fields and command.payload.questions is not None:
            survey.questions = _question_rows(command.payload.questions)
        if "is_active" in fields and command.payload.is_active is not None:
            if command.payload.is_active:
                if survey.status is SurveyStatus.closed:
                    raise SurveyDomainError(
                        "closed_survey",
                        "A closed Survey cannot be activated.",
                        kind="conflict",
                    )
                _require_usable_questions(survey)
                survey.status = SurveyStatus.active
                survey.is_active = True
            else:
                survey.is_active = False
                if survey.status is SurveyStatus.active:
                    survey.status = SurveyStatus.paused
        db.flush()
        stage_audit_event(
            db,
            action="survey.updated",
            entity_type="survey",
            entity_id=str(survey.id),
            actor_type=AuditActorType.user,
            actor_id=command.context.actor,
            request_id=command.context.idempotency_key,
            metadata={"owner": OWNER, "fields": sorted(fields)},
        )
        emit_event(
            db,
            EventType.custom,
            {"name": "survey.updated", "survey_id": str(survey.id)},
            actor=command.context.actor,
        )
        return SurveyMutationOutcome(survey.id, survey.status)

    try:
        return execute_owner_command(
            db,
            definition=_LIFECYCLE_DEFINITION,
            context=command.context,
            operation=operation,
        )
    except IntegrityError as exc:
        if "public_slug" in _constraint_name(exc):
            raise SurveyDomainError(
                "public_slug_duplicate",
                "That public slug is already in use. Choose another slug.",
                kind="invalid",
                field="public_slug",
            ) from exc
        raise


def transition_survey(
    db: Session, command: SurveyLifecycleCommand
) -> SurveyMutationOutcome:
    def operation() -> SurveyMutationOutcome:
        survey = (
            db.query(Survey)
            .filter(Survey.id == command.survey_id)
            .with_for_update()
            .one_or_none()
        )
        if survey is None:
            raise SurveyDomainError(
                "survey_not_found", "Survey not found.", kind="not_found"
            )
        previous = survey.status
        if command.action is SurveyLifecycleAction.activate:
            if survey.status is SurveyStatus.closed:
                raise SurveyDomainError(
                    "closed_survey",
                    "A closed Survey cannot be activated.",
                    kind="conflict",
                )
            _require_usable_questions(survey)
            survey.status = SurveyStatus.active
            survey.is_active = True
        elif command.action is SurveyLifecycleAction.pause:
            if survey.status is not SurveyStatus.active:
                raise SurveyDomainError(
                    "pause_requires_active",
                    "Only an active Survey can be paused.",
                    kind="conflict",
                )
            survey.status = SurveyStatus.paused
        elif command.action is SurveyLifecycleAction.close:
            survey.status = SurveyStatus.closed
        elif command.action is SurveyLifecycleAction.archive:
            survey.status = SurveyStatus.closed
            survey.is_active = False
        db.flush()
        action_name = f"survey.{command.action.value}d"
        stage_audit_event(
            db,
            action=action_name,
            entity_type="survey",
            entity_id=str(survey.id),
            actor_type=AuditActorType.user,
            actor_id=command.context.actor,
            request_id=command.context.idempotency_key,
            metadata={
                "owner": OWNER,
                "previous_status": previous.value,
                "status": survey.status.value,
                "is_active": survey.is_active,
            },
        )
        emit_event(
            db,
            EventType.custom,
            {
                "name": action_name,
                "survey_id": str(survey.id),
                "previous_status": previous.value,
                "status": survey.status.value,
            },
            actor=command.context.actor,
        )
        return SurveyMutationOutcome(survey.id, survey.status)

    return execute_owner_command(
        db,
        definition=_LIFECYCLE_DEFINITION,
        context=command.context,
        operation=operation,
    )


def _queue_invitation(
    db: Session,
    *,
    survey: Survey,
    subscriber_id: UUID,
    source_type: SurveyTriggerType,
    source_event_id: UUID,
    source_entity_id: UUID | None,
) -> tuple[SurveyInvitation, bool]:
    existing = (
        db.query(SurveyInvitation)
        .filter(SurveyInvitation.survey_id == survey.id)
        .filter(SurveyInvitation.subscriber_id == subscriber_id)
        .filter(SurveyInvitation.source_event_id == source_event_id)
        .one_or_none()
    )
    if existing is not None:
        return existing, False
    if db.get(Subscriber, subscriber_id) is None:
        raise SurveyDomainError(
            "recipient_not_found",
            "A Survey invitation recipient could not be resolved.",
            kind="not_found",
        )
    invitation = SurveyInvitation(
        survey_id=survey.id,
        subscriber_id=subscriber_id,
        token=secrets.token_urlsafe(32),
        source_type=source_type,
        source_event_id=source_event_id,
        source_entity_id=source_entity_id,
        status=SurveyInvitationStatus.pending,
        sent_at=_now(),
    )
    db.add(invitation)
    db.flush()
    from app.services import customer_experience_communications

    customer_experience_communications.request_update(
        db,
        subscriber_id=subscriber_id,
        event_type="survey_invitation",
        subject=survey.name,
        body=f"Please share your feedback: /s/t/{invitation.token}",
        metadata={
            "type": "survey",
            "survey_id": str(survey.id),
            "invitation_id": str(invitation.id),
            "source_type": source_type.value,
            "source_entity_id": str(source_entity_id) if source_entity_id else None,
        },
        dedupe_key=f"survey-invitation:{invitation.id}",
        default_channels=(
            NotificationChannel.email,
            NotificationChannel.whatsapp,
            NotificationChannel.push,
        ),
    )
    return invitation, True


def send_survey(db: Session, command: SendSurveyCommand) -> SurveyInvitationOutcome:
    def operation() -> SurveyInvitationOutcome:
        survey = (
            db.query(Survey)
            .filter(Survey.id == command.survey_id)
            .with_for_update()
            .one_or_none()
        )
        if survey is None:
            raise SurveyDomainError(
                "survey_not_found", "Survey not found.", kind="not_found"
            )
        _require_usable_questions(survey)
        if survey.status is SurveyStatus.closed:
            raise SurveyDomainError(
                "closed_survey", "A closed Survey cannot be sent.", kind="conflict"
            )
        if survey.status is not SurveyStatus.active:
            survey.status = SurveyStatus.active
        survey.is_active = True
        invitation_ids: list[UUID] = []
        created = 0
        for subscriber_id in dict.fromkeys(command.subscriber_ids):
            invitation, was_created = _queue_invitation(
                db,
                survey=survey,
                subscriber_id=subscriber_id,
                source_type=SurveyTriggerType.manual,
                source_event_id=command.context.command_id,
                source_entity_id=None,
            )
            invitation_ids.append(invitation.id)
            created += int(was_created)
        survey.total_invited += created
        db.flush()
        stage_audit_event(
            db,
            action="survey.sent",
            entity_type="survey",
            entity_id=str(survey.id),
            actor_type=AuditActorType.user,
            actor_id=command.context.actor,
            request_id=command.context.idempotency_key,
            metadata={"owner": OWNER, "invitation_count": created},
        )
        emit_event(
            db,
            EventType.custom,
            {
                "name": "survey.sent",
                "survey_id": str(survey.id),
                "invitation_count": created,
            },
            actor=command.context.actor,
        )
        return SurveyInvitationOutcome((survey.id,), tuple(invitation_ids), created)

    return execute_owner_command(
        db,
        definition=_INVITATION_DEFINITION,
        context=command.context,
        operation=operation,
    )


def eligible_automatic_surveys(
    db: Session, trigger_type: SurveyTriggerType, *, now: datetime | None = None
) -> list[Survey]:
    effective_now = now or _now()
    return (
        db.query(Survey)
        .filter(Survey.trigger_type == trigger_type)
        .filter(Survey.status == SurveyStatus.active)
        .filter(Survey.is_active.is_(True))
        .filter(or_(Survey.expires_at.is_(None), Survey.expires_at > effective_now))
        .order_by(Survey.created_at.asc(), Survey.id.asc())
        .all()
    )


def create_trigger_invitations(
    db: Session, command: TriggerSurveyInvitationsCommand
) -> SurveyInvitationOutcome:
    def operation() -> SurveyInvitationOutcome:
        if command.subscriber_id is None:
            return SurveyInvitationOutcome((), (), 0)
        surveys = eligible_automatic_surveys(db, command.trigger_type)
        survey_ids: list[UUID] = []
        invitation_ids: list[UUID] = []
        created = 0
        for survey in surveys:
            try:
                _require_distributable(survey)
            except SurveyDomainError:
                continue
            invitation, was_created = _queue_invitation(
                db,
                survey=survey,
                subscriber_id=command.subscriber_id,
                source_type=command.trigger_type,
                source_event_id=command.source_event_id,
                source_entity_id=command.source_entity_id,
            )
            survey_ids.append(survey.id)
            invitation_ids.append(invitation.id)
            if was_created:
                survey.total_invited += 1
                created += 1
        db.flush()
        if created:
            emit_event(
                db,
                EventType.custom,
                {
                    "name": "survey.trigger_invitations_created",
                    "source_event_id": str(command.source_event_id),
                    "trigger_type": command.trigger_type.value,
                    "invitation_count": created,
                },
                actor=OWNER,
                subscriber_id=command.subscriber_id,
            )
        return SurveyInvitationOutcome(
            tuple(survey_ids), tuple(invitation_ids), created
        )

    return execute_owner_command(
        db,
        definition=_INVITATION_DEFINITION,
        context=command.context,
        operation=operation,
    )


def _public_filter(
    query: Query[Survey], *, now: datetime | None = None
) -> Query[Survey]:
    effective_now = now or _now()
    return (
        query.filter(Survey.is_active.is_(True))
        .filter(Survey.status == SurveyStatus.active)
        .filter(or_(Survey.expires_at.is_(None), Survey.expires_at > effective_now))
    )


def _try_coerce_uuid(value: str) -> UUID | None:
    try:
        return coerce_uuid(value)
    except ValueError:
        return None


def get_public_survey(db: Session, reference: str) -> Survey:
    query = db.query(Survey)
    survey_id = _try_coerce_uuid(reference)
    if survey_id is None:
        query = query.filter(Survey.public_slug == reference.strip().lower())
    else:
        query = query.filter(
            or_(Survey.id == survey_id, Survey.public_slug == reference)
        )
    survey = _public_filter(query).one_or_none()
    if survey is None:
        raise SurveyDomainError(
            "survey_unavailable",
            "This Survey is unavailable.",
            kind="not_found",
        )
    _require_distributable(survey)
    return survey


def get_invitation_survey(
    db: Session, token: str, *, include_completed: bool = False
) -> tuple[SurveyInvitation, Survey]:
    invitation = (
        db.query(SurveyInvitation).filter(SurveyInvitation.token == token).one_or_none()
    )
    if invitation is None or (
        not include_completed
        and invitation.status is not SurveyInvitationStatus.pending
    ):
        raise SurveyDomainError(
            "invitation_unavailable",
            "This Survey invitation is unavailable.",
            kind="not_found",
        )
    survey = _public_filter(
        db.query(Survey).filter(Survey.id == invitation.survey_id)
    ).one_or_none()
    if survey is None:
        raise SurveyDomainError(
            "survey_unavailable",
            "This Survey is unavailable.",
            kind="not_found",
        )
    _require_distributable(survey)
    return invitation, survey


def _validated_answers(
    questions: list[SurveyQuestion],
    answers: tuple[SurveyAnswer, ...],
    *,
    legacy_rating: int | None = None,
    legacy_nps_value: int | None = None,
) -> tuple[dict[str, str], int | None, int | None]:
    submitted: dict[str, str] = {}
    for answer in answers:
        if answer.key in submitted:
            raise SurveyDomainError(
                "duplicate_answer",
                f'Question "{answer.key}" was answered more than once.',
                kind="invalid",
            )
        submitted[answer.key] = answer.value.strip()
    known_keys = {question.key for question in questions}
    unknown = sorted(set(submitted) - known_keys)
    if unknown:
        raise SurveyDomainError(
            "unknown_answer_key",
            "The response contains an unknown question.",
            kind="invalid",
        )

    normalized: dict[str, str] = {}
    rating: int | None = None
    nps_value: int | None = None
    for question in questions:
        value = submitted.get(question.key, "").strip()
        if not value:
            if question.required:
                raise SurveyDomainError(
                    "answer_required",
                    f'An answer is required for "{question.label}".',
                    kind="invalid",
                )
            continue
        if question.type is SurveyQuestionType.rating:
            try:
                numeric = int(value)
            except ValueError as exc:
                raise SurveyDomainError(
                    "rating_invalid",
                    f'"{question.label}" requires a rating from 1 through 5.',
                    kind="invalid",
                ) from exc
            if str(numeric) != value or not 1 <= numeric <= 5:
                raise SurveyDomainError(
                    "rating_invalid",
                    f'"{question.label}" requires a rating from 1 through 5.',
                    kind="invalid",
                )
            rating = rating if rating is not None else numeric
        elif question.type is SurveyQuestionType.nps:
            try:
                numeric = int(value)
            except ValueError as exc:
                raise SurveyDomainError(
                    "nps_invalid",
                    f'"{question.label}" requires a score from 0 through 10.',
                    kind="invalid",
                ) from exc
            if str(numeric) != value or not 0 <= numeric <= 10:
                raise SurveyDomainError(
                    "nps_invalid",
                    f'"{question.label}" requires a score from 0 through 10.',
                    kind="invalid",
                )
            nps_value = nps_value if nps_value is not None else numeric
        elif question.type is SurveyQuestionType.multiple_choice:
            if question.options is None or value not in question.options:
                raise SurveyDomainError(
                    "choice_invalid",
                    f'Choose a configured option for "{question.label}".',
                    kind="invalid",
                )
        elif len(value) > 10_000:
            raise SurveyDomainError(
                "free_text_too_long",
                f'"{question.label}" cannot exceed 10,000 characters.',
                kind="invalid",
            )
        normalized[question.key] = value
    if rating is None and legacy_rating is not None:
        if not 1 <= legacy_rating <= 5:
            raise SurveyDomainError(
                "rating_invalid",
                "The overall rating must be from 1 through 5.",
                kind="invalid",
            )
        rating = legacy_rating
    if nps_value is None and legacy_nps_value is not None:
        if not 0 <= legacy_nps_value <= 10:
            raise SurveyDomainError(
                "nps_invalid",
                "The overall NPS score must be from 0 through 10.",
                kind="invalid",
            )
        nps_value = legacy_nps_value
    return normalized, rating, nps_value


def _refresh_survey_projections(db: Session, survey: Survey) -> SurveyProjectionOutcome:
    survey.total_invited = int(
        db.query(func.count(SurveyInvitation.id))
        .filter(SurveyInvitation.survey_id == survey.id)
        .scalar()
        or 0
    )
    survey.total_responses = int(
        db.query(func.count(SurveyResponse.id))
        .filter(SurveyResponse.survey_id == survey.id)
        .scalar()
        or 0
    )
    average = (
        db.query(func.avg(SurveyResponse.rating))
        .filter(SurveyResponse.survey_id == survey.id)
        .filter(SurveyResponse.rating.isnot(None))
        .scalar()
    )
    survey.avg_rating = (
        Decimal(str(average)).quantize(Decimal("0.01")) if average is not None else None
    )
    nps_values = [
        int(value)
        for (value,) in db.query(SurveyResponse.nps_value)
        .filter(SurveyResponse.survey_id == survey.id)
        .filter(SurveyResponse.nps_value.isnot(None))
        .all()
    ]
    if nps_values:
        promoters = sum(value >= 9 for value in nps_values)
        detractors = sum(value <= 6 for value in nps_values)
        survey.nps_score = Decimal(
            str(((promoters - detractors) / len(nps_values)) * 100)
        ).quantize(Decimal("0.01"))
    else:
        survey.nps_score = None
    return SurveyProjectionOutcome(
        survey_id=survey.id,
        total_invited=survey.total_invited,
        total_responses=survey.total_responses,
        avg_rating=survey.avg_rating,
        nps_score=survey.nps_score,
    )


def submit_response(
    db: Session, command: SubmitSurveyResponseCommand
) -> SurveyResponseOutcome:
    def operation() -> SurveyResponseOutcome:
        invitation: SurveyInvitation | None = None
        if command.invitation_token:
            invitation = (
                db.query(SurveyInvitation)
                .filter(SurveyInvitation.token == command.invitation_token)
                .with_for_update()
                .one_or_none()
            )
            if (
                invitation is None
                or invitation.status is not SurveyInvitationStatus.pending
            ):
                raise SurveyDomainError(
                    "invitation_unavailable",
                    "This Survey invitation is unavailable.",
                    kind="not_found",
                )
            survey_query = db.query(Survey).filter(Survey.id == invitation.survey_id)
        elif command.public_reference:
            survey_id = _try_coerce_uuid(command.public_reference)
            survey_query = db.query(Survey)
            if survey_id is None:
                survey_query = survey_query.filter(
                    Survey.public_slug == command.public_reference.strip().lower()
                )
            else:
                survey_query = survey_query.filter(
                    or_(
                        Survey.id == survey_id,
                        Survey.public_slug == command.public_reference,
                    )
                )
        else:
            raise SurveyDomainError(
                "survey_reference_required",
                "A Survey reference is required.",
                kind="invalid",
            )
        survey = _public_filter(survey_query).with_for_update().one_or_none()
        if survey is None:
            raise SurveyDomainError(
                "survey_unavailable",
                "This Survey is unavailable.",
                kind="not_found",
            )
        questions = _require_distributable(survey)
        responses, rating, nps_value = _validated_answers(
            questions,
            command.answers,
            legacy_rating=command.legacy_rating,
            legacy_nps_value=command.legacy_nps_value,
        )
        work_order_id = command.work_order_id
        ticket_id = command.ticket_id
        if invitation is not None:
            if invitation.source_type is SurveyTriggerType.work_order_completed:
                work_order_id = invitation.source_entity_id
            elif invitation.source_type is SurveyTriggerType.ticket_closed:
                ticket_id = invitation.source_entity_id
        response = SurveyResponse(
            survey_id=survey.id,
            invitation_id=invitation.id if invitation else None,
            work_order_id=work_order_id,
            ticket_id=ticket_id,
            responses=responses,
            rating=rating,
            nps_value=nps_value,
        )
        db.add(response)
        if invitation is not None:
            invitation.status = SurveyInvitationStatus.completed
            invitation.completed_at = _now()
        db.flush()
        _refresh_survey_projections(db, survey)
        stage_audit_event(
            db,
            action="survey.response_recorded",
            entity_type="survey_response",
            entity_id=str(response.id),
            actor_type=AuditActorType.service,
            actor_id=OWNER,
            request_id=command.context.idempotency_key,
            metadata={
                "owner": OWNER,
                "survey_id": str(survey.id),
                "invitation_id": str(invitation.id) if invitation else None,
            },
        )
        emit_event(
            db,
            EventType.custom,
            {
                "name": "survey.response_recorded",
                "survey_id": str(survey.id),
                "response_id": str(response.id),
                "invitation_id": str(invitation.id) if invitation else None,
            },
            actor=OWNER,
        )
        return SurveyResponseOutcome(
            survey_id=survey.id,
            response_id=response.id,
            survey_name=survey.name,
            thank_you_message=survey.thank_you_message,
        )

    try:
        return execute_owner_command(
            db,
            definition=_RESPONSE_DEFINITION,
            context=command.context,
            operation=operation,
        )
    except IntegrityError as exc:
        if "invitation_id" in _constraint_name(exc):
            raise SurveyDomainError(
                "invitation_completed",
                "This Survey invitation has already been completed.",
                kind="conflict",
            ) from exc
        raise


def rebuild_survey_projections(
    db: Session, command: RebuildSurveyProjectionsCommand
) -> SurveyProjectionOutcome:
    """Idempotently rebuild Survey metrics from invitation/response records."""

    def operation() -> SurveyProjectionOutcome:
        survey = (
            db.query(Survey)
            .filter(Survey.id == command.survey_id)
            .with_for_update()
            .one_or_none()
        )
        if survey is None:
            raise SurveyDomainError(
                "survey_not_found", "Survey not found.", kind="not_found"
            )
        outcome = _refresh_survey_projections(db, survey)
        db.flush()
        stage_audit_event(
            db,
            action="survey.projections_rebuilt",
            entity_type="survey",
            entity_id=str(survey.id),
            actor_type=AuditActorType.service,
            actor_id=command.context.actor,
            request_id=command.context.idempotency_key,
            metadata={
                "owner": OWNER,
                "total_invited": outcome.total_invited,
                "total_responses": outcome.total_responses,
            },
        )
        return outcome

    return execute_owner_command(
        db,
        definition=_REPAIR_DEFINITION,
        context=command.context,
        operation=operation,
    )


def get_survey(db: Session, survey_id: str | UUID) -> Survey:
    resolved = coerce_uuid(survey_id)
    survey = db.get(Survey, resolved) if resolved else None
    if survey is None:
        raise SurveyDomainError(
            "survey_not_found", "Survey not found.", kind="not_found"
        )
    return survey


def list_surveys(
    db: Session,
    is_active: bool | None = None,
    order_by: str = "created_at",
    order_dir: str = "desc",
    limit: int = 50,
    offset: int = 0,
    *,
    include_all: bool = False,
) -> list[Survey]:
    query = db.query(Survey)
    if is_active is None and not include_all:
        query = query.filter(Survey.is_active.is_(True))
    elif is_active is not None:
        query = query.filter(Survey.is_active == is_active)
    query = apply_ordering(
        query,
        order_by,
        order_dir,
        {"created_at": Survey.created_at, "name": Survey.name},
    )
    return apply_pagination(query, limit, offset).all()


def get_response(db: Session, response_id: str | UUID) -> SurveyResponse:
    resolved = coerce_uuid(response_id)
    response = db.get(SurveyResponse, resolved) if resolved else None
    if response is None:
        raise SurveyDomainError(
            "response_not_found", "Survey response not found.", kind="not_found"
        )
    return response


def list_responses(
    db: Session,
    survey_id: str | UUID | None = None,
    order_by: str = "created_at",
    order_dir: str = "desc",
    limit: int = 50,
    offset: int = 0,
) -> list[SurveyResponse]:
    query = db.query(SurveyResponse)
    resolved_survey_id = coerce_uuid(survey_id)
    if survey_id is not None and resolved_survey_id is None:
        return []
    if resolved_survey_id is not None:
        query = query.filter(SurveyResponse.survey_id == resolved_survey_id)
    query = apply_ordering(
        query,
        order_by,
        order_dir,
        {"created_at": SurveyResponse.created_at},
    )
    return apply_pagination(query, limit, offset).all()
