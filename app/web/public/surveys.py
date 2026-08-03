"""Public Survey adapters backed by the authoritative Survey service."""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import surveys as survey_service
from app.services.owner_commands import CommandContext
from app.web.templates import templates

router = APIRouter(prefix="/s", tags=["web-public-surveys"])


def _unavailable(request: Request):
    return templates.TemplateResponse(
        "public/surveys/unavailable.html",
        {"request": request},
        status_code=410,
    )


def _response_context(reason: str) -> CommandContext:
    command_id = uuid4()
    return CommandContext.system(
        actor="public:anonymous",
        scope="communications.surveys:respond",
        reason=reason,
        command_id=command_id,
        correlation_id=command_id,
        idempotency_key=f"public-response:{command_id}",
    )


async def _answers(request: Request) -> tuple[survey_service.SurveyAnswer, ...]:
    form = await request.form()
    return tuple(
        survey_service.SurveyAnswer(key=str(key), value=str(value))
        for key, value in form.multi_items()
        if key != "_csrf_token"
    )


@router.get("/t/{token}", response_class=HTMLResponse)
def tracked_survey_page(request: Request, token: str, db: Session = Depends(get_db)):
    try:
        _invitation, survey = survey_service.get_invitation_survey(db, token)
    except survey_service.SurveyDomainError:
        return _unavailable(request)
    return templates.TemplateResponse(
        "public/surveys/respond.html",
        {"request": request, "survey": survey, "error": None, "answers": {}},
    )


@router.post("/t/{token}", response_class=HTMLResponse)
async def tracked_survey_submit(
    request: Request, token: str, db: Session = Depends(get_db)
):
    answers = await _answers(request)
    try:
        outcome = survey_service.submit_response(
            db,
            survey_service.SubmitSurveyResponseCommand(
                public_reference=None,
                invitation_token=token,
                answers=answers,
                work_order_id=None,
                ticket_id=None,
                context=_response_context("record invitation Survey response"),
            ),
        )
    except survey_service.SurveyDomainError as exc:
        if exc.kind in {"not_found", "conflict"}:
            return _unavailable(request)
        try:
            _invitation, survey = survey_service.get_invitation_survey(db, token)
        except survey_service.SurveyDomainError:
            return _unavailable(request)
        return templates.TemplateResponse(
            "public/surveys/respond.html",
            {
                "request": request,
                "survey": survey,
                "error": exc.message,
                "answers": {answer.key: answer.value for answer in answers},
            },
            status_code=400,
        )
    return templates.TemplateResponse(
        "public/surveys/thank_you.html",
        {
            "request": request,
            "survey_name": outcome.survey_name,
            "thank_you_message": outcome.thank_you_message,
        },
        status_code=201,
    )


@router.get("/{reference}", response_class=HTMLResponse)
def survey_response_page(
    request: Request, reference: str, db: Session = Depends(get_db)
):
    try:
        survey = survey_service.get_public_survey(db, reference)
    except survey_service.SurveyDomainError:
        return _unavailable(request)
    return templates.TemplateResponse(
        "public/surveys/respond.html",
        {"request": request, "survey": survey, "error": None, "answers": {}},
    )


@router.post("/{reference}", response_class=HTMLResponse)
async def survey_response_submit(
    request: Request, reference: str, db: Session = Depends(get_db)
):
    answers = await _answers(request)
    try:
        outcome = survey_service.submit_response(
            db,
            survey_service.SubmitSurveyResponseCommand(
                public_reference=reference,
                invitation_token=None,
                answers=answers,
                work_order_id=None,
                ticket_id=None,
                context=_response_context("record public Survey response"),
            ),
        )
    except survey_service.SurveyDomainError as exc:
        if exc.kind in {"not_found", "conflict"}:
            return _unavailable(request)
        try:
            survey = survey_service.get_public_survey(db, reference)
        except survey_service.SurveyDomainError:
            return _unavailable(request)
        return templates.TemplateResponse(
            "public/surveys/respond.html",
            {
                "request": request,
                "survey": survey,
                "error": exc.message,
                "answers": {answer.key: answer.value for answer in answers},
            },
            status_code=400,
        )
    return templates.TemplateResponse(
        "public/surveys/thank_you.html",
        {
            "request": request,
            "survey_name": outcome.survey_name,
            "thank_you_message": outcome.thank_you_message,
        },
        status_code=201,
    )
