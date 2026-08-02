"""Admin dispatch work-order routes."""

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_dispatch_work_orders as work_orders_service
from app.services.auth_dependencies import can, require_permission
from app.web.request_parsing import parse_form_data_sync

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/dispatch", tags=["web-admin-dispatch"])


def _ctx(request: Request, db: Session) -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": "dispatch-work-orders",
        "active_menu": "operations",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


@router.get(
    "/work-orders",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("operations:dispatch:read"))],
)
def dispatch_work_orders(
    request: Request,
    status: str | None = None,
    q: str | None = None,
    active: bool | None = None,
    project_task_id: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    notice: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
):
    state = work_orders_service.list_page(
        db,
        status=status,
        q=q,
        active=active,
        project_task_id=project_task_id,
        can_create=can(request, "operations:dispatch:write"),
        page=page,
        per_page=per_page,
    )
    context = _ctx(request, db)
    context.update(state)
    context.update({"notice": notice, "error": error})
    return templates.TemplateResponse("admin/dispatch/work_orders.html", context)


@router.get(
    "/work-orders/{work_order_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("operations:dispatch:read"))],
)
def dispatch_work_order_detail(
    request: Request,
    work_order_id: str,
    notice: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
):
    state = work_orders_service.detail_page(db, work_order_id)
    context = _ctx(request, db)
    context.update(state)
    context.update({"notice": notice, "error": error})
    return templates.TemplateResponse("admin/dispatch/work_order_detail.html", context)


@router.post(
    "/work-orders",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("operations:dispatch:write"))],
)
def create_dispatch_work_order(
    request: Request,
    db: Session = Depends(get_db),
):
    form = dict(parse_form_data_sync(request))
    try:
        row = work_orders_service.create_from_form(
            db,
            form,
            auth=getattr(request.state, "auth", None),
            request_id=request.headers.get("X-Request-ID"),
        )
    except (HTTPException, ValidationError, ValueError) as exc:
        detail = getattr(exc, "detail", None) or str(exc)
        return _redirect(error=detail)
    return _detail_redirect(row.public_id, notice=f"Work order {row.public_id} created")


@router.post(
    "/work-orders/{work_order_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("operations:dispatch:write"))],
)
def update_dispatch_work_order(
    request: Request,
    work_order_id: str,
    db: Session = Depends(get_db),
):
    form = dict(parse_form_data_sync(request))
    try:
        work_orders_service.update_from_form(
            db,
            work_order_id,
            form,
            auth=getattr(request.state, "auth", None),
            request_id=request.headers.get("X-Request-ID"),
        )
    except (HTTPException, ValidationError, ValueError) as exc:
        detail = getattr(exc, "detail", None) or str(exc)
        return _redirect(error=detail)
    return _detail_redirect(work_order_id, notice=f"Work order {work_order_id} updated")


@router.post(
    "/work-orders/{work_order_id}/queue",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("operations:dispatch:assign"))],
)
def queue_dispatch_work_order(
    request: Request,
    work_order_id: str,
    assigned_technician_id: str = Form(...),
    status: str = Form("queued"),
    reason: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        work_orders_service.queue_assignment_from_form(
            db,
            work_order_id,
            {
                "assigned_technician_id": assigned_technician_id,
                "status": status,
                "reason": reason,
            },
            auth=getattr(request.state, "auth", None),
            request_id=request.headers.get("X-Request-ID"),
        )
    except (HTTPException, ValidationError, ValueError) as exc:
        detail = getattr(exc, "detail", None) or str(exc)
        return _redirect(error=detail)
    return _detail_redirect(work_order_id, notice=f"Work order {work_order_id} queued")


def _detail_redirect(
    work_order_id: str, *, notice: str | None = None, error: str | None = None
) -> RedirectResponse:
    params = {
        key: str(value)
        for key, value in {"notice": notice, "error": error}.items()
        if value
    }
    suffix = f"?{urlencode(params)}" if params else ""
    return RedirectResponse(
        url=f"/admin/dispatch/work-orders/{work_order_id}{suffix}", status_code=303
    )


def _redirect(
    *,
    notice: str | None = None,
    error: str | None = None,
    q: str | None = None,
) -> RedirectResponse:
    url = "/admin/dispatch/work-orders"
    params: dict[str, str] = {}
    if notice:
        params["notice"] = str(notice)
    elif error:
        params["error"] = str(error)
    if q:
        params["q"] = q
    if params:
        url += f"?{urlencode(params)}"
    return RedirectResponse(url=url, status_code=303)
