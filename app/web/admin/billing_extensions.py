"""Admin billing service-extension (outage compensation) routes."""

import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.service_extension import ServiceExtensionScope
from app.services import service_extensions as service_extensions_service
from app.services import web_admin as web_admin_service
from app.services import web_billing_service_extensions
from app.services.auth_dependencies import require_permission
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/billing", tags=["web-admin-billing"])
logger = logging.getLogger(__name__)


def _extension_http_error(exc: DomainError) -> HTTPException:
    if exc.code.endswith("extension_not_found"):
        status_code = 404
    elif exc.code.endswith(
        (
            "transition_conflict",
            "idempotency_conflict",
            "active_caller_transaction",
        )
    ):
        status_code = 409
    else:
        status_code = 400
    return HTTPException(status_code=status_code, detail=exc.message)


def _context(request: Request, db: Session, extra: dict) -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        **extra,
        "active_page": "service-extensions",
        "active_menu": "billing",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


def _form_context(
    db: Session,
    *,
    idempotency_key: str,
    error: str | None = None,
    values: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "scope_options": service_extensions_service.scope_options(db),
        "form_values": {
            "idempotency_key": idempotency_key,
            "reason": "",
            "window_start": "",
            "window_end": "",
            "days": 1,
            "scope_type": ServiceExtensionScope.network.value,
            "scope_id": "",
            "subscriber_identifiers": "",
            "subscriber_ids": (),
            **(values or {}),
        },
        "error": error,
    }


def _parse_window(value: str, label: str) -> datetime:
    from fastapi import HTTPException

    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400, detail=f"Invalid {label} date/time"
        ) from exc


def _parse_optional_uuid(value: str | None, label: str) -> uuid.UUID | None:
    cleaned = str(value or "").strip()
    if not cleaned:
        return None
    try:
        return uuid.UUID(cleaned)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid {label}") from exc


def _command_context(
    request: Request,
    *,
    scope: str,
    reason: str,
    idempotency_key: str,
) -> CommandContext:
    command_id = uuid.uuid4()
    raw_request_id = str(getattr(request.state, "request_id", "") or "").strip()
    try:
        correlation_id = uuid.UUID(raw_request_id)
    except ValueError:
        correlation_id = command_id
    auth = getattr(request.state, "auth", {}) or {}
    principal_type = str(auth.get("principal_type") or "user")
    actor_prefix = (
        "api_key"
        if principal_type == "api_key"
        else "service"
        if principal_type == "service"
        else "user"
    )
    actor_id = web_admin_service.get_actor_id(request) or "unknown"
    return CommandContext(
        command_id=command_id,
        correlation_id=correlation_id,
        actor=f"{actor_prefix}:{actor_id}",
        scope=scope,
        reason=reason,
        idempotency_key=idempotency_key,
    )


def _subscriber_scope_inputs(
    subscriber_ids: list[str] | None,
    subscriber_identifiers: str | None,
) -> tuple[list[str] | None, bool]:
    selected_ids: list[str] = []
    pasted_identifiers: list[str] = []
    for raw_value in subscriber_ids or []:
        for item in str(raw_value or "").splitlines():
            value = item.strip()
            if value:
                selected_ids.append(value)
    for item in str(subscriber_identifiers or "").splitlines():
        value = item.strip()
        if value:
            pasted_identifiers.append(value)
    if selected_ids:
        if all(_is_uuid(value) for value in selected_ids):
            return selected_ids, True
        return [*selected_ids, *pasted_identifiers], False
    if pasted_identifiers:
        return pasted_identifiers, False
    return None, False


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(str(value))
    except (TypeError, ValueError):
        return False
    return True


def _nonblank_lines(values: list[str] | None) -> list[str]:
    lines: list[str] = []
    for raw_value in values or []:
        for item in str(raw_value or "").splitlines():
            value = item.strip()
            if value:
                lines.append(value)
    return lines


def _service_extension_failure_diagnostics(
    request: Request,
    *,
    detail: str,
    reason: str | None,
    window_start: str | None,
    window_end: str | None,
    days: int | None,
    scope_type: str | None,
    scope_id: str | None,
    subscriber_ids: list[str] | None,
    subscriber_identifiers: str | None,
    resolved_ids: list[str] | None,
    ids_resolved: bool,
) -> dict:
    selected_lines = _nonblank_lines(subscriber_ids)
    pasted_lines = _nonblank_lines([subscriber_identifiers or ""])
    return {
        "event": "service_extension_create_failed",
        "request_id": getattr(request.state, "request_id", None),
        "actor_id": web_admin_service.get_actor_id(request),
        "path": str(request.url.path),
        "method": request.method,
        "status": 400,
        "validation_detail": detail,
        "scope_type": scope_type,
        "scope_id_present": bool(str(scope_id or "").strip()),
        "reason_present": bool(str(reason or "").strip()),
        "window_start_present": bool(str(window_start or "").strip()),
        "window_end_present": bool(str(window_end or "").strip()),
        "days": days,
        "subscriber_ids_field_count": len(subscriber_ids or []),
        "subscriber_ids_nonblank_count": len(selected_lines),
        "subscriber_ids_uuid_count": sum(
            1 for value in selected_lines if _is_uuid(value)
        ),
        "subscriber_identifiers_line_count": len(pasted_lines),
        "resolved_subscriber_count": len(resolved_ids or []),
        "subscriber_ids_resolved": ids_resolved,
    }


