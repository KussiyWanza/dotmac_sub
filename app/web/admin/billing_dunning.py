"""Admin billing dunning web routes."""

from urllib.parse import urlencode
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import dunning_staff_actions
from app.services import web_billing_dunning as web_dunning
from app.services.action_forms import ActionFormSubmission
from app.services.auth_dependencies import has_permission, require_permission
from app.services.collections import _core as dunning_owner
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/billing", tags=["web-admin-billing"])


def _base_context(request: Request, db: Session, active_page: str) -> dict[str, object]:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": active_page,
        "active_menu": "billing",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


def _command_context(
    auth: dict,
    *,
    action: dunning_owner.DunningStaffAction,
    preview_fingerprint: str,
) -> CommandContext:
    principal_id = str(auth.get("principal_id") or "").strip()
    if not principal_id:
        raise HTTPException(status_code=403, detail="Authorized actor is missing")
    actor_type = "api_key" if auth.get("principal_type") == "api_key" else "user"
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor=f"{actor_type}:{principal_id}",
        scope=dunning_staff_actions.ACTION_SCOPE,
        reason=f"Staff confirmed dunning {action.value}",
        idempotency_key=f"dunning-staff:{action.value}:{preview_fingerprint}",
    )


def _error_status(error: DomainError) -> int:
    if error.code.endswith(".stale_preview"):
        return 409
    return 400


def _dunning_list_url(
    *,
    note: str | None = None,
    warning: str | None = None,
) -> str:
    params = {
        key: value
        for key, value in {"dunning_note": note, "dunning_warning": warning}.items()
        if value
    }
    query = urlencode(params)
    return f"/admin/billing/dunning?{query}" if query else "/admin/billing/dunning"


