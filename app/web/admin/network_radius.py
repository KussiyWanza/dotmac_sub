"""Admin network RADIUS web routes."""

import logging
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_network_access_ledger as web_network_access_ledger_service
from app.services import web_network_radius as web_network_radius_service
from app.services.auth_dependencies import require_permission
from app.web.request_parsing import parse_form_data_sync

logger = logging.getLogger(__name__)
templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/network", tags=["web-admin-network"])


def _base_context(
    request: Request, db: Session, active_page: str, active_menu: str = "network"
) -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": active_page,
        "active_menu": active_menu,
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


@router.get(
    "/radius",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:radius:read"))],
)
def radius_page(
    request: Request,
    clients_page: int = Query(1, ge=1),
    profiles_page: int = Query(1, ge=1),
    db: Session = Depends(get_db),
):
    context = _base_context(request, db, active_page="radius")
    query = web_network_radius_service.RadiusOverviewQuery.from_pages(
        clients_page=clients_page,
        profiles_page=profiles_page,
    )
    state = web_network_radius_service.radius_page_data(db=db, query=query)
    context.update(
        {
            "servers": state.servers,
            "clients": state.clients.items,
            "clients_page_meta": state.clients.page_meta,
            "profiles": state.profiles.items,
            "profiles_page_meta": state.profiles.page_meta,
            "recent_sessions": state.recent_sessions,
            "recent_errors": state.recent_errors,
            "activities": state.activities,
            "total_online": state.total_online,
            "total_errors": state.total_errors,
            "notice": request.query_params.get("notice"),
            "error": request.query_params.get("error"),
        }
    )
    return templates.TemplateResponse("admin/network/radius/index.html", context)


@router.post(
    "/radius/import-credentials",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:radius:write"))],
)
def radius_import_credentials(_request: Request, db: Session = Depends(get_db)):
    try:
        notice = web_network_radius_service.import_credentials_notice(db)
        return RedirectResponse(
            f"/admin/network/radius?notice={quote_plus(notice)}",
            status_code=303,
        )
    except Exception as exc:
        logger.error("RADIUS credential import failed: %s", exc)
        return RedirectResponse(
            f"/admin/network/radius?error={quote_plus(str(exc))}",
            status_code=303,
        )


@router.get(
    "/radius/servers/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:radius:read"))],
)
def radius_server_new(request: Request, db: Session = Depends(get_db)):
    context = _base_context(request, db, active_page="radius")
    context.update(web_network_radius_service.server_new_form_data())
    return templates.TemplateResponse("admin/network/radius/server_form.html", context)


@router.get(
    "/radius/servers",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:radius:read"))],
)
def radius_servers_redirect():
    return RedirectResponse("/admin/network/radius", status_code=303)


@router.get(
    "/radius/servers/{server_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:radius:read"))],
)
def radius_server_edit(request: Request, server_id: str, db: Session = Depends(get_db)):
    form_data = web_network_radius_service.server_edit_form_data(db, server_id)
    if not form_data:
        context = _base_context(request, db, active_page="radius")
        context.update({"message": "RADIUS server not found"})
        return templates.TemplateResponse(
            "admin/errors/404.html",
            context,
            status_code=404,
        )

    context = _base_context(request, db, active_page="radius")
    context.update(form_data)
    return templates.TemplateResponse("admin/network/radius/server_form.html", context)


@router.post(
    "/radius/servers",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:radius:write"))],
)
def radius_server_create(request: Request, db: Session = Depends(get_db)):
    result = web_network_radius_service.create_server_from_form(
        request,
        db,
        parse_form_data_sync(request),
    )
    if result.success:
        return RedirectResponse("/admin/network/radius", status_code=303)

    context = _base_context(request, db, active_page="radius")
    context.update(result.form_context or {})
    return templates.TemplateResponse("admin/network/radius/server_form.html", context)


@router.post(
    "/radius/servers/{server_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:radius:write"))],
)
def radius_server_update(
    request: Request, server_id: str, db: Session = Depends(get_db)
):
    result = web_network_radius_service.update_server_from_form(
        request,
        db,
        server_id=server_id,
        form=parse_form_data_sync(request),
    )
    if result.not_found_message:
        context = _base_context(request, db, active_page="radius")
        context.update({"message": result.not_found_message})
        return templates.TemplateResponse(
            "admin/errors/404.html",
            context,
            status_code=404,
        )
    if result.success:
        return RedirectResponse("/admin/network/radius", status_code=303)

    context = _base_context(request, db, active_page="radius")
    context.update(result.form_context or {})
    return templates.TemplateResponse("admin/network/radius/server_form.html", context)


@router.get(
    "/radius/clients/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:radius:read"))],
)
def radius_client_new(request: Request, db: Session = Depends(get_db)):
    context = _base_context(request, db, active_page="radius")
    context.update(web_network_radius_service.client_new_form_data(db))
    return templates.TemplateResponse("admin/network/radius/client_form.html", context)


@router.get(
    "/radius/clients/{client_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:radius:read"))],
)
def radius_client_edit(request: Request, client_id: str, db: Session = Depends(get_db)):
    form_data = web_network_radius_service.client_edit_form_data(db, client_id)
    if not form_data:
        context = _base_context(request, db, active_page="radius")
        context.update({"message": "RADIUS client not found"})
        return templates.TemplateResponse(
            "admin/errors/404.html",
            context,
            status_code=404,
        )

    context = _base_context(request, db, active_page="radius")
    context.update(form_data)
    return templates.TemplateResponse("admin/network/radius/client_form.html", context)


