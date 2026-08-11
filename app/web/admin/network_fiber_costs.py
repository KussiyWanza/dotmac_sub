"""Admin adapter for fiber drop-cost items.

Its own module, not part of `network_fiber_plant`, because that adapter carries
a guard — `test_the_admin_adapter_is_thin_and_gated_by_the_existing_plant
_permission` — asserting it never touches `is_active`. That guard is about
as-built plant ACTIVATION, where flipping activation state in the adapter would
put an authority decision in a transport layer.

A cost item's active flag is a different concern with the same field name, so
sharing a module would have meant either weakening a guard that is right or
renaming a field to slip past it. A separate adapter is neither.

Thin, like its neighbour: it parses a form, calls
`app.services.fiber_cost_items`, and maps that owner's typed refusal onto a
redirect.
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import fiber_cost_items as fiber_cost_items_service
from app.services.audit_helpers import log_audit_event
from app.services.auth_dependencies import require_permission

router = APIRouter(prefix="/network", tags=["admin-network-fiber-costs"])
templates = Jinja2Templates(directory="templates")

_LIST_URL = "/admin/network/fiber-cost-items"


def _redirect_with_error(message: str) -> RedirectResponse:
    return RedirectResponse(f"{_LIST_URL}?error={quote(message)}", status_code=303)


def _actor_id(request: Request) -> str | None:
    """Who is making the change, for the audit record.

    Same shape as the billing adapters: an audit row that cannot name an actor
    records None honestly rather than inventing one.
    """

    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    if not current_user:
        return None
    value = current_user.get("actor_id") or current_user.get("subscriber_id")
    return str(value) if value else None


def _base_context(request: Request, db: Session, active_page: str) -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": active_page,
        "active_menu": "fiber",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


@router.get(
    "/fiber-cost-items",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:fiber:read"))],
)
def fiber_cost_items_page(request: Request, db: Session = Depends(get_db)):
    """The components a drop estimate is built from, and their prices."""

    context = _base_context(request, db, active_page="fiber-cost-items")
    context.update(fiber_cost_items_service.list_data(db))
    return templates.TemplateResponse("admin/network/fiber/cost_items.html", context)


@router.post(
    "/fiber-cost-items",
    dependencies=[Depends(require_permission("network:fiber:write"))],
)
def fiber_cost_item_create(
    request: Request,
    code: str = Form(...),
    label: str = Form(...),
    unit: str = Form(...),
    amount: str | None = Form(None),
    sort_order: int = Form(100),
    description: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        item = fiber_cost_items_service.create_item(
            db,
            code=code,
            label=label,
            unit=unit,
            amount=amount,
            sort_order=sort_order,
            description=description,
        )
    except fiber_cost_items_service.FiberCostItemError as exc:
        return _redirect_with_error(str(exc))
    log_audit_event(
        db=db,
        request=request,
        action="create",
        entity_type="fiber_cost_item",
        entity_id=str(item.id),
        actor_id=_actor_id(request),
    )
    return RedirectResponse(_LIST_URL, status_code=303)


@router.post(
    "/fiber-cost-items/{item_id}",
    dependencies=[Depends(require_permission("network:fiber:write"))],
)
def fiber_cost_item_update(
    request: Request,
    item_id: str,
    label: str | None = Form(None),
    unit: str | None = Form(None),
    amount: str | None = Form(None),
    is_active: str | None = Form(None),
    sort_order: int | None = Form(None),
    description: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        item = fiber_cost_items_service.update_item(
            db,
            item_id,
            label=label,
            unit=unit,
            amount=amount,
            # An unchecked checkbox sends nothing, so absence means False here
            # rather than "unchanged" — the form always submits every field.
            is_active=is_active is not None,
            sort_order=sort_order,
            description=description,
        )
    except fiber_cost_items_service.FiberCostItemError as exc:
        return _redirect_with_error(str(exc))
    log_audit_event(
        db=db,
        request=request,
        action="update",
        entity_type="fiber_cost_item",
        entity_id=str(item.id),
        actor_id=_actor_id(request),
    )
    return RedirectResponse(_LIST_URL, status_code=303)
