"""Settings routes for reviewed payment-configuration lifecycle actions."""

from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import finish_read_transaction, get_db
from app.services import payment_configuration_staff_actions as staff_actions
from app.services import web_payment_configuration
from app.services.auth_dependencies import (
    has_permission,
    load_permission_keys,
    require_user_auth,
)
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/settings/billing", tags=["web-admin-settings"])


def _require_scoped_permission(
    resource: staff_actions.PaymentConfigurationResource,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
) -> dict:
    permission = staff_actions.action_scope(resource)
    load_permission_keys(auth, db)
    if not has_permission(auth, db, permission):
        raise HTTPException(status_code=403, detail="Forbidden")
    finish_read_transaction(db)
    return auth


def _context(
    auth: dict,
    *,
    resource: staff_actions.PaymentConfigurationResource,
    resource_id: UUID,
    action: staff_actions.PaymentConfigurationAction,
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
        scope=staff_actions.action_scope(resource),
        reason=(
            f"Staff confirmed {action.value} for payment configuration {resource.value}"
        ),
        idempotency_key=(
            f"payment-configuration:{resource.value}:{resource_id}:"
            f"{action.value}:{preview_fingerprint}"
        ),
    )


def _render(
    request: Request,
    db: Session,
    *,
    resource: staff_actions.PaymentConfigurationResource,
    resource_id: UUID,
    action: staff_actions.PaymentConfigurationAction,
    page_error: str | None = None,
    status_code: int = 200,
) -> Response:
    from app.web.admin import get_current_user, get_sidebar_stats

    state = web_payment_configuration.review_state(
        db,
        resource=resource,
        resource_id=resource_id,
        action=action,
        page_error=page_error,
    )
    return templates.TemplateResponse(
        "admin/system/payment_configuration_review.html",
        {
            "request": request,
            **state,
            "active_page": "settings-hub",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
        status_code=status_code,
    )


@router.post(
    "/payment-configuration/{resource}/{resource_id}/{action}/review",
    response_class=HTMLResponse,
)
def review_payment_configuration_action(
    request: Request,
    resource: staff_actions.PaymentConfigurationResource,
    resource_id: UUID,
    action: staff_actions.PaymentConfigurationAction,
    db: Session = Depends(get_db),
    _auth: dict = Depends(_require_scoped_permission),
) -> Response:
    return _render(
        request,
        db,
        resource=resource,
        resource_id=resource_id,
        action=action,
    )


@router.post(
    "/payment-configuration/{resource}/{resource_id}/{action}/confirm",
    response_class=HTMLResponse,
)
def confirm_payment_configuration_action(
    request: Request,
    resource: staff_actions.PaymentConfigurationResource,
    resource_id: UUID,
    action: staff_actions.PaymentConfigurationAction,
    preview_fingerprint: str = Form(...),
    confirmed: str | None = Form(None),
    db: Session = Depends(get_db),
    auth: dict = Depends(_require_scoped_permission),
) -> Response:
    try:
        db_session_adapter.release_read_transaction(db)
        staff_actions.confirm_staff_action(
            db,
            staff_actions.ConfirmPaymentConfigurationStaffAction(
                resource=resource,
                resource_id=resource_id,
                action=action,
                preview_fingerprint=preview_fingerprint,
                confirmed=confirmed == "yes",
                actor_id=str(auth.get("principal_id") or ""),
                context=_context(
                    auth,
                    resource=resource,
                    resource_id=resource_id,
                    action=action,
                    preview_fingerprint=preview_fingerprint,
                ),
            ),
        )
    except DomainError as exc:
        return _render(
            request,
            db,
            resource=resource,
            resource_id=resource_id,
            action=action,
            page_error=exc.message,
            status_code=web_payment_configuration.error_status(exc),
        )
    return RedirectResponse(
        url=web_payment_configuration.settings_url(resource),
        status_code=303,
    )
