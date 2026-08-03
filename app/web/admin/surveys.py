"""Thin admin adapters for the authoritative Survey service."""

from __future__ import annotations

from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.comms import SurveyTriggerType
from app.schemas.comms import SurveyUpdate
from app.services import surveys as survey_service
from app.services.auth_dependencies import require_permission
from app.services.owner_commands import CommandContext
from app.web.templates import templates

router = APIRouter(prefix="/surveys", tags=["web-admin-surveys"])


def _context(request: Request, db: Session, **values: object) -> dict[str, object]:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": "surveys",
        "active_menu": "services",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        **values,
    }


def _actor(request: Request) -> tuple[UUID, str]:
    auth = getattr(request.state, "auth", {})
    try:
        principal_id = UUID(str(auth.get("principal_id") or ""))
    except ValueError as exc:
        raise HTTPException(
            status_code=403,
            detail="A resolved administrator is required to manage Surveys.",
        ) from exc
    return principal_id, str(auth.get("principal_type") or "")


def _context_for(
    request: Request,
    *,
    reason: str,
    idempotency_key: str | None = None,
    command_id: UUID | None = None,
) -> CommandContext:
    principal_id, principal_type = _actor(request)
    return CommandContext.system(
        actor=f"{principal_type}:{principal_id}",
        scope="communications.surveys:write",
        reason=reason,
        command_id=command_id,
        correlation_id=command_id,
        idempotency_key=idempotency_key,
    )


def _render_form(
    request: Request,
    db: Session,
    *,
    survey: object | None,
    validation: survey_service.SurveyFormValidation,
    status_code: int = 200,
):
    return templates.TemplateResponse(
        "admin/surveys/form.html",
        _context(
            request,
            db,
            survey=survey,
            form=validation.values,
            questions_seed=validation.questions_seed,
            errors=validation.errors,
            field_errors=validation.field_errors,
        ),
        status_code=status_code,
    )


def _form_error(
    validation: survey_service.SurveyFormValidation,
    exc: survey_service.SurveyDomainError,
) -> survey_service.SurveyFormValidation:
    field_errors = dict(validation.field_errors)
    if exc.field:
        field_errors[exc.field] = exc.message
    return survey_service.SurveyFormValidation(
        values=validation.values,
        questions_seed=validation.questions_seed,
        payload=None,
        errors=(*validation.errors, exc.message),
        field_errors=field_errors,
    )


def _submitted_values(
    *,
    name: str,
    description: str,
    trigger_type: str,
    public_slug: str,
    thank_you_message: str,
    questions_json: str,
    idempotency_key: str,
) -> survey_service.SurveyFormValues:
    return survey_service.SurveyFormValues(
        name=name,
        description=description,
        trigger_type=trigger_type,
        public_slug=public_slug,
        thank_you_message=thank_you_message,
        questions_json=questions_json,
        idempotency_key=idempotency_key,
    )


@router.get("", response_class=HTMLResponse)
def survey_list(request: Request, db: Session = Depends(get_db)):
    surveys = survey_service.list_surveys(
        db, None, "created_at", "desc", 200, 0, include_all=True
    )
    return templates.TemplateResponse(
        "admin/surveys/index.html",
        _context(request, db, surveys=surveys),
    )


@router.get("/new", response_class=HTMLResponse)
def survey_new(request: Request, db: Session = Depends(get_db)):
    values = survey_service.form_values_for_survey(None, idempotency_key=str(uuid4()))
    validation = survey_service.SurveyFormValidation(
        values=values,
        questions_seed=(),
        payload=None,
        errors=(),
        field_errors={},
    )
    return _render_form(request, db, survey=None, validation=validation)


@router.post("", dependencies=[Depends(require_permission("customer:write"))])
def survey_create(
    request: Request,
    name: str = Form(""),
    description: str = Form(""),
    trigger_type: str = Form(SurveyTriggerType.manual.value),
    public_slug: str = Form(""),
    thank_you_message: str = Form(""),
    questions_json: str = Form("[]"),
    idempotency_key: str = Form(""),
    db: Session = Depends(get_db),
):
    values = _submitted_values(
        name=name,
        description=description,
        trigger_type=trigger_type,
        public_slug=public_slug,
        thank_you_message=thank_you_message,
        questions_json=questions_json,
        idempotency_key=idempotency_key,
    )
    validation = survey_service.validate_form(values)
    if validation.payload is None:
        return _render_form(
            request, db, survey=None, validation=validation, status_code=400
        )
    try:
        stable_id = UUID(idempotency_key)
    except ValueError:
        error = survey_service.SurveyDomainError(
            "idempotency_key_invalid",
            "The form submission key is invalid. Reload the page and try again.",
            kind="invalid",
        )
        return _render_form(
            request,
            db,
            survey=None,
            validation=_form_error(validation, error),
            status_code=400,
        )
    principal_id, principal_type = _actor(request)
    try:
        outcome = survey_service.create_survey(
            db,
            survey_service.CreateSurveyCommand(
                payload=validation.payload,
                principal_id=principal_id,
                principal_type=principal_type,
                context=_context_for(
                    request,
                    reason="create Survey from admin form",
                    idempotency_key=idempotency_key,
                    command_id=stable_id,
                ),
            ),
        )
    except survey_service.SurveyDomainError as exc:
        return _render_form(
            request,
            db,
            survey=None,
            validation=_form_error(validation, exc),
            status_code=400 if exc.kind == "invalid" else 403,
        )
    return RedirectResponse(f"/admin/surveys/{outcome.survey_id}", status_code=303)