@router.get(
    "/service-extensions",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:extension:read"))],
)
def service_extensions_list(
    request: Request,
    limit: int = Query(50, ge=10, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    extensions = service_extensions_service.list_extensions(
        db, limit=limit, offset=offset
    )
    return templates.TemplateResponse(
        "admin/billing/service_extensions.html",
        _context(request, db, {"extensions": extensions}),
    )


@router.get(
    "/service-extensions/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:extension:create"))],
)
def service_extension_new(request: Request, db: Session = Depends(get_db)):
    idempotency_key = str(uuid.uuid4())
    return templates.TemplateResponse(
        "admin/billing/service_extension_form.html",
        _context(
            request,
            db,
            _form_context(db, idempotency_key=idempotency_key),
        ),
    )


@router.post(
    "/service-extensions",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:extension:create"))],
)
def service_extension_create(
    request: Request,
    reason: str = Form(...),
    window_start: str = Form(...),
    window_end: str = Form(...),
    days: int = Form(...),
    scope_type: str = Form(...),
    scope_id: str | None = Form(None),
    subscriber_ids: list[str] = Form(default=[]),
    subscriber_identifiers: str | None = Form(None),
    idempotency_key: str = Form(...),
    db: Session = Depends(get_db),
):
    ids: list[str] | None = None
    ids_resolved = False
    try:
        scope = ServiceExtensionScope(scope_type)
        ids, ids_resolved = _subscriber_scope_inputs(
            subscriber_ids, subscriber_identifiers
        )
        outcome = service_extensions_service.create_service_extension(
            db,
            service_extensions_service.CreateServiceExtensionCommand(
                context=_command_context(
                    request,
                    scope=service_extensions_service.CREATE_SCOPE,
                    reason=reason,
                    idempotency_key=idempotency_key,
                ),
                reason=reason,
                window_start=_parse_window(window_start, "outage start"),
                window_end=_parse_window(window_end, "outage end"),
                days=days,
                scope_type=scope,
                scope_id=_parse_optional_uuid(scope_id, "scope identifier"),
                subscriber_identifiers=tuple(ids or ()),
                subscriber_ids_resolved=ids_resolved,
            ),
        )
    except (DomainError, HTTPException, ValueError) as exc:
        detail = (
            getattr(exc, "detail", None) or getattr(exc, "message", None) or str(exc)
        )
        logger.warning(
            "service_extension_create_failed",
            extra=_service_extension_failure_diagnostics(
                request,
                detail=str(detail),
                reason=reason,
                window_start=window_start,
                window_end=window_end,
                days=days,
                scope_type=scope_type,
                scope_id=scope_id,
                subscriber_ids=subscriber_ids,
                subscriber_identifiers=subscriber_identifiers,
                resolved_ids=ids,
                ids_resolved=ids_resolved,
            ),
        )
        return templates.TemplateResponse(
            "admin/billing/service_extension_form.html",
            _context(
                request,
                db,
                _form_context(
                    db,
                    idempotency_key=idempotency_key,
                    error=str(detail),
                    values={
                        "reason": reason,
                        "window_start": window_start,
                        "window_end": window_end,
                        "days": days,
                        "scope_type": scope_type,
                        "scope_id": scope_id or "",
                        "subscriber_identifiers": subscriber_identifiers or "",
                        "subscriber_ids": tuple(subscriber_ids),
                    },
                ),
            ),
            status_code=400,
        )
    return RedirectResponse(
        url=f"/admin/billing/service-extensions/{outcome.extension_id}",
        status_code=303,
    )


@router.get(
    "/service-extensions/{extension_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:extension:read"))],
)
def service_extension_detail(
    request: Request, extension_id: str, db: Session = Depends(get_db)
):
    try:
        projection = web_billing_service_extensions.build_service_extension_detail(
            db,
            extension_id=uuid.UUID(extension_id),
            auth=getattr(request.state, "auth", None),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="Service extension identifier is invalid.",
        ) from exc
    except DomainError as exc:
        raise _extension_http_error(exc) from exc
    return templates.TemplateResponse(
        "admin/billing/service_extension_detail.html",
        _context(
            request,
            db,
            {"detail": projection},
        ),
    )


@router.post(
    "/service-extensions/{extension_id}/apply",
    dependencies=[Depends(require_permission("billing:extension:apply"))],
)
def service_extension_apply(
    request: Request,
    extension_id: str,
    idempotency_key: str = Form(...),
    db: Session = Depends(get_db),
):
    try:
        service_extensions_service.apply_service_extension(
            db,
            service_extensions_service.ApplyServiceExtensionCommand(
                context=_command_context(
                    request,
                    scope=service_extensions_service.APPLY_SCOPE,
                    reason="Apply service extension",
                    idempotency_key=idempotency_key,
                ),
                extension_id=uuid.UUID(extension_id),
            ),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="Service extension identifier is invalid.",
        ) from exc
    except DomainError as exc:
        raise _extension_http_error(exc) from exc
    return RedirectResponse(
        url=f"/admin/billing/service-extensions/{extension_id}", status_code=303
    )


@router.post(
    "/service-extensions/{extension_id}/cancel",
    dependencies=[Depends(require_permission("billing:extension:apply"))],
)
def service_extension_cancel(
    request: Request,
    extension_id: str,
    idempotency_key: str = Form(...),
    db: Session = Depends(get_db),
):
    try:
        service_extensions_service.cancel_service_extension(
            db,
            service_extensions_service.CancelServiceExtensionCommand(
                context=_command_context(
                    request,
                    scope=service_extensions_service.CANCEL_SCOPE,
                    reason="Cancel service extension",
                    idempotency_key=idempotency_key,
                ),
                extension_id=uuid.UUID(extension_id),
            ),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="Service extension identifier is invalid.",
        ) from exc
    except DomainError as exc:
        raise _extension_http_error(exc) from exc
    return RedirectResponse(
        url=f"/admin/billing/service-extensions/{extension_id}",
        status_code=303,
    )
