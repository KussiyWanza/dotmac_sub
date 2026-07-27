"""Thin admin adapters for the native agent-workqueue owner."""

from __future__ import annotations

import json
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.responses import Response

from app.db import get_db
from app.services.auth_dependencies import require_permission
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext
from app.services.workqueue import principal_from_auth
from app.services.workqueue.commands import (
    SnoozeMode,
    WorkqueueActionCommand,
    execute_action,
)
from app.services.workqueue.types import ActionKind, ItemKind
from app.services.workqueue.web import build_page

router = APIRouter(prefix="/workqueue", tags=["web-admin-workqueue"])
templates = Jinja2Templates(directory="templates")

READ_PERMISSION = "support:ticket:read"
ACT_PERMISSION = "support:ticket:update"


def _base_context(request: Request, db: Session) -> dict[str, object]:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": "agent-workqueue",
        "active_menu": "support",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


def _filters(
    *,
    audience: str | None,
    service_team_id: UUID | None,
    include_snoozed: bool,
) -> dict[str, str]:
    values: dict[str, str] = {}
    if audience:
        values["audience"] = audience
    if service_team_id:
        values["service_team_id"] = str(service_team_id)
    if include_snoozed:
        values["include_snoozed"] = "true"
    return values


def _page_url(
    *,
    audience: str | None,
    service_team_id: UUID | None,
    include_snoozed: bool,
    message: str | None = None,
    error: str | None = None,
) -> str:
    values = _filters(
        audience=audience,
        service_team_id=service_team_id,
        include_snoozed=include_snoozed,
    )
    if message:
        values["message"] = message
    if error:
        values["error"] = error
    query = urlencode(values)
    return f"/admin/workqueue?{query}" if query else "/admin/workqueue"


def _command_context(
    auth: dict,
    *,
    request_id: UUID,
    reason: str,
) -> CommandContext:
    principal_id = str(auth.get("principal_id") or auth.get("person_id") or "").strip()
    actor_type = "api_key" if auth.get("principal_type") == "api_key" else "user"
    return CommandContext(
        command_id=request_id,
        correlation_id=request_id,
        actor=f"{actor_type}:{principal_id}",
        scope=ACT_PERMISSION,
        reason=reason,
        idempotency_key=str(request_id),
    )


def _action_response(
    request: Request,
    *,
    audience: str | None,
    service_team_id: UUID | None,
    include_snoozed: bool,
    message: str | None = None,
    error: str | None = None,
) -> Response:
    if request.headers.get("HX-Request") == "true":
        if error:
            return templates.TemplateResponse(
                "admin/workqueue/_feedback.html",
                {"request": request, "error": error, "message": None},
            )
        trigger = json.dumps(
            {
                "workqueue-refresh": {},
                "showToast": {
                    "message": message or "Workqueue updated",
                    "type": "success",
                },
            }
        )
        return HTMLResponse(
            "",
            status_code=204,
            headers={"HX-Trigger": trigger},
        )
    return RedirectResponse(
        url=_page_url(
            audience=audience,
            service_team_id=service_team_id,
            include_snoozed=include_snoozed,
            message=message,
            error=error,
        ),
        status_code=303,
    )


def _execute(
    request: Request,
    db: Session,
    auth: dict,
    *,
    item_kind: ItemKind,
    item_id: UUID,
    action: ActionKind,
    request_id: UUID,
    audience: str | None,
    service_team_id: UUID | None,
    include_snoozed: bool,
    reason: str,
    snooze_mode: SnoozeMode | None = None,
    state_fingerprint: str | None = None,
    confirmed: bool = False,
) -> Response:
    try:
        outcome = execute_action(
            db,
            WorkqueueActionCommand(
                context=_command_context(auth, request_id=request_id, reason=reason),
                principal=principal_from_auth(db, auth),
                item_kind=item_kind,
                item_id=item_id,
                action=action,
                requested_audience=audience,
                service_team_id=service_team_id,
                snooze_mode=snooze_mode,
                state_fingerprint=state_fingerprint,
                confirmed=confirmed,
            ),
        )
    except (DomainError, ValueError) as exc:
        message = exc.message if isinstance(exc, DomainError) else str(exc)
        return _action_response(
            request,
            audience=audience,
            service_team_id=service_team_id,
            include_snoozed=include_snoozed,
            error=message,
        )
    labels = {
        ActionKind.snooze: "Item snoozed",
        ActionKind.clear_snooze: "Item restored to the queue",
        ActionKind.claim: "Item claimed",
        ActionKind.complete: "Item completed",
    }
    return _action_response(
        request,
        audience=audience,
        service_team_id=service_team_id,
        include_snoozed=include_snoozed,
        message=labels.get(outcome.action, "Workqueue updated"),
    )


@router.get(
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission(READ_PERMISSION))],
)
def workqueue_page(
    request: Request,
    audience: str | None = Query(default=None),
    service_team_id: UUID | None = Query(default=None),
    include_snoozed: bool = Query(default=False),
    message: str | None = Query(default=None),
    error: str | None = Query(default=None),
    db: Session = Depends(get_db),
    auth: dict = Depends(require_permission(READ_PERMISSION)),
):
    projection = build_page(
        db,
        principal_from_auth(db, auth),
        requested_audience=audience,
        service_team_id=service_team_id,
        include_snoozed=include_snoozed,
    )
    return templates.TemplateResponse(
        "admin/workqueue/index.html",
        {
            **_base_context(request, db),
            "projection": projection,
            "audience_filter": audience,
            "message": message,
            "error": error,
        },
    )