def _detail_response(
    request: Request,
    case_id: str,
    *,
    db: Session,
    auth: dict,
    submission: ActionFormSubmission | None = None,
    page_error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    state = web_dunning.build_detail_data(
        db,
        case_id=case_id,
        can_write=has_permission(auth, db, dunning_staff_actions.ACTION_SCOPE),
        submission=submission,
    )
    return templates.TemplateResponse(
        "admin/billing/dunning_detail.html",
        {
            **_base_context(request, db, "dunning"),
            **state,
            "page_error": page_error,
        },
        status_code=status_code,
    )


def _parse_bulk_action(action: str) -> dunning_owner.DunningStaffAction:
    try:
        parsed = dunning_owner.DunningStaffAction(action)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Dunning action not found") from exc
    if parsed is dunning_owner.DunningStaffAction.close:
        raise HTTPException(status_code=404, detail="Bulk close is not supported")
    return parsed


def _confirm(
    *,
    action: dunning_owner.DunningStaffAction,
    case_ids: tuple[str, ...],
    preview_fingerprint: str,
    confirmed: str,
    db: Session,
    auth: dict,
) -> dunning_staff_actions.DunningStaffActionResult:
    context = _command_context(
        auth,
        action=action,
        preview_fingerprint=preview_fingerprint,
    )
    db_session_adapter.release_read_transaction(db)
    normalized = dunning_staff_actions.normalize_case_ids(case_ids)
    return dunning_staff_actions.confirm_staff_action(
        db,
        dunning_staff_actions.ConfirmDunningStaffAction(
            action=action,
            case_ids=normalized,
            preview_fingerprint=preview_fingerprint,
            confirmed=confirmed == "yes",
            actor_id=str(auth.get("principal_id") or ""),
            context=context,
        ),
    )


@router.get("/dunning", response_class=HTMLResponse)
def billing_dunning(
    request: Request,
    page: int = 1,
    per_page: int = Query(50, ge=10, le=100),
    status: str | None = None,
    customer_ref: str | None = Query(None),
    db: Session = Depends(get_db),
    auth: dict = Depends(require_permission("billing:dunning:read")),
):
    state = web_dunning.build_listing_data(
        db,
        page=page,
        per_page=per_page,
        status=status,
        customer_ref=customer_ref,
        can_write=has_permission(auth, db, dunning_staff_actions.ACTION_SCOPE),
    )
    return templates.TemplateResponse(
        "admin/billing/dunning.html",
        {
            **_base_context(request, db, "dunning"),
            **state,
        },
    )


@router.get("/dunning/{case_id}", response_class=HTMLResponse)
def dunning_detail(
    request: Request,
    case_id: str,
    db: Session = Depends(get_db),
    auth: dict = Depends(require_permission("billing:dunning:read")),
):
    return _detail_response(
        request,
        case_id,
        db=db,
        auth=auth,
    )


@router.post("/dunning/{case_id}/{action}/confirm", response_class=HTMLResponse)
def confirm_case_action(
    request: Request,
    case_id: UUID,
    action: dunning_owner.DunningStaffAction,
    preview_fingerprint: str = Form(""),
    confirmed: str = Form(""),
    db: Session = Depends(get_db),
    auth: dict = Depends(require_permission("billing:dunning:write")),
) -> Response:
    try:
        result = _confirm(
            action=action,
            case_ids=(str(case_id),),
            preview_fingerprint=preview_fingerprint,
            confirmed=confirmed,
            db=db,
            auth=auth,
        )
    except DomainError as exc:
        submission = web_dunning.action_error_submission(
            action=action,
            error=exc,
        )
        return _detail_response(
            request,
            str(case_id),
            db=db,
            auth=auth,
            submission=submission,
            page_error=exc.message,
            status_code=_error_status(exc),
        )
    return RedirectResponse(
        url=_dunning_list_url(
            note=(
                f"{action.value.title()}d {len(result.processed_case_ids)} "
                "dunning case."
            )
        ),
        status_code=303,
    )


@router.post("/dunning/bulk/{action}/preview", response_class=HTMLResponse)
def preview_bulk_action(
    request: Request,
    action: str,
    case_ids: str = Form(""),
    db: Session = Depends(get_db),
    _auth: dict = Depends(require_permission("billing:dunning:write")),
):
    parsed_action = _parse_bulk_action(action)
    try:
        state = web_dunning.build_bulk_preview_data(
            db,
            action=parsed_action,
            case_ids_csv=case_ids,
        )
    except DomainError as exc:
        return RedirectResponse(
            url=_dunning_list_url(warning=exc.message),
            status_code=303,
        )
    return templates.TemplateResponse(
        "admin/billing/dunning_bulk_confirm.html",
        {
            **_base_context(request, db, "dunning"),
            **state,
        },
    )


@router.post("/dunning/bulk/{action}/confirm", response_class=HTMLResponse)
def confirm_bulk_action(
    request: Request,
    action: str,
    case_ids: str = Form(""),
    preview_fingerprint: str = Form(""),
    confirmed: str = Form(""),
    db: Session = Depends(get_db),
    auth: dict = Depends(require_permission("billing:dunning:write")),
) -> Response:
    parsed_action = _parse_bulk_action(action)
    try:
        result = _confirm(
            action=parsed_action,
            case_ids=tuple(case_ids.split(",")),
            preview_fingerprint=preview_fingerprint,
            confirmed=confirmed,
            db=db,
            auth=auth,
        )
    except DomainError as exc:
        try:
            state = web_dunning.build_bulk_preview_data(
                db,
                action=parsed_action,
                case_ids_csv=case_ids,
            )
        except DomainError:
            return RedirectResponse(
                url=_dunning_list_url(warning=exc.message),
                status_code=303,
            )
        return templates.TemplateResponse(
            "admin/billing/dunning_bulk_confirm.html",
            {
                **_base_context(request, db, "dunning"),
                **state,
                "page_error": exc.message,
            },
            status_code=_error_status(exc),
        )
    return RedirectResponse(
        url=_dunning_list_url(
            note=(
                f"{parsed_action.value.title()}d {len(result.processed_case_ids)} "
                f"of {result.preview.selected_count} selected dunning cases; "
                f"{result.preview.skipped_count} skipped."
            )
        ),
        status_code=303,
    )
