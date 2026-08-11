"""Thin admin adapter for authoritative fiber drop-cost commands."""

from __future__ import annotations

from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.audit import AuditActorType
from app.schemas.fiber_cost_items import (
    CreateFiberCostItemCommand,
    UpdateFiberCostItemCommand,
)
from app.services import fiber_cost_items as fiber_cost_items_service
from app.services.auth_dependencies import has_permission, require_permission
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext

router = APIRouter(prefix="/network", tags=["admin-network-fiber-costs"])
templates = Jinja2Templates(directory="templates")

_LIST_URL = "/admin/network/fiber-cost-items"


def _redirect_with_error(message: str) -> RedirectResponse:
    return RedirectResponse(f"{_LIST_URL}?error={quote(message)}", status_code=303)


def _actor(auth: dict[str, object]) -> tuple[UUID, AuditActorType]:
    raw_id = str(auth.get("principal_id") or "").strip()
    try:
        actor_id = UUID(raw_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=403,
            detail="Authorized actor identity is missing or invalid",
        ) from exc
    actor_type = (
        AuditActorType.api_key
        if auth.get("principal_type") == "api_key"
        else AuditActorType.user
    )
    return actor_id, actor_type


def _command_context(
    *,
    actor_id: UUID,
    actor_type: AuditActorType,
    action: str,
    identity: str,
) -> CommandContext:
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor=f"{actor_type.value}:{actor_id}",
        scope=fiber_cost_items_service.WRITE_SCOPE,
        reason=f"Network operator {action} fiber drop-cost item {identity}",
        idempotency_key=f"fiber-cost-item:{action}:{identity}",
    )


def _base_context(request: Request, db: Session, active_page: str) -> dict[str, object]:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": active_page,
        "active_menu": "fiber",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


@router.get("/fiber-cost-items", response_class=HTMLResponse)
def fiber_cost_items_page(
    request: Request,
    db: Session = Depends(get_db),
    auth: dict[str, object] = Depends(require_permission("network:fiber:read")),
):
    """Render committed cost state and only the actions this actor may use."""

    state = fiber_cost_items_service.list_state(db)
    context = _base_context(request, db, active_page="fiber-cost-items")
    context.update(
        {
            "items": state.items,
            "units": state.units,
            "currency": state.pricing.currency,
            "is_complete": state.pricing.is_complete,
            "unpriced": [code.value for code in state.pricing.unpriced],
            "can_write": has_permission(
                auth,
                db,
                fiber_cost_items_service.WRITE_SCOPE,
            ),
        }
    )
    return templates.TemplateResponse("admin/network/fiber/cost_items.html", context)


@router.post("/fiber-cost-items")
def fiber_cost_item_create(
    request: Request,
    code: str = Form(...),
    label: str = Form(...),
    unit: str = Form(...),
    amount: str | None = Form(None),
    sort_order: int = Form(100),
    description: str | None = Form(None),
    db: Session = Depends(get_db),
    auth: dict[str, object] = Depends(
        require_permission(fiber_cost_items_service.WRITE_SCOPE)
    ),
):
    try:
        actor_id, actor_type = _actor(auth)
        parsed_code = fiber_cost_items_service.parse_code(code)
        command = CreateFiberCostItemCommand(
            context=_command_context(
                actor_id=actor_id,
                actor_type=actor_type,
                action="created",
                identity=parsed_code.value,
            ),
            actor_id=actor_id,
            actor_type=actor_type,
            code=parsed_code,
            label=label,
            unit=fiber_cost_items_service.parse_unit(unit),
            amount=fiber_cost_items_service.parse_amount(amount),
            sort_order=sort_order,
            description=description,
        )
        db_session_adapter.release_read_transaction(db)
        fiber_cost_items_service.create_item(db, command)
    except DomainError as exc:
        return _redirect_with_error(exc.message)
    return RedirectResponse(_LIST_URL, status_code=303)


@router.post("/fiber-cost-items/{item_id}")
def fiber_cost_item_update(
    request: Request,
    item_id: UUID,
    expected_version: int = Form(...),
    label: str = Form(...),
    unit: str = Form(...),
    amount: str | None = Form(None),
    is_active: str | None = Form(None),
    sort_order: int = Form(...),
    description: str | None = Form(None),
    db: Session = Depends(get_db),
    auth: dict[str, object] = Depends(
        require_permission(fiber_cost_items_service.WRITE_SCOPE)
    ),
):
    try:
        actor_id, actor_type = _actor(auth)
        command = UpdateFiberCostItemCommand(
            context=_command_context(
                actor_id=actor_id,
                actor_type=actor_type,
                action="updated",
                identity=f"{item_id}:v{expected_version}",
            ),
            actor_id=actor_id,
            actor_type=actor_type,
            item_id=item_id,
            expected_version=expected_version,
            label=label,
            unit=fiber_cost_items_service.parse_unit(unit),
            amount=fiber_cost_items_service.parse_amount(amount),
            is_active=is_active is not None,
            sort_order=sort_order,
            description=description,
        )
        db_session_adapter.release_read_transaction(db)
        fiber_cost_items_service.update_item(db, command)
    except DomainError as exc:
        return _redirect_with_error(exc.message)
    return RedirectResponse(_LIST_URL, status_code=303)