@router.get(
    "/_right-now",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission(READ_PERMISSION))],
)
def workqueue_right_now(
    request: Request,
    audience: str | None = Query(default=None),
    service_team_id: UUID | None = Query(default=None),
    include_snoozed: bool = Query(default=False),
    db: Session = Depends(get_db),
    auth: dict = Depends(require_permission(READ_PERMISSION)),
):
    projection = build_page(
        db,
        principal_from_auth(db, auth),
        requested_audience=audience,
        service_team_id=service_team_id,
        include_snoozed=include_snoozed,
    )
    return templates.TemplateResponse(
        "admin/workqueue/_right_now.html",
        {
            "request": request,
            "projection": projection,
            "audience_filter": audience,
        },
    )


@router.get(
    "/_section/{kind}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission(READ_PERMISSION))],
)
def workqueue_section(
    request: Request,
    kind: ItemKind,
    audience: str | None = Query(default=None),
    service_team_id: UUID | None = Query(default=None),
    include_snoozed: bool = Query(default=False),
    db: Session = Depends(get_db),
    auth: dict = Depends(require_permission(READ_PERMISSION)),
):
    projection = build_page(
        db,
        principal_from_auth(db, auth),
        requested_audience=audience,
        service_team_id=service_team_id,
        include_snoozed=include_snoozed,
    )
    section = next(
        section for section in projection.sections if section.item_kind is kind
    )
    return templates.TemplateResponse(
        "admin/workqueue/_section.html",
        {
            "request": request,
            "projection": projection,
            "section": section,
            "audience_filter": audience,
        },
    )


@router.post("/snooze", response_class=HTMLResponse)
def workqueue_snooze(
    request: Request,
    item_kind: ItemKind = Form(...),
    item_id: UUID = Form(...),
    request_id: UUID = Form(...),
    snooze_mode: SnoozeMode = Form(...),
    audience: str | None = Form(default=None),
    service_team_id: UUID | None = Form(default=None),
    include_snoozed: bool = Form(default=False),
    db: Session = Depends(get_db),
    auth: dict = Depends(require_permission(READ_PERMISSION)),
):
    return _execute(
        request,
        db,
        auth,
        item_kind=item_kind,
        item_id=item_id,
        action=ActionKind.snooze,
        request_id=request_id,
        audience=audience,
        service_team_id=service_team_id,
        include_snoozed=include_snoozed,
        reason="Snooze personal workqueue item",
        snooze_mode=snooze_mode,
    )


@router.post("/snooze/clear", response_class=HTMLResponse)
def workqueue_clear_snooze(
    request: Request,
    item_kind: ItemKind = Form(...),
    item_id: UUID = Form(...),
    request_id: UUID = Form(...),
    audience: str | None = Form(default=None),
    service_team_id: UUID | None = Form(default=None),
    include_snoozed: bool = Form(default=False),
    db: Session = Depends(get_db),
    auth: dict = Depends(require_permission(READ_PERMISSION)),
):
    return _execute(
        request,
        db,
        auth,
        item_kind=item_kind,
        item_id=item_id,
        action=ActionKind.clear_snooze,
        request_id=request_id,
        audience=audience,
        service_team_id=service_team_id,
        include_snoozed=include_snoozed,
        reason="Restore personal workqueue item",
    )


@router.post("/claim", response_class=HTMLResponse)
def workqueue_claim(
    request: Request,
    item_kind: ItemKind = Form(...),
    item_id: UUID = Form(...),
    request_id: UUID = Form(...),
    state_fingerprint: str = Form(...),
    audience: str | None = Form(default=None),
    service_team_id: UUID | None = Form(default=None),
    include_snoozed: bool = Form(default=False),
    db: Session = Depends(get_db),
    auth: dict = Depends(require_permission(ACT_PERMISSION)),
):
    return _execute(
        request,
        db,
        auth,
        item_kind=item_kind,
        item_id=item_id,
        action=ActionKind.claim,
        request_id=request_id,
        audience=audience,
        service_team_id=service_team_id,
        include_snoozed=include_snoozed,
        reason="Claim native workqueue item",
        state_fingerprint=state_fingerprint,
    )


@router.post("/complete", response_class=HTMLResponse)
def workqueue_complete(
    request: Request,
    item_kind: ItemKind = Form(...),
    item_id: UUID = Form(...),
    request_id: UUID = Form(...),
    state_fingerprint: str = Form(...),
    confirmed: bool = Form(default=False),
    audience: str | None = Form(default=None),
    service_team_id: UUID | None = Form(default=None),
    include_snoozed: bool = Form(default=False),
    db: Session = Depends(get_db),
    auth: dict = Depends(require_permission(ACT_PERMISSION)),
):
    return _execute(
        request,
        db,
        auth,
        item_kind=item_kind,
        item_id=item_id,
        action=ActionKind.complete,
        request_id=request_id,
        audience=audience,
        service_team_id=service_team_id,
        include_snoozed=include_snoozed,
        reason="Complete native workqueue item through its lifecycle owner",
        state_fingerprint=state_fingerprint,
        confirmed=confirmed,
    )
