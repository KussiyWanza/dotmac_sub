"""Admin network topology routes (replaces legacy weathermap)."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_network_topology as web_topology_service
from app.services.auth_dependencies import require_permission
from app.web.request_parsing import parse_form_data_sync

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
    "/topology",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:weathermap:read"))],
)
def network_topology(
    request: Request,
    group: str | None = None,
    site: str | None = None,
    sort: Literal["source_device"] | None = Query(None),
    dir: Literal["asc", "desc"] | None = Query(None),
    page: int = Query(1, ge=1),
    per_page: Literal[10, 25, 50, 100] = Query(25),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Network topology visualization page."""
    context = _base_context(request, db, active_page="topology")
    context.update(
        web_topology_service.topology_page_context(
            db,
            group=group,
            site=site,
            sort_by=sort,
            sort_dir=dir,
            page=page,
            per_page=per_page,
        )
    )
    return templates.TemplateResponse("admin/network/topology/index.html", context)


@router.get(
    "/weathermap",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:weathermap:read"))],
)
def network_weathermap(
    request: Request,
    group: str | None = None,
    site: str | None = None,
    view: str | None = None,
    noc: bool = False,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Read-only operational weather map."""
    context = _base_context(request, db, active_page="weathermap")
    try:
        context.update(
            web_topology_service.weathermap_page_context(
                db, group=group, site=site, view_slug=view, noc=noc
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return templates.TemplateResponse("admin/network/weathermap.html", context)


# ── Link CRUD ────────────────────────────────────────────────────────


@router.get(
    "/topology/links/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:weathermap:write"))],
)
def topology_link_new(request: Request, db: Session = Depends(get_db)):
    context = _base_context(request, db, active_page="topology")
    context.update(
        web_topology_service.link_form_context(
            db,
            link=None,
            action_url="/admin/network/topology/links/new",
        )
    )
    return templates.TemplateResponse("admin/network/topology/link_form.html", context)


@router.post(
    "/topology/links/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:weathermap:write"))],
)
def topology_link_create(request: Request, db: Session = Depends(get_db)):
    form = parse_form_data_sync(request)
    data = web_topology_service.parse_link_form(form)
    try:
        web_topology_service.create_link(db, data=data)
        return RedirectResponse(url="/admin/network/topology", status_code=303)
    except (ValueError, Exception) as exc:
        context = _base_context(request, db, active_page="topology")
        context.update(
            web_topology_service.link_form_context(
                db,
                link=data,
                action_url="/admin/network/topology/links/new",
                error=str(exc),
            )
        )
        return templates.TemplateResponse(
            "admin/network/topology/link_form.html", context
        )


@router.get(
    "/topology/links/{link_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:weathermap:write"))],
)
def topology_link_edit(request: Request, link_id: str, db: Session = Depends(get_db)):
    context = _base_context(request, db, active_page="topology")
    context.update(web_topology_service.link_edit_context(db, link_id))
    return templates.TemplateResponse("admin/network/topology/link_form.html", context)


@router.post(
    "/topology/links/{link_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:weathermap:write"))],
)
def topology_link_update(request: Request, link_id: str, db: Session = Depends(get_db)):
    form = parse_form_data_sync(request)
    data = web_topology_service.parse_link_form(form)
    try:
        web_topology_service.update_link(db, link_id, data=data)
        return RedirectResponse(url="/admin/network/topology", status_code=303)
    except (ValueError, Exception) as exc:
        context = _base_context(request, db, active_page="topology")
        context.update(
            web_topology_service.link_form_context(
                db,
                link=data,
                action_url=f"/admin/network/topology/links/{link_id}/edit",
                error=str(exc),
            )
        )
        return templates.TemplateResponse(
            "admin/network/topology/link_form.html", context
        )


@router.post(
    "/topology/links/{link_id}/delete",
    dependencies=[Depends(require_permission("network:weathermap:write"))],
)
def topology_link_delete(
    link_id: str, db: Session = Depends(get_db)
) -> RedirectResponse:
    web_topology_service.delete_link(db, link_id)
    return RedirectResponse(url="/admin/network/topology", status_code=303)


# ── AJAX Helpers ─────────────────────────────────────────────────────


@router.get(
    "/topology/api/interfaces/{device_id}",
    response_class=JSONResponse,
    dependencies=[Depends(require_permission("network:weathermap:read"))],
)
def topology_device_interfaces(device_id: str, db: Session = Depends(get_db)):
    """Return interfaces for a device (populates dropdowns via HTMX/JS)."""
    return web_topology_service.get_device_interfaces(db, device_id)


@router.get(
    "/topology/api/node/{device_id}",
    response_class=JSONResponse,
    dependencies=[Depends(require_permission("network:weathermap:read"))],
)
def topology_node_summary(device_id: str, db: Session = Depends(get_db)):
    """Return node summary for drilldown panel."""
    return web_topology_service.node_summary(db, device_id)


@router.post(
    "/topology/api/node-positions",
    response_class=JSONResponse,
    dependencies=[Depends(require_permission("network:weathermap:write"))],
)
async def topology_save_node_positions(request: Request, db: Session = Depends(get_db)):
    """Persist manually arranged topology node positions."""
    try:
        payload = await request.json()
        positions = payload.get("positions", []) if isinstance(payload, dict) else []
        result = web_topology_service.save_node_positions(db, positions)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result


@router.get(
    "/topology/api/graph",
    response_class=JSONResponse,
    dependencies=[Depends(require_permission("network:weathermap:read"))],
)
def topology_graph_data(
    group: str | None = None, site: str | None = None, db: Session = Depends(get_db)
):
    """Return full graph data as JSON (for D3 refresh without full page reload)."""
    return web_topology_service.graph_data(db, group=group, site=site)


@router.get(
    "/weathermap/api/graph",
    response_class=JSONResponse,
    dependencies=[Depends(require_permission("network:weathermap:read"))],
)
def weathermap_graph_data(
    group: str | None = None,
    site: str | None = None,
    view: str | None = None,
    db: Session = Depends(get_db),
):
    """Return weather-map graph data without reloading the full admin page."""
    try:
        return web_topology_service.weathermap_graph_data(
            db, group=group, site=site, view_slug=view
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/weathermap/api/layout",
    response_class=JSONResponse,
    dependencies=[Depends(require_permission("network:weathermap:write"))],
)
async def weathermap_save_layout(
    request: Request,
    view: str | None = None,
    db: Session = Depends(get_db),
):
    """Persist the operator-arranged weather-map node positions."""
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid layout payload")
    try:
        return web_topology_service.save_weathermap_layout(
            db, view_slug=view, payload=payload
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/weathermap/api/layout/reset",
    response_class=JSONResponse,
    dependencies=[Depends(require_permission("network:weathermap:write"))],
)
def weathermap_reset_layout(
    view: str | None = None,
    db: Session = Depends(get_db),
):
    """Clear saved weather-map coordinates so automatic layout can run again."""
    try:
        return web_topology_service.reset_weathermap_layout(db, view_slug=view)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
