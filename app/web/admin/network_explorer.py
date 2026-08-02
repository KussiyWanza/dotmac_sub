"""Unified network explorer routes.

Thin adapters around ui.network_explorer_projection: they authorize, read
URL state (subject + query), and render the projection. No topology, search,
or presentation decision lives here.
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import network_explorer as network_explorer_service
from app.services.auth_dependencies import has_permission, require_permission

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


def _include_customer_identity(request: Request, db: Session) -> bool:
    auth = getattr(request.state, "auth", None)
    if not auth:
        return False
    return has_permission(auth, db, "customer:read")


@router.get(
    "/explorer",
    response_class=HTMLResponse,
    dependencies=[
        Depends(require_permission(network_explorer_service.EXPLORER_PAGE_PERMISSION))
    ],
)
def network_explorer(
    request: Request,
    subject: str | None = None,
    q: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Subject-centred explorer page; state lives in the URL for sharing."""

    context = _base_context(request, db, active_page="explorer")
    explorer = network_explorer_service.build_explorer_context(
        db,
        subject=subject,
        query=q,
        include_customer_identity=_include_customer_identity(request, db),
    )
    context.update({"explorer": explorer, "graph": explorer.view_dict})
    return templates.TemplateResponse("admin/network/explorer/index.html", context)


@router.get(
    "/explorer/coverage",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("monitoring:read"))],
)
def network_explorer_coverage(
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Topology-quality view: per-subscription coverage and drift worklists."""

    context = _base_context(request, db, active_page="explorer")
    context["coverage"] = network_explorer_service.build_network_coverage(db)
    return templates.TemplateResponse("admin/network/explorer/coverage.html", context)


@router.get(
    "/explorer/inspect",
    response_class=HTMLResponse,
    dependencies=[
        Depends(require_permission(network_explorer_service.EXPLORER_PAGE_PERMISSION))
    ],
)
def network_explorer_inspect(
    request: Request,
    subject: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """On-demand inspector fragment for a selected subject."""

    inspector = network_explorer_service.build_inspector(
        db,
        subject,
        include_customer_identity=_include_customer_identity(request, db),
    )
    return templates.TemplateResponse(
        "admin/network/explorer/_inspector.html",
        {"request": request, "inspector": inspector},
    )


@router.get(
    "/explorer/api/graph",
    dependencies=[
        Depends(require_permission(network_explorer_service.EXPLORER_PAGE_PERMISSION))
    ],
)
def network_explorer_graph(
    request: Request,
    subject: str,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """The subject's bounded graph as JSON, for on-demand recentring."""

    explorer = network_explorer_service.build_explorer_context(
        db,
        subject=subject,
        query=None,
        include_customer_identity=_include_customer_identity(request, db),
    )
    if explorer.view is None:
        return JSONResponse({"error": "subject_not_found"}, status_code=404)
    return JSONResponse(explorer.view_dict)
