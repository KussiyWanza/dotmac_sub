"""Staff workspace for Sub-owned work-order material dependencies."""

from __future__ import annotations

from urllib.parse import urlencode
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import backoffice
from app.services.auth_dependencies import can, require_permission
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.field import material_requests as material_service
from app.services.owner_commands import CommandContext
from app.web.request_parsing import parse_form_data_sync

templates = Jinja2Templates(directory="templates")
router = APIRouter(
    prefix="/operations/material-requests",
    tags=["web-admin-material-requests"],
)

READ_PERMISSION = "operations:material_request:read"
WRITE_PERMISSION = "operations:material_request:write"


def _base_context(request: Request, db: Session) -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": "material-requests",
        "active_menu": "operations",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "can_write_material_requests": can(request, WRITE_PERMISSION),
        "statuses": tuple(material_service.MaterialRequestStatus),
        "priorities": tuple(material_service.MaterialRequestPriority),
    }


def _command_context(
    auth: dict,
    *,
    request_id: UUID,
    scope: str,
    reason: str,
) -> CommandContext:
    principal_id = str(auth.get("principal_id") or "").strip()
    if not principal_id:
        raise material_service.MaterialRequestError(
            code="operations.material_dependencies.invalid_command_context",
            message="Authorized staff identity is missing.",
        )
    principal_type = str(auth.get("principal_type") or "user").strip()
    return CommandContext(
        command_id=request_id,
        correlation_id=request_id,
        actor=f"{principal_type}:{principal_id}",
        scope=scope,
        reason=reason,
        idempotency_key=f"{scope}:{request_id}",
    )


def _status(value: str | None) -> material_service.MaterialRequestStatus | None:
    if not str(value or "").strip():
        return None
    try:
        return material_service.MaterialRequestStatus(str(value).strip().lower())
    except ValueError:
        return None


def _error_status(error: DomainError) -> int:
    suffix = error.code.rsplit(".", 1)[-1]
    if suffix in {
        "request_not_found",
        "work_order_not_found",
        "material_item_not_found",
    }:
        return 404
    if suffix in {"invalid_transition", "idempotency_conflict"}:
        return 409
    return 400


def _scope(
    *,
    ticket_id: str | None = None,
    project_id: str | None = None,
    project_task_id: str | None = None,
) -> material_service.MaterialRequestScope:
    def _uuid(value: str | None) -> UUID | None:
        try:
            return UUID(str(value or "").strip())
        except ValueError:
            return None

    return material_service.MaterialRequestScope(
        ticket_id=_uuid(ticket_id),
        project_id=_uuid(project_id),
        project_task_id=_uuid(project_task_id),
    )


def _detail_response(
    request: Request,
    db: Session,
    request_id: UUID,
    *,
    error: str | None = None,
    notice: str | None = None,
    status_code: int = 200,
):
    try:
        material_request = material_service.get_staff_material_request(db, request_id)
    except DomainError as exc:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": exc.message},
            status_code=404,
        )
    return templates.TemplateResponse(
        "admin/material_requests/detail.html",
        {
            **_base_context(request, db),
            "material_request": material_request,
            "delivery": backoffice.get_material_request_delivery(db, request_id),
            "error": error,
            "notice": notice,
            "request_id": str(uuid4()),
        },
        status_code=status_code,
    )


def _form_response(
    request: Request,
    db: Session,
    *,
    selected_work_order_id: str | None = None,
    scope: material_service.MaterialRequestScope | None = None,
    error: str | None = None,
    values: dict[str, str] | None = None,
    status_code: int = 200,
):
    options = material_service.staff_material_request_form_options(db, scope=scope)
    selected = selected_work_order_id or ""
    if not selected and len(options.work_orders) == 1:
        selected = str(options.work_orders[0].id)
    return templates.TemplateResponse(
        "admin/material_requests/form.html",
        {
            **_base_context(request, db),
            "options": options,
            "selected_work_order_id": selected,
            "material_scope": scope or material_service.MaterialRequestScope(),
            "values": values or {},
            "error": error,
            "request_id": str(uuid4()),
        },
        status_code=status_code,
    )


