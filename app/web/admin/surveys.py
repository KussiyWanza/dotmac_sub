"""Admin pages for the existing communications survey service."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.comms import SurveyCreate, SurveyUpdate
from app.services import comms as comms_service
from app.services.auth_dependencies import require_permission
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


def _parse_questions(raw: str) -> list[dict[str, object]]:
    parsed = json.loads(raw or "[]")
    if not isinstance(parsed, list):
        raise ValueError("Questions must be a JSON list.")
    return [item for item in parsed if isinstance(item, dict)]


@router.get(
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:conversation:read"))],
)
def survey_list(request: Request, db: Session = Depends(get_db)):
    surveys = comms_service.surveys.list(db, None, "created_at", "desc", 200, 0)
    return templates.TemplateResponse(
        "admin/surveys/index.html",
        _context(request, db, surveys=surveys),
    )


@router.get(
    "/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:conversation:write"))],
)
def survey_new(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        "admin/surveys/form.html",
        _context(request, db, survey=None, error=None),
    )


@router.post(
    "",
    dependencies=[Depends(require_permission("crm:conversation:write"))],
)
def survey_create(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    questions_json: str = Form("[]"),
    db: Session = Depends(get_db),
):
    try:
        payload = SurveyCreate(
            name=name.strip(),
            description=description.strip() or None,
            questions=_parse_questions(questions_json),
            is_active=True,
        )
    except (ValueError, json.JSONDecodeError, ValidationError) as exc:
        return templates.TemplateResponse(
            "admin/surveys/form.html",
            _context(request, db, survey=None, error=str(exc)),
            status_code=400,
        )
    survey = comms_service.surveys.create(db, payload)
    return RedirectResponse(f"/admin/surveys/{survey.id}", status_code=303)


@router.get(
    "/{survey_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:conversation:read"))],
)
def survey_detail(request: Request, survey_id: str, db: Session = Depends(get_db)):
    survey = comms_service.surveys.get(db, survey_id)
    responses = comms_service.survey_responses.list(
        db, survey_id=survey_id, limit=200, offset=0
    )
    return templates.TemplateResponse(
        "admin/surveys/detail.html",
        _context(request, db, survey=survey, responses=responses),
    )


@router.get(
    "/{survey_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:conversation:write"))],
)
def survey_edit(request: Request, survey_id: str, db: Session = Depends(get_db)):
    survey = comms_service.surveys.get(db, survey_id)
    return templates.TemplateResponse(
        "admin/surveys/form.html",
        _context(request, db, survey=survey, error=None),
    )


@router.post(
    "/{survey_id}",
    dependencies=[Depends(require_permission("crm:conversation:write"))],
)
def survey_update(
    request: Request,
    survey_id: str,
    name: str = Form(...),
    description: str = Form(""),
    questions_json: str = Form("[]"),
    is_active: str | None = Form(None),
    db: Session = Depends(get_db),
):
    survey = comms_service.surveys.get(db, survey_id)
    try:
        payload = SurveyUpdate(
            name=name.strip(),
            description=description.strip() or None,
            questions=_parse_questions(questions_json),
            is_active=is_active == "on",
        )
    except (ValueError, json.JSONDecodeError, ValidationError) as exc:
        return templates.TemplateResponse(
            "admin/surveys/form.html",
            _context(request, db, survey=survey, error=str(exc)),
            status_code=400,
        )
    comms_service.surveys.update(db, survey_id, payload)
    return RedirectResponse(f"/admin/surveys/{survey_id}", status_code=303)


@router.post(
    "/{survey_id}/archive",
    dependencies=[Depends(require_permission("crm:conversation:write"))],
)
def survey_archive(survey_id: str, db: Session = Depends(get_db)):
    comms_service.surveys.delete(db, survey_id)
    return RedirectResponse("/admin/surveys", status_code=303)
