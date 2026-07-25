"""Admin billing payment-arrangement routes."""

from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import payment_arrangement_staff_actions, payment_arrangements
from app.services import web_billing_arrangements as web_arrangements
from app.services.action_forms import ActionFormSubmission
from app.services.auth_dependencies import has_permission, require_permission
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/billing", tags=["web-admin-billing"])


def _command_context(
    auth: dict,
    *,
    arrangement_id: UUID,
    action: payment_arrangements.PaymentArrangementStaffAction,
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
        scope=payment_arrangement_staff_actions.ACTION_SCOPE,
        reason=f"Staff confirmed payment-arrangement {action.value}",
        idempotency_key=(
            f"payment-arrangement:{arrangement_id}:{action.value}:{preview_fingerprint}"
        ),
    )


def _error_status(error: DomainError) -> int:
    if error.code.endswith(".not_found"):
        return 404
    if error.code.endswith((".stale_preview", ".active_caller_transaction")):
        return 409
    return 400


def _detail_response(
    request: Request,
    arrangement_id: UUID,
    *,
    db: Session,
    auth: dict,
    submission: ActionFormSubmission | None = None,
    page_error: str | None = None,
    status_code: int = 200,
) -> Response:
    can_write = has_permission(auth, db, "billing:arrangement:write")
    try:
        state = web_arrangements.detail_data(
            db,
            arrangement_id=str(arrangement_id),
            can_write=can_write,
            submission=submission,
        )
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Payment arrangement not found"},
            status_code=404,
        )

    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/billing/payment_arrangement_detail.html",
        {
            "request": request,
            **state,
            "page_error": page_error,
            "active_page": "payment_arrangements",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
        status_code=status_code,
    )


def _confirm_action(
    request: Request,
    arrangement_id: UUID,
    *,
    action: payment_arrangements.PaymentArrangementStaffAction,
    preview_fingerprint: str,
    confirmed: str,
    note: str | None,
    db: Session,
    auth: dict,
) -> Response:
    context = _command_context(
        auth,
        arrangement_id=arrangement_id,
        action=action,
        preview_fingerprint=preview_fingerprint,
    )
    try:
        db_session_adapter.release_read_transaction(db)
        payment_arrangement_staff_actions.confirm_staff_action(
            db,
            payment_arrangement_staff_actions.ConfirmPaymentArrangementStaffAction(
                arrangement_id=arrangement_id,
                action=action,
                preview_fingerprint=preview_fingerprint,
                confirmed=confirmed == "yes",
                actor_id=str(auth.get("principal_id") or ""),
                note=note,
                context=context,
            ),
        )
    except DomainError as exc:
        submission = web_arrangements.action_error_submission(
            action=action,
            note=note,
            error=exc,
        )
        return _detail_response(
            request,
            arrangement_id,
            db=db,
            auth=auth,
            submission=submission,
            page_error=exc.message,
            status_code=_error_status(exc),
        )
    return RedirectResponse(
        url=f"/admin/billing/payment-arrangements/{arrangement_id}",
        status_code=303,
    )


@router.get(
    "/payment-arrangements",
    response_class=HTMLResponse,
)
def payment_arrangements_list(
    request: Request,
    status: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
    _auth: dict = Depends(require_permission("billing:arrangement:read")),
):
    state = web_arrangements.list_data(
        db,
        status=status,
        page=page,
        per_page=per_page,
    )
    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/billing/payment_arrangements.html",
        {
            "request": request,
            **state,
            "active_page": "payment_arrangements",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.get(
    "/payment-arrangements/{arrangement_id}",
    response_class=HTMLResponse,
)
def payment_arrangements_detail(
    request: Request,
    arrangement_id: UUID,
    db: Session = Depends(get_db),
    auth: dict = Depends(require_permission("billing:arrangement:read")),
):
    return _detail_response(
        request,
        arrangement_id,
        db=db,
        auth=auth,
    )


@router.post(
    "/payment-arrangements/{arrangement_id}/approve",
    response_class=HTMLResponse,
)
def payment_arrangements_approve(
    request: Request,
    arrangement_id: UUID,
    preview_fingerprint: str = Form(""),
    confirmed: str = Form(""),
    db: Session = Depends(get_db),
    auth: dict = Depends(require_permission("billing:arrangement:write")),
):
    return _confirm_action(
        request,
        arrangement_id,
        action=payment_arrangements.PaymentArrangementStaffAction.approve,
        preview_fingerprint=preview_fingerprint,
        confirmed=confirmed,
        note=None,
        db=db,
        auth=auth,
    )


@router.post(
    "/payment-arrangements/{arrangement_id}/record-payment",
    response_class=HTMLResponse,
)
def payment_arrangements_record_payment(
    request: Request,
    arrangement_id: UUID,
    preview_fingerprint: str = Form(""),
    confirmed: str = Form(""),
    note: str = Form(""),
    db: Session = Depends(get_db),
    auth: dict = Depends(require_permission("billing:arrangement:write")),
):
    return _confirm_action(
        request,
        arrangement_id,
        action=payment_arrangements.PaymentArrangementStaffAction.record_payment,
        preview_fingerprint=preview_fingerprint,
        confirmed=confirmed,
        note=note,
        db=db,
        auth=auth,
    )


@router.post(
    "/payment-arrangements/{arrangement_id}/cancel",
    response_class=HTMLResponse,
)
def payment_arrangements_cancel(
    request: Request,
    arrangement_id: UUID,
    preview_fingerprint: str = Form(""),
    confirmed: str = Form(""),
    db: Session = Depends(get_db),
    auth: dict = Depends(require_permission("billing:arrangement:write")),
):
    return _confirm_action(
        request,
        arrangement_id,
        action=payment_arrangements.PaymentArrangementStaffAction.cancel,
        preview_fingerprint=preview_fingerprint,
        confirmed=confirmed,
        note=None,
        db=db,
        auth=auth,
    )
