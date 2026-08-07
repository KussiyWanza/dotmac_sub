"""Service helpers for admin notifications dropdown."""

from fastapi import Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.services import admin_alerts as admin_alerts_service
from app.services import web_admin as web_admin_service

templates = Jinja2Templates(directory="templates")


def notifications_menu(request: Request, db: Session):
    current_user = web_admin_service.get_current_user(request)
    system_user_id = (
        current_user.get("id")
        if current_user.get("principal_type") == "system_user"
        else None
    )
    menu_context = admin_alerts_service.notification_menu_context(
        db, system_user_id=system_user_id
    )
    return templates.TemplateResponse(
        request,
        "admin/partials/notifications_menu.html",
        {
            "request": request,
            **menu_context,
        },
    )
