"""Admin dispatch work-order routes."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from urllib.parse import urlencode
from uuid import UUID, uuid4, uuid5

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.orm import Session
from starlette.datastructures import FormData, UploadFile

from app.csrf import CSRF_COOKIE_NAME, CSRFValidationError
from app.db import get_db
from app.services import web_dispatch_work_orders as work_orders_service
from app.services import web_work_order_expenses as expense_web
from app.services.auth_dependencies import (
    can,
    grant_scopes_for_permission,
    require_permission,
    require_scoped_permission,
)
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.field.expense_requests import (
    ExpenseReceiptUploadInput,
    ExpenseRequestAccessMode,
    ExpenseRequestLineInput,
    SubmitFieldExpenseRequest,
    submit_field_expense_request_command,
)
from app.services.owner_commands import CommandContext
from app.services.work_order_views import get_work_order_row
from app.web.request_parsing import parse_form_data_sync

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/dispatch", tags=["web-admin-dispatch"])
_WORK_ORDER_READ_PERMISSION = "operations:dispatch:read"
_WORK_ORDER_EXPENSE_PERMISSION = "operations:dispatch:write"


def _ctx(request: Request, db: Session) -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": "dispatch-work-orders",
        "active_menu": "operations",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


def _work_order_scope_for(
    permission_key: str,
) -> Callable[[Request, Session], tuple[str, str] | None]:
    """Build an exact work-order scope resolver for one permission tier."""

    def _work_order_scope(request: Request, db: Session) -> tuple[str, str] | None:
        pair = get_work_order_row(
            db, str(request.path_params.get("work_order_id") or "")
        )
        if pair is None:
            raise HTTPException(status_code=404, detail="Work order not found")
        _, subscriber = pair
        candidates: list[tuple[str, str]] = []
        if subscriber is not None:
            if subscriber.reseller_id is not None:
                candidates.append(("reseller", str(subscriber.reseller_id)))
            if subscriber.region:
                candidates.append(("region", subscriber.region))
        auth = getattr(request.state, "auth", None)
        if isinstance(auth, dict):
            grants = grant_scopes_for_permission(auth, db, permission_key)
            if isinstance(grants, set):
                for candidate in candidates:
                    if candidate in grants:
                        return candidate
        return candidates[0] if candidates else None

    return _work_order_scope


_require_work_order_read_access = require_scoped_permission(
    _WORK_ORDER_READ_PERMISSION, _work_order_scope_for(_WORK_ORDER_READ_PERMISSION)
)
_require_work_order_expense_access = require_scoped_permission(
    _WORK_ORDER_EXPENSE_PERMISSION,
    _work_order_scope_for(_WORK_ORDER_EXPENSE_PERMISSION),
)


def _actor_id(auth: dict) -> UUID:
    if auth.get("principal_type") != "system_user":
        raise HTTPException(status_code=403, detail="Staff access is required")
    try:
        return UUID(str(auth["principal_id"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=403, detail="Authorized actor is missing"
        ) from exc


def _require_expense_csrf(request: Request) -> None:
    """Fail closed at the financial route in addition to global middleware."""

    cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
    form_token = str(parse_form_data_sync(request).get("_csrf_token") or "")
    if (
        not cookie_token
        or not form_token
        or not secrets.compare_digest(cookie_token, form_token)
    ):
        raise CSRFValidationError()


def _form_text(form: FormData, name: str) -> str:
    value = form.get(name)
    return value if isinstance(value, str) else ""


def _expense_form(form: FormData) -> expense_web.WorkOrderExpenseFormInput:
    request_id_raw = _form_text(form, "client_ref").strip()
    try:
        request_id = UUID(request_id_raw)
    except ValueError:
        request_id = uuid4()

    lines: list[expense_web.ExpenseLineFormInput] = []
    seen_keys: set[str] = set()
    for index, value in enumerate(form.getlist("line_key")):
        raw_key = str(value).strip()
        key = raw_key
        if not key or len(key) > 64 or not key.isalnum() or key in seen_keys:
            key = uuid5(request_id, f"line:{index}").hex
        seen_keys.add(key)
        upload_value = form.get(f"receipt_file_{raw_key}")
        receipt_upload: ExpenseReceiptUploadInput | None = None
        if isinstance(upload_value, UploadFile) and upload_value.filename:
            content = upload_value.file.read()
            digest = hashlib.sha256(content).hexdigest()
            receipt_upload = ExpenseReceiptUploadInput(
                file_name=upload_value.filename,
                mime_type=upload_value.content_type,
                content=content,
                client_ref=uuid5(request_id, f"receipt:{index}:{digest}"),
            )
        lines.append(
            expense_web.ExpenseLineFormInput(
                key=key,
                category_code=_form_text(form, f"category_code_{raw_key}"),
                description=_form_text(form, f"description_{raw_key}"),
                amount=_form_text(form, f"amount_{raw_key}"),
                expense_date=_form_text(form, f"line_date_{raw_key}"),
                vendor_name=_form_text(form, f"vendor_{raw_key}"),
                receipt_url=_form_text(form, f"receipt_url_{raw_key}"),
                notes=_form_text(form, f"line_notes_{raw_key}"),
                receipt_upload=receipt_upload,
            )
        )
    return expense_web.WorkOrderExpenseFormInput(
        request_id=str(request_id),
        purpose=_form_text(form, "purpose"),
        expense_date=_form_text(form, "expense_date"),
        currency=_form_text(form, "currency"),
        notes=_form_text(form, "notes"),
        lines=tuple(lines),
    )


def _expense_detail_response(
    request: Request,
    db: Session,
    *,
    work_order_id: str,
    actor_id: UUID,
    notice: str | None = None,
    error: str | None = None,
    expense_form: expense_web.WorkOrderExpenseFormInput | None = None,
    expense_errors: tuple[expense_web.ExpenseFieldError, ...] = (),
    status_code: int = 200,
):
    state = work_orders_service.detail_page(db, work_order_id)
    state["expense_panel"] = expense_web.build_work_order_expense_panel(
        db,
        work_order_public_id=work_order_id,
        actor_system_user_id=actor_id,
        form=expense_form,
        errors=expense_errors,
    )
    context = _ctx(request, db)
    context.update(state)
    context.update({"notice": notice, "error": error})
    return templates.TemplateResponse(
        "admin/dispatch/work_order_detail.html", context, status_code=status_code
    )


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
)
def dispatch_work_order_detail(
    request: Request,
    work_order_id: str,
    notice: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
    auth: dict = Depends(_require_work_order_read_access),
):
    return _expense_detail_response(
        request,
        db,
        work_order_id=work_order_id,
        actor_id=_actor_id(auth),
        notice=notice,
        error=error,
    )


@router.post(
    "/work-orders/{work_order_id}/expenses",
    response_class=HTMLResponse,
    dependencies=[Depends(_require_expense_csrf)],
)
def create_work_order_expense(
    request: Request,
    work_order_id: str,
    db: Session = Depends(get_db),
    auth: dict = Depends(_require_work_order_expense_access),
):
    actor_id = _actor_id(auth)
    raw_form = parse_form_data_sync(request)
    form = _expense_form(raw_form)
    try:
        panel = expense_web.build_work_order_expense_panel(
            db,
            work_order_public_id=work_order_id,
            actor_system_user_id=actor_id,
            form=form,
        )
        prepared = expense_web.validate_work_order_expense_form(
            form,
            category_rules=panel.categories,
        )
        db_session_adapter.release_read_transaction(db)
        outcome = submit_field_expense_request_command(
            db,
            SubmitFieldExpenseRequest(
                context=CommandContext(
                    command_id=prepared.request_id,
                    correlation_id=prepared.request_id,
                    actor=f"user:{actor_id}",
                    scope=_WORK_ORDER_EXPENSE_PERMISSION,
                    reason=f"Create expense for work order {work_order_id}",
                    idempotency_key=str(prepared.request_id),
                ),
                requester_person_id=None,
                work_order_public_id=work_order_id,
                request_id=prepared.request_id,
                purpose=prepared.purpose,
                expense_date=prepared.expense_date,
                currency=prepared.currency,
                notes=prepared.notes,
                items=tuple(
                    ExpenseRequestLineInput(
                        category_code=line.category_code,
                        category_name=line.category_name,
                        description=line.description,
                        amount=line.amount,
                        expense_date=line.expense_date,
                        vendor_name=line.vendor_name,
                        receipt_url=line.receipt_url,
                        receipt_attachment_id=None,
                        notes=line.notes,
                        receipt_upload=line.receipt_upload,
                    )
                    for line in prepared.lines
                ),
                access_mode=ExpenseRequestAccessMode.STAFF_WORK_ORDER,
                authorized_work_order_id=panel.work_order_id,
                category_rules=prepared.category_rules,
            ),
        )
    except expense_web.WorkOrderExpenseFormError as exc:
        redisplay_form, redisplay_errors = expense_web.prepare_form_redisplay(
            exc.form, exc.errors
        )
        return _expense_detail_response(
            request,
            db,
            work_order_id=work_order_id,
            actor_id=actor_id,
            expense_form=redisplay_form,
            expense_errors=redisplay_errors,
            status_code=422,
        )
    except DomainError as exc:
        db_session_adapter.discard_failed_transaction(db)
        redisplay_form, redisplay_errors = expense_web.prepare_form_redisplay(
            form,
            (expense_web.ExpenseFieldError("form", exc.message),),
        )
        return _expense_detail_response(
            request,
            db,
            work_order_id=work_order_id,
            actor_id=actor_id,
            expense_form=redisplay_form,
            expense_errors=redisplay_errors,
            status_code=409,
        )
    return _detail_redirect(
        work_order_id,
        notice=f"Expense claim {outcome.id} submitted",
    )


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
