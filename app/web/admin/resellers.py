"""Admin reseller portal web routes."""

from urllib.parse import quote_plus
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.db import get_db
from app.services import reseller_onboarding, subscriber_assignments
from app.services import web_admin_resellers as reseller_svc
from app.services.auth_dependencies import require_permission
from app.services.owner_commands import CommandContext
from app.services.web_system_common import humanize_integrity_error
from app.web.request_parsing import parse_form_data

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/resellers", tags=["web-admin-resellers"])


def _command_context(
    auth: dict,
    *,
    scope: str,
    reason: str,
    idempotency_key: str,
    correlation_id=None,
) -> CommandContext:
    principal_id = str(auth.get("principal_id") or "").strip()
    if not principal_id:
        raise HTTPException(status_code=403, detail="Authorized actor is missing")
    actor_type = "api_key" if auth.get("principal_type") == "api_key" else "user"
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=correlation_id or command_id,
        actor=f"{actor_type}:{principal_id}",
        scope=scope,
        reason=reason,
        idempotency_key=idempotency_key,
    )


def _assignment_context(
    auth: dict, *, reason: str, idempotency_key: str, correlation_id=None
) -> CommandContext:
    return _command_context(
        auth,
        scope=subscriber_assignments.ASSIGNMENT_SCOPE,
        reason=reason,
        idempotency_key=idempotency_key,
        correlation_id=correlation_id,
    )


def _onboarding_context(
    auth: dict, *, reason: str, idempotency_key: str
) -> CommandContext:
    return _command_context(
        auth,
        scope=reseller_onboarding.RESELLER_WRITE_SCOPE,
        reason=reason,
        idempotency_key=idempotency_key,
    )


def _form_str(form: FormData, key: str, default: str = "") -> str:
    value = form.get(key, default)
    return value if isinstance(value, str) else default


def _form_int(form: FormData, key: str, default: int) -> int:
    raw = _form_str(form, key, str(default)).strip()
    try:
        return int(raw)
    except Exception:
        return default


def _error_message(exc: Exception, fallback: str) -> str:
    if isinstance(exc, IntegrityError):
        return humanize_integrity_error(exc)
    return str(exc) or fallback


def _base_context(request: Request, db: Session, active_page: str) -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": active_page,
        "active_menu": "resellers",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


@router.post("/{reseller_id}/impersonate", response_class=HTMLResponse)
def reseller_impersonate(
    request: Request,
    reseller_id: str,
    db: Session = Depends(get_db),
    auth=Depends(require_permission("reseller:impersonate")),
):
    """Open the reseller portal as the reseller ("view as"), audited.

    Mirrors customer impersonation: mints a real reseller-principal session so
    support/staff can see and act on the portal exactly as the reseller does.
    Admin already has full access to all reseller data, so this exposes nothing
    new — it is a faster lens, gated behind ``reseller:impersonate`` and logged.
    """
    from app.models.audit import AuditActorType
    from app.services import reseller_portal
    from app.services.audit_adapter import record_audit_event

    return_to = f"/admin/resellers/{reseller_id}"
    try:
        session_token = reseller_portal.create_impersonation_session(
            db, reseller_id=reseller_id, return_to=return_to
        )
    except HTTPException as exc:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": str(exc.detail)},
            status_code=exc.status_code,
        )

    actor_id_value = None
    if isinstance(auth, dict):
        actor_id_value = (
            str(auth.get("subscriber_id") or auth.get("person_id") or "") or None
        )
    record_audit_event(
        db,
        actor_type=AuditActorType.user,
        actor_id=actor_id_value,
        action="impersonate",
        entity_type="reseller",
        entity_id=str(reseller_id),
        status_code=303,
        is_success=True,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        metadata={"surface": "reseller_portal"},
    )

    response = RedirectResponse(url="/reseller/dashboard", status_code=303)
    response.set_cookie(
        key=reseller_portal.SESSION_COOKIE_NAME,
        value=session_token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=reseller_portal.get_session_max_age(db),
    )
    return response


