"""Admin vendor route-view web routes (maps §C).

UI-only port of the CRM ``vendors/quotes/route-view`` page: renders the native
vendor ``route_geom`` (proposed + as-built) over the fiber-plant network on
Leaflet. Geometry is fetched client-side from the ``/api/v1/vendor-routes``
GeoJSON endpoint; the fiber overlay reuses ``fiber_plant_api``. Guarded by
``network:fiber:read`` — consistent with the fiber map and the GeoJSON API.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import fiber_plant_api, vendor_routes_api
from app.services.auth_dependencies import require_permission
from app.services.vendor_portal_operations import vendor_portal_operations

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/vendors", tags=["web-admin-vendor-routes"])


def _ctx(request: Request, db: Session, active_page: str) -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": active_page,
        "active_menu": "operations",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


@router.get(
    "/routes",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:fiber:read"))],
)
def vendor_routes_list(request: Request, db: Session = Depends(get_db)):
    context = _ctx(request, db, "vendor-routes")
    context["projects"] = vendor_routes_api.list_route_projects(db)
    return templates.TemplateResponse("admin/vendors/routes.html", context)


@router.get(
    "/routes/{project_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:fiber:read"))],
)
def vendor_route_view(
    request: Request,
    project_id: str,
    revision_id: str | None = None,
    message: str | None = None,
    db: Session = Depends(get_db),
):
    project = vendor_routes_api.get_route_project(db, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Installation project not found")
    context = _ctx(request, db, "vendor-routes")
    context.update(
        {
            "project": project,
            "project_id": project_id,
            "revision_id": revision_id,
            "message": message,
            "route_revisions": (
                vendor_portal_operations.list_route_revisions_for_project(
                    db,
                    project_id,
                )
            ),
            "network_geojson": fiber_plant_api.build_fiber_plant_geojson(
                db,
                include_fdh=True,
                include_closures=True,
                include_pops=True,
                include_segments=True,
            ),
        }
    )
    return templates.TemplateResponse("admin/vendors/route_view.html", context)
