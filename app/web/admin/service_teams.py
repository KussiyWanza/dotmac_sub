"""Thin admin adapters for the native service-team lifecycle owner."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.service_team import ServiceTeamMemberRole, ServiceTeamType
from app.services import service_team_lifecycle
from app.services.auth_dependencies import require_permission
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext

router = APIRouter(prefix="/system/service-teams", tags=["web-admin-service-teams"])
templates = Jinja2Templates(directory="templates")

READ_PERMISSION = "operations:service_team:read"
CREATE_PERMISSION = "operations:service_team:create"
UPDATE_PERMISSION = "operations:service_team:update"
MEMBERSHIP_PERMISSION = "operations:service_team:membership"
RETIRE_PERMISSION = "operations:service_team:retire"


def _context(
    auth: dict,
    *,
    scope: str,
    reason: str,
    request_id: UUID,
) -> CommandContext:
    principal_id = str(auth.get("principal_id") or "").strip()
    if not principal_id:
        raise HTTPException(status_code=403, detail="Authorized actor is missing")
    actor_type = "api_key" if auth.get("principal_type") == "api_key" else "user"
    return CommandContext(
        command_id=request_id,
        correlation_id=request_id,
        actor=f"{actor_type}:{principal_id}",
        scope=scope,
        reason=reason,
        idempotency_key=f"{scope}:{request_id}",
    )


def _base_context(request: Request, db: Session) -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": "service-teams",
        "active_menu": "system",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "service_team_permissions": {
            "create": CREATE_PERMISSION,
            "update": UPDATE_PERMISSION,
            "membership": MEMBERSHIP_PERMISSION,
            "retire": RETIRE_PERMISSION,
        },
    }


def _error_status(error: DomainError) -> int:
    if error.code == "service_team_not_found":
        return 404
    if error.code in {"service_team_stale", "service_team_name_conflict"}:
        return 409
    return 400


def _parse_optional_uuid(value: str | None) -> UUID | None:
    cleaned = str(value or "").strip()
    return UUID(cleaned) if cleaned else None


def _parse_timestamp(value: str) -> datetime:
    try:
        return datetime.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise service_team_lifecycle.ServiceTeamLifecycleError(
            code="service_team_stale",
            message="The lifecycle evidence is invalid; reload the page.",
        ) from exc


def _team_form_response(
    request: Request,
    db: Session,
    *,
    mode: str,
    team=None,
    values: dict[str, str] | None = None,
    error: str | None = None,
    status_code: int = 200,
):
    return templates.TemplateResponse(
        "admin/system/service_teams/form.html",
        {
            **_base_context(request, db),
            "mode": mode,
            "team": team,
            "values": values or {},
            "error": error,
            "request_id": str(uuid4()),
            "team_types": tuple(ServiceTeamType),
            "staff_options": service_team_lifecycle.list_staff_options(db),
        },
        status_code=status_code,
    )


def _detail_response(
    request: Request,
    db: Session,
    team_id: UUID,
    *,
    error: str | None = None,
    status_code: int = 200,
):
    try:
        detail = service_team_lifecycle.get_team(db, team_id)
    except DomainError as exc:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": exc.message},
            status_code=404,
        )
    return templates.TemplateResponse(
        "admin/system/service_teams/detail.html",
        {
            **_base_context(request, db),
            "detail": detail,
            "member_roles": tuple(ServiceTeamMemberRole),
            "error": error,
            "request_ids": {
                "active": str(uuid4()),
                "add_member": str(uuid4()),
                "member_role": {
                    member.member_id: str(uuid4()) for member in detail.members
                },
                "member_remove": {
                    member.member_id: str(uuid4()) for member in detail.members
                },
            },
        },
        status_code=status_code,
    )


@router.get(
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission(READ_PERMISSION))],
)
def service_team_list(
    request: Request,
    q: str | None = Query(default=None),
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=10, le=100),
    db: Session = Depends(get_db),
):
    active_filter = {"active": True, "inactive": False}.get(
        str(status or "").strip().lower()
    )
    result = service_team_lifecycle.list_teams(
        db,
        search=q,
        is_active=active_filter,
        offset=(page - 1) * per_page,
        limit=per_page,
    )
    role_region_groups = service_team_lifecycle.list_role_region_groups(
        db,
        search=q,
    )
    total_pages = max(1, (result.total + per_page - 1) // per_page)
    safe_page = min(page, total_pages)
    if safe_page != page:
        result = service_team_lifecycle.list_teams(
            db,
            search=q,
            is_active=active_filter,
            offset=(safe_page - 1) * per_page,
            limit=per_page,
        )
    return templates.TemplateResponse(
        "admin/system/service_teams/index.html",
        {
            **_base_context(request, db),
            "result": result,
            "status_filter": str(status or ""),
            "page": safe_page,
            "per_page": per_page,
            "total_pages": total_pages,
            "role_region_groups": role_region_groups,
        },
    )


@router.get("/new", response_class=HTMLResponse)
def service_team_new(
    request: Request,
    db: Session = Depends(get_db),
    _auth: dict = Depends(require_permission(CREATE_PERMISSION)),
):
    return _team_form_response(request, db, mode="create")


@router.post("", response_class=HTMLResponse)
def service_team_create(
    request: Request,
    team_id: UUID = Form(...),
    request_id: UUID = Form(...),
    name: str = Form(...),
    team_type: ServiceTeamType = Form(...),
    region: str | None = Form(default=None),
    manager_system_user_id: str | None = Form(default=None),
    db: Session = Depends(get_db),
    auth: dict = Depends(require_permission(CREATE_PERMISSION)),
):
    values = {
        "name": name,
        "team_type": team_type.value,
        "region": str(region or ""),
        "manager_system_user_id": str(manager_system_user_id or ""),
    }
    try:
        db_session_adapter.release_read_transaction(db)
        service_team_lifecycle.create_team(
            db,
            service_team_lifecycle.CreateServiceTeam(
                context=_context(
                    auth,
                    scope=CREATE_PERMISSION,
                    reason="Create service team",
                    request_id=request_id,
                ),
                team_id=team_id,
                name=name,
                team_type=team_type,
                region=region,
                manager_system_user_id=_parse_optional_uuid(manager_system_user_id),
            ),
        )
    except (DomainError, ValueError) as exc:
        message = exc.message if isinstance(exc, DomainError) else str(exc)
        return _team_form_response(
            request,
            db,
            mode="create",
            values=values,
            error=message,
            status_code=_error_status(exc) if isinstance(exc, DomainError) else 400,
        )
    return RedirectResponse(
        url=f"/admin/system/service-teams/{team_id}",
        status_code=303,
    )


@router.get("/{team_id}", response_class=HTMLResponse)
def service_team_detail(
    request: Request,
    team_id: UUID,
    db: Session = Depends(get_db),
    _auth: dict = Depends(require_permission(READ_PERMISSION)),
):
    return _detail_response(request, db, team_id)


@router.get("/{team_id}/edit", response_class=HTMLResponse)
def service_team_edit(
    request: Request,
    team_id: UUID,
    db: Session = Depends(get_db),
    _auth: dict = Depends(require_permission(UPDATE_PERMISSION)),
):
    try:
        detail = service_team_lifecycle.get_team(db, team_id)
    except DomainError:
        return _detail_response(request, db, team_id)
    if not detail.actions.can_edit:
        return _detail_response(
            request,
            db,
            team_id,
            error=detail.actions.lifecycle_block_reason,
            status_code=409,
        )
    return _team_form_response(
        request,
        db,
        mode="edit",
        team=detail.team,
    )


@router.post("/{team_id}", response_class=HTMLResponse)
def service_team_update(
    request: Request,
    team_id: UUID,
    request_id: UUID = Form(...),
    expected_updated_at: str = Form(...),
    name: str = Form(...),
    team_type: ServiceTeamType = Form(...),
    region: str | None = Form(default=None),
    manager_system_user_id: str | None = Form(default=None),
    db: Session = Depends(get_db),
    auth: dict = Depends(require_permission(UPDATE_PERMISSION)),
):
    try:
        db_session_adapter.release_read_transaction(db)
        service_team_lifecycle.update_team(
            db,
            service_team_lifecycle.UpdateServiceTeam(
                context=_context(
                    auth,
                    scope=UPDATE_PERMISSION,
                    reason="Update service team",
                    request_id=request_id,
                ),
                team_id=team_id,
                expected_updated_at=_parse_timestamp(expected_updated_at),
                name=name,
                team_type=team_type,
                region=region,
                manager_system_user_id=_parse_optional_uuid(manager_system_user_id),
            ),
        )
    except (DomainError, ValueError) as exc:
        message = exc.message if isinstance(exc, DomainError) else str(exc)
        detail = None
        try:
            detail = service_team_lifecycle.get_team(db, team_id)
        except DomainError:
            pass
        return _team_form_response(
            request,
            db,
            mode="edit",
            team=detail.team if detail else None,
            values={
                "name": name,
                "team_type": team_type.value,
                "region": str(region or ""),
                "manager_system_user_id": str(manager_system_user_id or ""),
            },
            error=message,
            status_code=_error_status(exc) if isinstance(exc, DomainError) else 400,
        )
    return RedirectResponse(
        url=f"/admin/system/service-teams/{team_id}",
        status_code=303,
    )


@router.post("/{team_id}/active", response_class=HTMLResponse)
def service_team_set_active(
    request: Request,
    team_id: UUID,
    request_id: UUID = Form(...),
    expected_updated_at: str = Form(...),
    is_active: bool = Form(...),
    reason: str = Form(...),
    db: Session = Depends(get_db),
    auth: dict = Depends(require_permission(RETIRE_PERMISSION)),
):
    try:
        db_session_adapter.release_read_transaction(db)
        service_team_lifecycle.set_team_active(
            db,
            service_team_lifecycle.SetServiceTeamActive(
                context=_context(
                    auth,
                    scope=RETIRE_PERMISSION,
                    reason=reason,
                    request_id=request_id,
                ),
                team_id=team_id,
                expected_updated_at=_parse_timestamp(expected_updated_at),
                is_active=is_active,
                reason=reason,
            ),
        )
    except DomainError as exc:
        return _detail_response(
            request,
            db,
            team_id,
            error=exc.message,
            status_code=_error_status(exc),
        )
    return RedirectResponse(
        url=f"/admin/system/service-teams/{team_id}",
        status_code=303,
    )


@router.post("/{team_id}/members", response_class=HTMLResponse)
def service_team_add_member(
    request: Request,
    team_id: UUID,
    request_id: UUID = Form(...),
    system_user_id: UUID = Form(...),
    role: ServiceTeamMemberRole = Form(...),
    db: Session = Depends(get_db),
    auth: dict = Depends(require_permission(MEMBERSHIP_PERMISSION)),
):
    try:
        db_session_adapter.release_read_transaction(db)
        service_team_lifecycle.add_member(
            db,
            service_team_lifecycle.AddServiceTeamMember(
                context=_context(
                    auth,
                    scope=MEMBERSHIP_PERMISSION,
                    reason="Add service-team member",
                    request_id=request_id,
                ),
                team_id=team_id,
                system_user_id=system_user_id,
                role=role,
            ),
        )
    except DomainError as exc:
        return _detail_response(
            request,
            db,
            team_id,
            error=exc.message,
            status_code=_error_status(exc),
        )
    return RedirectResponse(
        url=f"/admin/system/service-teams/{team_id}",
        status_code=303,
    )


@router.post("/{team_id}/members/{member_id}/role", response_class=HTMLResponse)
def service_team_update_member(
    request: Request,
    team_id: UUID,
    member_id: UUID,
    request_id: UUID = Form(...),
    role: ServiceTeamMemberRole = Form(...),
    db: Session = Depends(get_db),
    auth: dict = Depends(require_permission(MEMBERSHIP_PERMISSION)),
):
    try:
        db_session_adapter.release_read_transaction(db)
        service_team_lifecycle.update_member(
            db,
            service_team_lifecycle.UpdateServiceTeamMember(
                context=_context(
                    auth,
                    scope=MEMBERSHIP_PERMISSION,
                    reason="Change service-team member role",
                    request_id=request_id,
                ),
                team_id=team_id,
                member_id=member_id,
                role=role,
            ),
        )
    except DomainError as exc:
        return _detail_response(
            request,
            db,
            team_id,
            error=exc.message,
            status_code=_error_status(exc),
        )
    return RedirectResponse(
        url=f"/admin/system/service-teams/{team_id}",
        status_code=303,
    )


@router.post("/{team_id}/members/{member_id}/remove", response_class=HTMLResponse)
def service_team_remove_member(
    request: Request,
    team_id: UUID,
    member_id: UUID,
    request_id: UUID = Form(...),
    reason: str = Form(...),
    db: Session = Depends(get_db),
    auth: dict = Depends(require_permission(MEMBERSHIP_PERMISSION)),
):
    try:
        db_session_adapter.release_read_transaction(db)
        service_team_lifecycle.remove_member(
            db,
            service_team_lifecycle.RemoveServiceTeamMember(
                context=_context(
                    auth,
                    scope=MEMBERSHIP_PERMISSION,
                    reason=reason,
                    request_id=request_id,
                ),
                team_id=team_id,
                member_id=member_id,
                reason=reason,
            ),
        )
    except DomainError as exc:
        return _detail_response(
            request,
            db,
            team_id,
            error=exc.message,
            status_code=_error_status(exc),
        )
    return RedirectResponse(
        url=f"/admin/system/service-teams/{team_id}",
        status_code=303,
    )