@router.get(
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("reseller:read"))],
)
def resellers_list(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=200),
    status: str = Query("active"),
    notice: str | None = Query(None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    context = _base_context(request, db, active_page="resellers")
    context.update(
        reseller_svc.list_page_context(
            db, page=page, per_page=per_page, status_filter=status
        )
    )
    context["notice"] = notice
    return templates.TemplateResponse("admin/resellers/index.html", context)


@router.get("/new", response_class=HTMLResponse)
def reseller_new(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    context = _base_context(request, db, active_page="resellers")
    context.update(reseller_svc.new_form_context(db))
    return templates.TemplateResponse("admin/resellers/reseller_form.html", context)


@router.get("/{reseller_id}/edit", response_class=HTMLResponse)
def reseller_edit(
    reseller_id: str, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    context = _base_context(request, db, active_page="resellers")
    context.update(reseller_svc.edit_form_context(db, reseller_id=reseller_id))
    return templates.TemplateResponse("admin/resellers/reseller_form.html", context)


@router.post(
    "",
    response_class=HTMLResponse,
)
def reseller_create(
    request: Request,
    form: FormData = Depends(parse_form_data),
    reseller_auth: dict = Depends(require_permission("reseller:write")),
    assignment_auth: dict = Depends(require_permission("rbac:assign")),
    db: Session = Depends(get_db),
):
    payload = reseller_svc.parse_reseller_payload(form)
    user_payload = reseller_svc.parse_create_user_payload(form)
    user_error = reseller_svc.validate_create_user_payload(user_payload)
    if user_error:
        context = _base_context(request, db, active_page="resellers")
        context.update(
            reseller_svc.create_form_error_context(
                db,
                payload=payload,
                error=user_error,
            )
        )
        return templates.TemplateResponse(
            "admin/resellers/reseller_form.html", context, status_code=400
        )
    try:
        owner_context = _onboarding_context(
            reseller_auth,
            reason="Create reseller and optional portal principal",
            idempotency_key="reseller-onboarding",
        )
        outcome, invite_note = reseller_svc.create_reseller_from_form(
            db,
            form,
            context=owner_context,
            assignment_context=_assignment_context(
                assignment_auth,
                reason="Reseller onboarding role assignment",
                idempotency_key="reseller-onboarding-role-assignment",
                correlation_id=owner_context.correlation_id,
            ),
        )
    except Exception as exc:
        db.rollback()
        context = _base_context(request, db, active_page="resellers")
        context.update(
            reseller_svc.create_form_error_context(
                db,
                payload=payload,
                error=_error_message(exc, "Unable to create reseller."),
            )
        )
        return templates.TemplateResponse(
            "admin/resellers/reseller_form.html", context, status_code=400
        )
    if invite_note and "could not" in invite_note.lower():
        return RedirectResponse(
            url=(
                f"/admin/resellers/{outcome.reseller_id}"
                f"?notice={quote_plus(invite_note)}"
            ),
            status_code=303,
        )
    return RedirectResponse(url="/admin/resellers", status_code=303)


@router.post(
    "/{reseller_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("reseller:write"))],
)
def reseller_update(
    reseller_id: str,
    request: Request,
    form: FormData = Depends(parse_form_data),
    db: Session = Depends(get_db),
):
    payload = reseller_svc.parse_reseller_payload(form)
    try:
        reseller_svc.update_reseller_from_form(db, reseller_id=reseller_id, form=form)
    except Exception as exc:
        db.rollback()
        context = _base_context(request, db, active_page="resellers")
        context.update(
            reseller_svc.update_form_error_context(
                db,
                reseller_id=reseller_id,
                payload=payload,
                error=_error_message(exc, "Unable to update reseller."),
            )
        )
        return templates.TemplateResponse(
            "admin/resellers/reseller_form.html", context, status_code=400
        )
    return RedirectResponse(url="/admin/resellers", status_code=303)


@router.post(
    "/{reseller_id}/status",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("reseller:write"))],
)
def reseller_status_update(
    reseller_id: str,
    request: Request,
    form: FormData = Depends(parse_form_data),
    db: Session = Depends(get_db),
):
    is_active = _form_str(form, "status").strip().lower() == "active"
    return_status = _form_str(form, "return_status", "all").strip() or "all"
    page = _form_int(form, "page", 1)
    per_page = _form_int(form, "per_page", 25)
    try:
        reseller_svc.update_reseller_active_status(
            db, reseller_id=reseller_id, is_active=is_active
        )
    except Exception:
        db.rollback()
        raise
    return RedirectResponse(
        url=(
            f"/admin/resellers?status={return_status}&page={page}&per_page={per_page}"
        ),
        status_code=303,
    )


@router.get("/{reseller_id}", response_class=HTMLResponse)
def reseller_detail(
    reseller_id: str,
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=200),
    notice: str | None = Query(None),
    db: Session = Depends(get_db),
):
    detail = reseller_svc.get_reseller_detail_context(
        db,
        reseller_id,
        page=page,
        per_page=per_page,
    )
    if not detail:
        return RedirectResponse(
            url="/admin/resellers?notice=" + quote_plus("Reseller not found."),
            status_code=303,
        )
    context = _base_context(request, db, active_page="resellers")
    context.update(detail)
    context["notice"] = notice
    return templates.TemplateResponse("admin/resellers/detail.html", context)


