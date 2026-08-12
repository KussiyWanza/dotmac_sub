"""Admin network management base web routes."""

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_network_core_devices as web_network_core_devices_service
from app.services.auth_dependencies import require_permission

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
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:hub:read"))],
)
def network_hub(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """Network hub landing page."""
    return templates.TemplateResponse(
        "admin/network/index.html",
        _base_context(request, db, active_page="network"),
    )


def _build_device_query(
    *,
    device_type: str | None,
    type_filter: str | None,
    search: str | None,
    status: str | None,
    vendor: str | None,
    sort_by: str | None = None,
    sort_dir: str | None = None,
    page: int = 1,
    per_page: int | None = None,
    offset: int | None = None,
    limit: int | None = None,
):
    """Build the validated device ListQuery from loose route params.

    Accepts either page/per_page or the offset/limit that
    components/data/table_pagination.html emits. Falls back to defaults on
    out-of-contract params rather than erroring the page.
    """
    if limit:
        per_page = limit
        page = ((offset or 0) // limit) + 1
    selected_type = type_filter or device_type
    try:
        return web_network_core_devices_service.build_network_device_list_query(
            device_type=selected_type,
            status=status,
            vendor=vendor,
            search=search,
            sort_by=sort_by,
            sort_dir=sort_dir,
            page=page,
            per_page=per_page,
        )
    except ValueError:
        return web_network_core_devices_service.build_network_device_list_query(
            page=max(page, 1)
        )


@router.get(
    "/devices",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:device:read"))],
)
def devices_list(
    request: Request,
    device_type: str | None = None,
    type_filter: str | None = Query(default=None, alias="type"),
    search: str | None = None,
    status: str | None = None,
    vendor: str | None = None,
    sort_by: str | None = None,
    sort_dir: str | None = None,
    page: int = Query(default=1, ge=1),
    per_page: int | None = Query(default=None),
    offset: int | None = Query(default=None),
    limit: int | None = Query(default=None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """List all network devices (SQL-paginated over the device projection)."""
    list_query = _build_device_query(
        device_type=device_type,
        type_filter=type_filter,
        search=search,
        status=status,
        vendor=vendor,
        sort_by=sort_by,
        sort_dir=sort_dir,
        page=page,
        per_page=per_page,
        offset=offset,
        limit=limit,
    )
    page_data = web_network_core_devices_service.devices_list_page_data(db, list_query)
    context = _base_context(request, db, active_page="devices")
    context.update(page_data)
    return templates.TemplateResponse("admin/network/devices/index.html", context)


@router.get(
    "/devices/search",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:device:read"))],
)
def devices_search(
    request: Request,
    search: str = "",
    offset: int | None = Query(default=None),
    limit: int | None = Query(default=None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    list_query = _build_device_query(
        device_type=None,
        type_filter=None,
        search=search,
        status=None,
        vendor=None,
        offset=offset,
        limit=limit,
    )
    devices = web_network_core_devices_service.devices_search_data(db, list_query)
    return templates.TemplateResponse(
        "admin/network/devices/_table_rows.html",
        {"request": request, "devices": devices},
    )


@router.get(
    "/devices/filter",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:device:read"))],
)
def devices_filter(
    request: Request,
    device_type: str | None = None,
    type_filter: str | None = Query(default=None, alias="type"),
    search: str | None = None,
    status: str | None = None,
    vendor: str | None = None,
    sort_by: str | None = None,
    sort_dir: str | None = None,
    offset: int | None = Query(default=None),
    limit: int | None = Query(default=None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    list_query = _build_device_query(
        device_type=device_type,
        type_filter=type_filter,
        search=search,
        status=status,
        vendor=vendor,
        sort_by=sort_by,
        sort_dir=sort_dir,
        offset=offset,
        limit=limit,
    )
    devices = web_network_core_devices_service.devices_filter_data(db, list_query)
    return templates.TemplateResponse(
        "admin/network/devices/_table_rows.html",
        {"request": request, "devices": devices},
    )


@router.get(
    "/devices/create",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:device:read"))],
)
def device_create(request: Request, db: Session = Depends(get_db)):
    # Redirect to the more specific device creation pages.
    return RedirectResponse(url="/admin/network/core-devices/new", status_code=302)


@router.get(
    "/devices/{device_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:device:read"))],
)
def device_detail(
    request: Request, device_id: str, db: Session = Depends(get_db)
) -> Response:
    redirect_url = web_network_core_devices_service.resolve_device_redirect(
        db, device_id
    )
    if redirect_url:
        return RedirectResponse(url=redirect_url, status_code=302)

    return templates.TemplateResponse(
        "admin/errors/404.html",
        {"request": request, "message": "Device not found"},
        status_code=404,
    )


@router.post(
    "/devices/{device_id}/ping",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:device:write"))],
)
def device_ping(request: Request, device_id: str, db: Session = Depends(get_db)):
    return HTMLResponse(
        '<div class="rounded-lg border border-slate-200 bg-white px-4 py-3 text-sm text-slate-600 shadow-sm dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300">'
        f"Ping queued for device {device_id}."
        "</div>"
    )


@router.post(
    "/devices/{device_id}/reboot",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:device:write"))],
)
def device_reboot(request: Request, device_id: str, db: Session = Depends(get_db)):
    return HTMLResponse(
        '<div class="rounded-lg border border-slate-200 bg-white px-4 py-3 text-sm text-slate-600 shadow-sm dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300">'
        f"Reboot request queued for device {device_id}."
        "</div>"
    )


@router.get(
    "/devices/{device_id}/reboot/preview",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:device:write"))],
)
def device_reboot_preview(
    request: Request, device_id: str, db: Session = Depends(get_db)
):
    """Safe impact-preview step before the existing reboot command adapter."""
    context = _base_context(request, db, active_page="devices")
    context.update({"device_id": device_id, "affected": 1})
    return templates.TemplateResponse(
        "admin/network/devices/reboot_preview.html", context
    )


@router.get(
    "/map",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:map:read"))],
)
def comprehensive_network_map(request: Request, db: Session = Depends(get_db)):
    """Comprehensive network map showing all infrastructure and customers."""
    from app.services import network_map as network_map_service

    context = _base_context(request, db, active_page="network-map")
    projection = network_map_service.build_network_map_projection(db=db)
    context.update(projection.to_template_context())
    return templates.TemplateResponse("admin/network/map.html", context)


@router.get(
    "/map/plant-data",
    dependencies=[Depends(require_permission("network:map:read"))],
)
def network_map_plant_data(db: Session = Depends(get_db)) -> dict[str, object]:
    """Read-only GeoJSON plant subset for the dispatch live map."""
    from app.services import network_map as network_map_service

    return network_map_service.build_network_map_plant_projection(db=db).to_transport()
