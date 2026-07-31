"""Meta connection readiness and account status page."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.domain_settings import SettingDomain
from app.services import meta_pages
from app.services.auth_dependencies import require_permission
from app.services.settings_spec import resolve_value
from app.web.templates import templates

router = APIRouter(prefix="/crm/meta", tags=["web-admin-meta"])


@router.get(
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:conversation:read"))],
)
def meta_connection(request: Request, db: Session = Depends(get_db)):
    from app.web.admin import get_current_user, get_sidebar_stats

    app_id = resolve_value(db, SettingDomain.comms, "meta_app_id")
    app_secret = resolve_value(db, SettingDomain.comms, "meta_app_secret")
    pages = meta_pages.get_connected_pages(db)
    instagram_accounts = meta_pages.get_connected_instagram_accounts(db)
    return templates.TemplateResponse(
        "admin/inbox/meta_connection.html",
        {
            "request": request,
            "active_page": "meta-connection",
            "active_menu": "services",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            "app_configured": bool(app_id and app_secret),
            "pages": pages,
            "instagram_accounts": instagram_accounts,
        },
    )
