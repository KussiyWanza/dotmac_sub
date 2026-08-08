"""Meta social installation configuration adapter."""

from dataclasses import asdict

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.db import finish_read_transaction, get_db
from app.services import web_integrations_meta_social
from app.services.auth_dependencies import can, require_permission
from app.services.domain_errors import DomainError
from app.services.integrations.meta_social_installation import (
    META_SOCIAL_CONFIGURATION_SCOPE,
)
from app.services.owner_commands import CommandContext
from app.web.templates import templates

router = APIRouter(prefix="/crm/meta", tags=["web-admin-meta"])


@router.get(
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:conversation:read"))],
)
def meta_connection(request: Request, db: Session = Depends(get_db)):
    from app.web.admin import get_current_user, get_sidebar_stats

    state = web_integrations_meta_social.build_config_page(db)
    return templates.TemplateResponse(
        "admin/inbox/meta_connection.html",
        {
            "request": request,
            "active_page": "meta-connection",
            "active_menu": "services",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            "saved": request.query_params.get("saved") == "1",
            "can_configure": can(request, "system:settings:write"),
            **asdict(state),
        },
    )


@router.post(
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def save_meta_connection(
    request: Request,
    auth_mode: str = Form("individual"),
    app_id: str = Form(""),
    facebook_page_id: str = Form(""),
    instagram_account_id: str = Form(""),
    graph_version: str = Form("v21.0"),
    webhook_url: str = Form(""),
    meta_oauth_access_token_ref: str = Form(""),
    facebook_page_access_token_ref: str = Form(""),
    instagram_login_access_token_ref: str = Form(""),
    webhook_signing_secret_ref: str = Form(""),
    webhook_verify_token_ref: str = Form(""),
    db: Session = Depends(get_db),
):
    from app.web.admin import get_current_user, get_sidebar_stats

    current_user = get_current_user(request)
    actor = str(getattr(current_user, "id", None) or "admin.meta_social")
    form = web_integrations_meta_social.MetaSocialConfigFormCommand(
        auth_mode=auth_mode,
        app_id=app_id,
        facebook_page_id=facebook_page_id,
        instagram_account_id=instagram_account_id,
        graph_version=graph_version,
        webhook_url=webhook_url,
        meta_oauth_access_token_ref=meta_oauth_access_token_ref,
        facebook_page_access_token_ref=facebook_page_access_token_ref,
        instagram_login_access_token_ref=instagram_login_access_token_ref,
        webhook_signing_secret_ref=webhook_signing_secret_ref,
        webhook_verify_token_ref=webhook_verify_token_ref,
    )
    finish_read_transaction(db)
    try:
        web_integrations_meta_social.save_config(
            db,
            form,
            context=CommandContext.system(
                actor=actor,
                scope=META_SOCIAL_CONFIGURATION_SCOPE,
                reason="Configure Meta social inbox transport",
                idempotency_key=(
                    f"meta-social-config:{auth_mode.strip()}:"
                    f"{app_id.strip()}:"
                    f"{facebook_page_id.strip()}:{instagram_account_id.strip()}"
                ),
            ),
        )
    except DomainError as exc:
        state = web_integrations_meta_social.build_config_page(db)
        context = {
            "request": request,
            "active_page": "meta-connection",
            "active_menu": "services",
            "current_user": current_user,
            "sidebar_stats": get_sidebar_stats(db),
            "error": str(exc),
            "can_configure": can(request, "system:settings:write"),
            **asdict(state),
            "auth_mode": auth_mode,
            "app_id": app_id,
            "facebook_page_id": facebook_page_id,
            "instagram_account_id": instagram_account_id,
            "graph_version": graph_version,
            "webhook_url": webhook_url,
        }
        return templates.TemplateResponse(
            "admin/inbox/meta_connection.html", context, status_code=400
        )
    return RedirectResponse(url="/admin/crm/meta?saved=1", status_code=303)