@router.get("/{survey_id}", response_class=HTMLResponse)
def survey_detail(request: Request, survey_id: str, db: Session = Depends(get_db)):
    try:
        survey = survey_service.get_survey(db, survey_id)
    except survey_service.SurveyDomainError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc
    responses = survey_service.list_responses(
        db, survey_id=survey.id, limit=200, offset=0
    )
    return templates.TemplateResponse(
        "admin/surveys/detail.html",
        _context(request, db, survey=survey, responses=responses, error=None),
    )


@router.get("/{survey_id}/edit", response_class=HTMLResponse)
def survey_edit(request: Request, survey_id: str, db: Session = Depends(get_db)):
    try:
        survey = survey_service.get_survey(db, survey_id)
    except survey_service.SurveyDomainError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc
    values = survey_service.form_values_for_survey(survey, idempotency_key=str(uuid4()))
    validation = survey_service.SurveyFormValidation(
        values=values,
        questions_seed=tuple(survey.questions or []),
        payload=None,
        errors=(),
        field_errors={},
    )
    return _render_form(request, db, survey=survey, validation=validation)


@router.post(
    "/{survey_id}", dependencies=[Depends(require_permission("customer:write"))]
)
def survey_update(
    request: Request,
    survey_id: str,
    name: str = Form(""),
    description: str = Form(""),
    trigger_type: str = Form(SurveyTriggerType.manual.value),
    public_slug: str = Form(""),
    thank_you_message: str = Form(""),
    questions_json: str = Form("[]"),
    idempotency_key: str = Form(""),
    db: Session = Depends(get_db),
):
    values = _submitted_values(
        name=name,
        description=description,
        trigger_type=trigger_type,
        public_slug=public_slug,
        thank_you_message=thank_you_message,
        questions_json=questions_json,
        idempotency_key=idempotency_key,
    )
    validation = survey_service.validate_form(values)
    try:
        resolved_survey_id = UUID(survey_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Survey not found.") from exc
    if validation.payload is None:
        survey = survey_service.get_survey(db, resolved_survey_id)
        return _render_form(
            request, db, survey=survey, validation=validation, status_code=400
        )
    payload = SurveyUpdate(
        name=validation.payload.name,
        description=validation.payload.description,
        trigger_type=validation.payload.trigger_type,
        public_slug=validation.payload.public_slug,
        thank_you_message=validation.payload.thank_you_message,
        questions=validation.payload.questions,
    )
    try:
        survey_service.update_survey(
            db,
            survey_service.UpdateSurveyCommand(
                survey_id=resolved_survey_id,
                payload=payload,
                context=_context_for(
                    request,
                    reason="update Survey from admin form",
                    idempotency_key=idempotency_key or None,
                ),
            ),
        )
    except survey_service.SurveyDomainError as exc:
        survey = survey_service.get_survey(db, resolved_survey_id)
        return _render_form(
            request,
            db,
            survey=survey,
            validation=_form_error(validation, exc),
            status_code=400 if exc.kind == "invalid" else 409,
        )
    return RedirectResponse(f"/admin/surveys/{resolved_survey_id}", status_code=303)


def _transition_response(
    request: Request,
    db: Session,
    *,
    survey_id: str,
    action: survey_service.SurveyLifecycleAction,
):
    try:
        resolved_survey_id = UUID(survey_id)
        survey_service.transition_survey(
            db,
            survey_service.SurveyLifecycleCommand(
                survey_id=resolved_survey_id,
                action=action,
                context=_context_for(
                    request, reason=f"{action.value} Survey from admin detail"
                ),
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Survey not found.") from exc
    except survey_service.SurveyDomainError as exc:
        if exc.kind == "not_found":
            raise HTTPException(status_code=404, detail=exc.message) from exc
        survey = survey_service.get_survey(db, survey_id)
        responses = survey_service.list_responses(db, survey_id=survey.id, limit=200)
        return templates.TemplateResponse(
            "admin/surveys/detail.html",
            _context(
                request, db, survey=survey, responses=responses, error=exc.message
            ),
            status_code=409,
        )
    return RedirectResponse(f"/admin/surveys/{survey_id}", status_code=303)


@router.post(
    "/{survey_id}/activate",
    dependencies=[Depends(require_permission("customer:write"))],
)
def survey_activate(request: Request, survey_id: str, db: Session = Depends(get_db)):
    return _transition_response(
        request,
        db,
        survey_id=survey_id,
        action=survey_service.SurveyLifecycleAction.activate,
    )


@router.post(
    "/{survey_id}/pause",
    dependencies=[Depends(require_permission("customer:write"))],
)
def survey_pause(request: Request, survey_id: str, db: Session = Depends(get_db)):
    return _transition_response(
        request,
        db,
        survey_id=survey_id,
        action=survey_service.SurveyLifecycleAction.pause,
    )


@router.post(
    "/{survey_id}/close",
    dependencies=[Depends(require_permission("customer:write"))],
)
def survey_close(request: Request, survey_id: str, db: Session = Depends(get_db)):
    return _transition_response(
        request,
        db,
        survey_id=survey_id,
        action=survey_service.SurveyLifecycleAction.close,
    )


@router.post(
    "/{survey_id}/archive",
    dependencies=[Depends(require_permission("customer:write"))],
)
def survey_archive(request: Request, survey_id: str, db: Session = Depends(get_db)):
    return _transition_response(
        request,
        db,
        survey_id=survey_id,
        action=survey_service.SurveyLifecycleAction.archive,
    )