@router.post(
    "/{reseller_id}/users/link",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("reseller:write"))],
)
def reseller_user_link(
    reseller_id: str,
    request: Request,
    form: FormData = Depends(parse_form_data),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    page = _form_int(form, "page", 1)
    per_page = _form_int(form, "per_page", 25)
    subscriber_id = (
        _form_str(form, "subscriber_id").strip() or _form_str(form, "person_id").strip()
    )
    if subscriber_id:
        try:
            reseller_svc.link_existing_subscriber_to_reseller(
                db, reseller_id=reseller_id, subscriber_id=subscriber_id
            )
        except Exception as exc:
            db.rollback()
            detail = reseller_svc.get_reseller_detail_context(
                db,
                reseller_id,
                page=page,
                per_page=per_page,
            )
            context = _base_context(request, db, active_page="resellers")
            context.update(detail or {})
            context["error"] = _error_message(exc, "Unable to link subscriber.")
            return templates.TemplateResponse(
                "admin/resellers/detail.html", context, status_code=400
            )
    return RedirectResponse(
        url=f"/admin/resellers/{reseller_id}?page={page}&per_page={per_page}",
        status_code=303,
    )


@router.post(
    "/{reseller_id}/users/create",
    response_class=HTMLResponse,
)
def reseller_user_create(
    reseller_id: str,
    request: Request,
    form: FormData = Depends(parse_form_data),
    reseller_auth: dict = Depends(require_permission("reseller:write")),
    assignment_auth: dict = Depends(require_permission("rbac:assign")),
    db: Session = Depends(get_db),
):
    page = _form_int(form, "page", 1)
    per_page = _form_int(form, "per_page", 25)
    fields = {
        "first_name": _form_str(form, "first_name").strip(),
        "last_name": _form_str(form, "last_name").strip(),
        "email": _form_str(form, "email").strip(),
        "username": _form_str(form, "email").strip(),
        "role": _form_str(form, "role").strip() or None,
    }
    if not all([fields["first_name"], fields["last_name"], fields["email"]]):
        detail = reseller_svc.get_reseller_detail_context(
            db,
            reseller_id,
            page=page,
            per_page=per_page,
        )
        context = _base_context(request, db, active_page="resellers")
        context.update(detail or {})
        context["error"] = "First name, last name, and email are required."
        return templates.TemplateResponse(
            "admin/resellers/detail.html", context, status_code=400
        )
    try:
        owner_context = _onboarding_context(
            reseller_auth,
            reason="Provision reseller portal principal",
            idempotency_key=f"reseller-user-onboarding:{reseller_id}",
        )
        reseller_svc.create_and_link_reseller_user(
            db,
            reseller_id=reseller_id,
            first_name=fields["first_name"],
            last_name=fields["last_name"],
            email=fields["email"],
            username=fields["username"],
            role=fields["role"],
            context=owner_context,
            assignment_context=_assignment_context(
                assignment_auth,
                reason="Reseller user role assignment",
                idempotency_key=f"reseller-user-role-assignment:{reseller_id}",
                correlation_id=owner_context.correlation_id,
            ),
        )
    except Exception as exc:
        db.rollback()
        detail = reseller_svc.get_reseller_detail_context(
            db,
            reseller_id,
            page=page,
            per_page=per_page,
        )
        context = _base_context(request, db, active_page="resellers")
        context.update(detail or {})
        context["error"] = _error_message(exc, "Unable to create reseller user.")
        return templates.TemplateResponse(
            "admin/resellers/detail.html", context, status_code=400
        )
    return RedirectResponse(
        url=f"/admin/resellers/{reseller_id}?page={page}&per_page={per_page}",
        status_code=303,
    )
