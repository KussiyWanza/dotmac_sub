"""Permission-gated Automation Center admin hub."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_automation_center
from app.services.auth_dependencies import has_permission, require_permission
from app.services.automation_rules import RULE_READ_PERMISSION
from app.services.automation_runtime import RUN_READ_PERMISSION

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/automation", tags=["web-admin-automation"])


def _base_context(request: Request, db: Session) -> dict[str, object]:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": "automation-center",
        "active_menu": "automation-center",
        "page_title": "Automation Center",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


@router.get("", response_class=HTMLResponse)
def automation_center_index(
    request: Request,
    db: Session = Depends(get_db),
    auth: dict = Depends(require_permission("automation:hub:read")),
):
    """Show registry, rules, run evidence, and legacy ownership in one place."""

    state = web_automation_center.build_automation_center_data(
        db,
        can_read_rules=has_permission(auth, db, RULE_READ_PERMISSION),
        can_read_runs=has_permission(auth, db, RUN_READ_PERMISSION),
    )
    return templates.TemplateResponse(
        "admin/automation/index.html",
        {**_base_context(request, db), **state},
    )


__all__ = ["router"]