@router.get("", response_class=HTMLResponse)
def material_request_list(
    request: Request,
    status: str | None = None,
    work_order: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    notice: str | None = None,
    db: Session = Depends(get_db),
    _auth: dict = Depends(require_permission(READ_PERMISSION)),
):
    selected_status = _status(status)
    result = material_service.list_staff_material_requests(
        db,
        status=selected_status,
        work_order_public_id=work_order,
        page=page,
        per_page=per_page,
    )
    total_pages = max(1, (result.total + result.per_page - 1) // result.per_page)
    return templates.TemplateResponse(
        "admin/material_requests/index.html",
        {
            **_base_context(request, db),
            "result": result,
            "status_filter": selected_status.value if selected_status else "",
            "work_order_filter": str(work_order or "").strip(),
            "total_pages": total_pages,
            "notice": notice,
        },
    )


@router.get("/new", response_class=HTMLResponse)
def material_request_new(
    request: Request,
    work_order_id: str | None = None,
    ticket_id: str | None = None,
    project_id: str | None = None,
    project_task_id: str | None = None,
    db: Session = Depends(get_db),
    _auth: dict = Depends(require_permission(WRITE_PERMISSION)),
):
    return _form_response(
        request,
        db,
        selected_work_order_id=work_order_id,
        scope=_scope(
            ticket_id=ticket_id,
            project_id=project_id,
            project_task_id=project_task_id,
        ),
    )


@router.post("", response_class=HTMLResponse)
def material_request_create(
    request: Request,
    db: Session = Depends(get_db),
    auth: dict = Depends(require_permission(WRITE_PERMISSION)),
):
    form = parse_form_data_sync(request)
    item_ids = [str(value).strip() for value in form.getlist("item_id")]
    quantities = [str(value).strip() for value in form.getlist("quantity")]
    item_notes = [str(value).strip() for value in form.getlist("item_notes")]
    values = {
        "priority": str(form.get("priority") or "medium"),
        "source_warehouse_code": str(form.get("source_warehouse_code") or ""),
        "fulfillment_channel": str(form.get("fulfillment_channel") or "erp"),
        "notes": str(form.get("notes") or ""),
    }
    selected_work_order_id = str(form.get("work_order_id") or "").strip()
    material_scope = _scope(
        ticket_id=str(form.get("ticket_id") or ""),
        project_id=str(form.get("project_id") or ""),
        project_task_id=str(form.get("project_task_id") or ""),
    )
    try:
        request_id = UUID(str(form.get("request_id") or ""))
        work_order_id = UUID(selected_work_order_id) if selected_work_order_id else None
        priority = material_service.MaterialRequestPriority(values["priority"])
        lines: list[material_service.MaterialRequestLineInput] = []
        for index, item_id in enumerate(item_ids):
            if not item_id:
                continue
            quantity = int(quantities[index] if index < len(quantities) else "1")
            notes = item_notes[index] if index < len(item_notes) else ""
            lines.append(
                material_service.MaterialRequestLineInput(
                    item_id=UUID(item_id),
                    quantity=quantity,
                    notes=notes or None,
                )
            )
        db_session_adapter.release_read_transaction(db)
        outcome = material_service.create_staff_material_request(
            db,
            material_service.CreateStaffMaterialRequest(
                context=_command_context(
                    auth,
                    request_id=request_id,
                    scope=WRITE_PERMISSION,
                    reason="Create and submit staff material request",
                ),
                scope=material_scope,
                work_order_id=work_order_id,
                request_id=request_id,
                priority=priority,
                fulfillment_channel=material_service.MaterialRequestFulfillmentChannel(
                    values["fulfillment_channel"]
                ),
                source_warehouse_code=values["source_warehouse_code"],
                notes=values["notes"] or None,
                items=tuple(lines),
            ),
        )
    except (DomainError, ValueError) as exc:
        message = exc.message if isinstance(exc, DomainError) else str(exc)
        return _form_response(
            request,
            db,
            selected_work_order_id=selected_work_order_id,
            scope=material_scope,
            values=values,
            error=message,
            status_code=_error_status(exc) if isinstance(exc, DomainError) else 400,
        )
    return RedirectResponse(
        url=f"/admin/operations/material-requests/{outcome.id}?notice=Request+submitted",
        status_code=303,
    )


@router.get("/{request_id}", response_class=HTMLResponse)
def material_request_detail(
    request: Request,
    request_id: UUID,
    notice: str | None = None,
    db: Session = Depends(get_db),
    _auth: dict = Depends(require_permission(READ_PERMISSION)),
):
    return _detail_response(request, db, request_id, notice=notice)


def _review_redirect(
    request_id: UUID, *, notice: str | None = None
) -> RedirectResponse:
    query = urlencode({"notice": notice}) if notice else ""
    suffix = f"?{query}" if query else ""
    return RedirectResponse(
        url=f"/admin/operations/material-requests/{request_id}{suffix}",
        status_code=303,
    )


def _review(
    request: Request,
    db: Session,
    auth: dict,
    request_id: UUID,
    command_id: UUID,
    reason: str | None,
    action: str,
):
    try:
        db_session_adapter.release_read_transaction(db)
        material_service.cancel_material_request(
            db,
            material_service.ReviewMaterialRequest(
                context=_command_context(
                    auth,
                    request_id=command_id,
                    scope=WRITE_PERMISSION,
                    reason=f"{action.title()} material request",
                ),
                request_id=request_id,
                reason=reason,
            ),
        )
    except DomainError as exc:
        return _detail_response(
            request,
            db,
            request_id,
            error=exc.message,
            status_code=_error_status(exc),
        )
    return _review_redirect(request_id, notice="Request canceled")


@router.post("/{request_id}/cancel", response_class=HTMLResponse)
def material_request_cancel(
    request: Request,
    request_id: UUID,
    db: Session = Depends(get_db),
    auth: dict = Depends(require_permission(WRITE_PERMISSION)),
):
    form = parse_form_data_sync(request)
    return _review(
        request,
        db,
        auth,
        request_id,
        UUID(str(form.get("request_id") or "")),
        str(form.get("reason") or ""),
        "cancel",
    )
