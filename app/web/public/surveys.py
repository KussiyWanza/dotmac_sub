"""Public survey response pages backed by the communications survey service."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.comms import SurveyResponseCreate
from app.services import comms as comms_service
from app.web.templates import templates

router = APIRouter(prefix="/s", tags=["web-public-surveys"])


@router.get("/{survey_id}", response_class=HTMLResponse)
def survey_response_page(
    request: Request, survey_id: str, db: Session = Depends(get_db)
):
    survey = comms_service.surveys.get(db, survey_id)
    if not survey.is_active:
        return templates.TemplateResponse(
            "public/surveys/unavailable.html",
            {"request": request},
            status_code=410,
        )
    return templates.TemplateResponse(
        "public/surveys/respond.html",
        {"request": request, "survey": survey},
    )


@router.post("/{survey_id}", response_class=HTMLResponse)
async def survey_response_submit(
    request: Request,
    survey_id: str,
    rating: int | None = Form(None),
    db: Session = Depends(get_db),
):
    survey = comms_service.surveys.get(db, survey_id)
    if not survey.is_active:
        return templates.TemplateResponse(
            "public/surveys/unavailable.html",
            {"request": request},
            status_code=410,
        )
    form = await request.form()
    answers: dict[str, str | list[str]] = {}
    for key in form:
        if key in {"rating", "_csrf_token"}:
            continue
        values = [str(value).strip() for value in form.getlist(key)]
        answers[str(key)] = values if len(values) > 1 else values[0]
    comms_service.survey_responses.create(
        db,
        SurveyResponseCreate(
            survey_id=survey.id,
            responses=answers,
            rating=rating,
        ),
    )
    return templates.TemplateResponse(
        "public/surveys/thank_you.html",
        {"request": request, "survey": survey},
        status_code=201,
    )