@router.post(
    "/radius/clients",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:radius:write"))],
)
def radius_client_create(request: Request, db: Session = Depends(get_db)):
    result = web_network_radius_service.create_client_from_form(
        request,
        db,
        parse_form_data_sync(request),
    )
    if result.success:
        return RedirectResponse("/admin/network/radius", status_code=303)

    context = _base_context(request, db, active_page="radius")
    context.update(result.form_context or {})
    return templates.TemplateResponse("admin/network/radius/client_form.html", context)


@router.post(
    "/radius/clients/{client_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:radius:write"))],
)
def radius_client_update(
    request: Request, client_id: str, db: Session = Depends(get_db)
):
    result = web_network_radius_service.update_client_from_form(
        request,
        db,
        client_id=client_id,
        form=parse_form_data_sync(request),
    )
    if result.not_found_message:
        context = _base_context(request, db, active_page="radius")
        context.update({"message": result.not_found_message})
        return templates.TemplateResponse(
            "admin/errors/404.html",
            context,
            status_code=404,
        )
    if result.success:
        return RedirectResponse("/admin/network/radius", status_code=303)

    context = _base_context(request, db, active_page="radius")
    context.update(result.form_context or {})
    return templates.TemplateResponse("admin/network/radius/client_form.html", context)


@router.get(
    "/radius/profiles/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:radius:read"))],
)
def radius_profile_new(request: Request, db: Session = Depends(get_db)):
    context = _base_context(request, db, active_page="radius")
    context.update(web_network_radius_service.profile_new_form_data())
    return templates.TemplateResponse("admin/network/radius/profile_form.html", context)


@router.get(
    "/radius/profiles/{profile_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:radius:read"))],
)
def radius_profile_edit(
    request: Request, profile_id: str, db: Session = Depends(get_db)
):
    form_data = web_network_radius_service.profile_edit_form_data(db, profile_id)
    if not form_data:
        context = _base_context(request, db, active_page="radius")
        context.update({"message": "RADIUS profile not found"})
        return templates.TemplateResponse(
            "admin/errors/404.html",
            context,
            status_code=404,
        )

    context = _base_context(request, db, active_page="radius")
    context.update(form_data)
    return templates.TemplateResponse("admin/network/radius/profile_form.html", context)


@router.post(
    "/radius/profiles",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:radius:write"))],
)
def radius_profile_create(request: Request, db: Session = Depends(get_db)):
    result = web_network_radius_service.create_profile_from_form(
        request,
        db,
        parse_form_data_sync(request),
    )
    if result.success:
        return RedirectResponse("/admin/network/radius", status_code=303)

    context = _base_context(request, db, active_page="radius")
    context.update(result.form_context or {})
    return templates.TemplateResponse("admin/network/radius/profile_form.html", context)


@router.post(
    "/radius/profiles/{profile_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:radius:write"))],
)
def radius_profile_update(
    request: Request, profile_id: str, db: Session = Depends(get_db)
):
    result = web_network_radius_service.update_profile_from_form(
        request,
        db,
        profile_id=profile_id,
        form=parse_form_data_sync(request),
    )
    if result.not_found_message:
        context = _base_context(request, db, active_page="radius")
        context.update({"message": result.not_found_message})
        return templates.TemplateResponse(
            "admin/errors/404.html",
            context,
            status_code=404,
        )
    if result.success:
        return RedirectResponse("/admin/network/radius", status_code=303)

    context = _base_context(request, db, active_page="radius")
    context.update(result.form_context or {})
    return templates.TemplateResponse("admin/network/radius/profile_form.html", context)


# --- Active Sessions (Who's Online) ---


@router.get(
    "/sessions",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:radius:read"))],
)
def active_sessions_page(
    request: Request,
    search: str = "",
    nas_filter: str = "",
    db: Session = Depends(get_db),
):
    """Who's Online — live RADIUS active sessions."""
    context = _base_context(request, db, active_page="radius")
    context.update(
        web_network_radius_service.active_sessions_page_data(
            db,
            search=search,
            nas_filter=nas_filter,
        )
    )
    return templates.TemplateResponse("admin/network/sessions.html", context)


# --- RADIUS Auth Errors ---


@router.get(
    "/radius-errors",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:radius:read"))],
)
def radius_auth_errors_page(
    request: Request,
    error_type: str = "",
    page: int = 1,
    db: Session = Depends(get_db),
):
    """RADIUS authentication error log."""
    context = _base_context(request, db, active_page="radius")
    context.update(
        web_network_radius_service.radius_auth_errors_page_data(
            db,
            error_type=error_type,
            page=page,
        )
    )
    return templates.TemplateResponse("admin/network/radius_errors.html", context)


@router.get(
    "/access",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:radius:read"))],
)
def access_ledger_page(
    request: Request,
    facet: str = Query(default="sessions"),
    db: Session = Depends(get_db),
):
    context = _base_context(request, db, active_page="access")
    context.update(web_network_access_ledger_service.access_ledger_data(db, facet))
    return templates.TemplateResponse("admin/network/access/index.html", context)
