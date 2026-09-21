from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote_plus, urlparse

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.datastructures import MutableHeaders, UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.services.application_exception_observability import (
    record_unhandled_http_exception,
)
from app.services.domain_errors import DomainError
from app.services.ticket_work_order_handoff import TicketWorkOrderHandoffError
from app.services.unit_of_work import ConcurrencyConflict
from app.services.vendor_portal_errors import VendorPortalOperationError
from app.services.work_order_errors import WorkOrderCommandError
from app.web.auth.dependencies import AuthenticationRequired
from app.web.customer.branding import register_customer_portal_filters

logger = logging.getLogger(__name__)
templates = Jinja2Templates(directory="templates")
# Error pages extend layouts/customer.html, which uses the customer-portal
# display filters (portal_datetime, currency_amount). Those are registered
# per-Templates-instance, so this standalone error-handler instance needs them
# too — without this a 404/403 on any portal path 500s on an unknown filter.
register_customer_portal_filters(templates)
_TEMPLATES_ROOT = Path("templates")

_FRIENDLY_DEFAULT_BAD_REQUEST = "Some required information is missing or invalid. Please check the form and try again."
_REDIRECT_ERROR_TOKEN_TO_STATUS = {
    "forbidden": 403,
    "access_denied": 403,
    "unauthorized": 403,
    "not_found": 404,
    "missing": 404,
    "does_not_exist": 404,
    "invalid": 400,
    "bad_request": 400,
}


def _error_payload(code: str, message: str, details: object, request_id: str | None):
    return {
        "code": code,
        "message": message,
        "details": details,
        "request_id": request_id,
    }


def _is_html_request(request: Request) -> bool:
    accept = (request.headers.get("accept") or "").lower()
    content_type = (request.headers.get("content-type") or "").lower()
    if request.headers.get("HX-Request", "").lower() == "true":
        return False
    if request.url.path.startswith("/api/"):
        return False
    if "application/json" in content_type:
        return False
    if "application/json" in accept and "text/html" not in accept:
        return False
    return "text/html" in accept or not request.url.path.startswith("/api/")


def _request_id(request: Request) -> str:
    rid = getattr(request.state, "request_id", None)
    return str(rid) if rid else "unknown"


def _validation_error_summary(exc: RequestValidationError) -> list[dict[str, object]]:
    summary: list[dict[str, object]] = []
    for error in exc.errors():
        item = dict(error)
        summary.append(
            {
                "loc": [str(part) for part in item.get("loc", [])],
                "type": str(item.get("type", "")),
                "msg": str(item.get("msg", "")),
            }
        )
    return summary


def _friendly_bad_request_message(detail: object) -> str:
    if isinstance(detail, str):
        msg = detail.strip()
        if not msg:
            return _FRIENDLY_DEFAULT_BAD_REQUEST
        lowered = msg.lower()
        if any(
            token in lowered
            for token in (
                "validation error",
                "type_error",
                "value_error",
                "traceback",
                "{",
                "[",
            )
        ):
            return _FRIENDLY_DEFAULT_BAD_REQUEST
        return msg
    if isinstance(detail, dict):
        for key in ("message", "detail", "error"):
            val = detail.get(key)
            if isinstance(val, str) and val.strip():
                lowered = val.lower()
                if any(
                    token in lowered
                    for token in ("type_error", "value_error", "traceback")
                ):
                    return _FRIENDLY_DEFAULT_BAD_REQUEST
                return val.strip()
    return _FRIENDLY_DEFAULT_BAD_REQUEST


def _login_redirect_for_path(request: Request) -> str:
    next_url = str(request.url.path)
    if request.url.query:
        next_url += f"?{request.url.query}"
    quoted_next = quote(next_url, safe="")
    path = request.url.path
    if path.startswith("/portal/"):
        return f"/portal/auth/login?next={quoted_next}"
    if path.startswith("/reseller/"):
        return f"/reseller/auth/login?next={quoted_next}"
    if path.startswith("/vendor/"):
        return f"/vendor/auth/login?next={quoted_next}"
    return f"/auth/login?next={quoted_next}"


def _friendly_redirect_error_message(error_value: str, status_code: int) -> str:
    token = error_value.strip().lower().replace(" ", "_")
    if token in {"forbidden", "access_denied", "unauthorized"}:
        return "You do not have permission to access this page."
    if token in {"not_found", "missing", "does_not_exist"}:
        return "The requested item could not be found."
    if status_code == 400:
        return _FRIENDLY_DEFAULT_BAD_REQUEST
    return error_value.strip()


def _template_response(request: Request, status_code: int, message: str):
    template_name = f"errors/{status_code}.html"
    # Route to the context-specific branded error template (which carries the
    # correct "back to dashboard" link for that portal) when one exists;
    # otherwise fall back to the generic template. Previously only /admin was
    # routed, so /portal and /reseller errors used the generic template whose
    # "Go to dashboard" link points at /admin/dashboard.
    path = request.url.path
    context_dir = (
        "admin"
        if path.startswith("/admin")
        else "customer"
        if path.startswith("/portal")
        else "reseller"
        if path.startswith("/reseller")
        else None
    )
    if context_dir:
        candidate = _TEMPLATES_ROOT / context_dir / "errors" / f"{status_code}.html"
        if candidate.exists():
            template_name = f"{context_dir}/errors/{status_code}.html"
    return templates.TemplateResponse(
        request,
        template_name,
        {
            "request": request,
            "message": message,
            "request_id": _request_id(request),
        },
        status_code=status_code,
    )


class RedirectErrorTemplateMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        captured: list[Message] = []

        async def send_wrapper(asgi_message: Message) -> None:
            captured.append(dict(asgi_message))

        await self.app(scope, receive, send_wrapper)

        start_message = next(
            (
                message
                for message in captured
                if message["type"] == "http.response.start"
            ),
            None,
        )
        if start_message is None:
            return

        headers = MutableHeaders(scope=start_message)
        if request.headers.get("HX-Request", "").lower() == "true":
            for asgi_message in captured:
                await send(asgi_message)
            return

        status_code = start_message["status"]
        location = headers.get("location")
        if (
            status_code not in {301, 302, 303, 307, 308}
            or not location
            or "error=" not in location
        ):
            for asgi_message in captured:
                await send(asgi_message)
            return

        parsed = urlparse(location)
        params = parse_qs(parsed.query or "")
        raw = params.get("error", [None])[0]
        if raw is None:
            for asgi_message in captured:
                await send(asgi_message)
            return

        raw_message = unquote_plus(str(raw)).strip()
        token = raw_message.lower().replace(" ", "_")
        mapped_status = _REDIRECT_ERROR_TOKEN_TO_STATUS.get(token)
        if mapped_status is None:
            for asgi_message in captured:
                await send(asgi_message)
            return

        friendly_message = _friendly_redirect_error_message(raw_message, mapped_status)
        response = _template_response(
            request,
            status_code=mapped_status,
            message=friendly_message,
        )
        await response(scope, receive, send)


def register_error_handlers(app) -> None:
    app.add_middleware(RedirectErrorTemplateMiddleware)

    @app.exception_handler(ConcurrencyConflict)
    async def concurrency_conflict_handler(request: Request, exc: ConcurrencyConflict):
        """Handle concurrency conflicts (optimistic locking failures, etc.)."""
        if _is_html_request(request):
            return _template_response(
                request,
                status_code=409,
                message=exc.message,
            )
        return JSONResponse(
            status_code=409,
            content=_error_payload(
                "concurrency_conflict",
                exc.message,
                None,
                _request_id(request),
            ),
        )

    @app.exception_handler(VendorPortalOperationError)
    async def vendor_portal_operation_error_handler(
        request: Request, exc: VendorPortalOperationError
    ):
        """Map vendor-domain rejections only at the HTTP delivery boundary."""

        status_code = {
            "invalid": 422,
            "forbidden": 403,
            "not_found": 404,
            "conflict": 409,
        }[exc.kind]
        if _is_html_request(request) and status_code == 422:
            status_code = 400
        return await _handle_http_exception(
            request,
            status_code,
            exc.message
            if _is_html_request(request)
            else {"code": exc.code, "message": exc.message},
        )

    @app.exception_handler(WorkOrderCommandError)
    async def work_order_command_error_handler(
        request: Request, exc: WorkOrderCommandError
    ):
        """Map work-order domain rejections only at the HTTP boundary."""

        status_code = {
            "invalid": 422,
            "forbidden": 403,
            "not_found": 404,
            "conflict": 409,
        }[exc.kind]
        if _is_html_request(request) and status_code == 422:
            status_code = 400
        return await _handle_http_exception(
            request,
            status_code,
            exc.message
            if _is_html_request(request)
            else {"code": exc.code, "message": exc.message},
        )

    @app.exception_handler(TicketWorkOrderHandoffError)
    async def ticket_work_order_handoff_error_handler(
        request: Request, exc: TicketWorkOrderHandoffError
    ):
        """Map ticket handoff rejections only at the HTTP boundary."""

        status_code = {
            "invalid": 422,
            "forbidden": 403,
            "not_found": 404,
            "conflict": 409,
        }[exc.kind]
        if _is_html_request(request) and status_code == 422:
            status_code = 400
        return await _handle_http_exception(
            request,
            status_code,
            exc.message
            if _is_html_request(request)
            else {"code": exc.code, "message": exc.message},
        )

    @app.exception_handler(DomainError)
    async def domain_error_handler(request: Request, exc: DomainError):
        """Map transport-neutral service failures at the shared HTTP adapter."""

        code = exc.code.lower()
        if code.endswith("not_found") or "_not_found" in code:
            status_code = 404
        elif "permission" in code or "forbidden" in code:
            status_code = 403
        elif any(
            marker in code
            for marker in (
                "invalid",
                "required",
                "not_eligible",
            )
        ):
            status_code = 422
        elif any(
            marker in code
            for marker in (
                "mismatch",
                "command_contract_violation",
                "nested_owner_command",
                "nested_transaction_completion",
            )
        ):
            status_code = 409 if "mismatch" in code else 500
        else:
            status_code = 409
        if _is_html_request(request) and status_code == 422:
            status_code = 400
        return await _handle_http_exception(
            request,
            status_code,
            exc.message
            if _is_html_request(request)
            else {"code": exc.code, "message": exc.message, "details": exc.details},
        )

    @app.exception_handler(AuthenticationRequired)
    async def auth_required_handler(request: Request, exc: AuthenticationRequired):
        """Redirect to login page when authentication is required."""
        return RedirectResponse(url=exc.redirect_url, status_code=303)

    async def _handle_http_exception(
        request: Request, status_code: int, detail: object, headers: object = None
    ):
        if 300 <= status_code < 400 and isinstance(headers, dict):
            location = headers.get("Location")
            if location:
                # A redirect is the whole response: an auth guard that raises
                # 303 + Location must reach the browser as a redirect, not as
                # an error payload that silently drops the destination.
                return RedirectResponse(url=location, status_code=status_code)
        if _is_html_request(request):
            if status_code == 401:
                return RedirectResponse(
                    url=_login_redirect_for_path(request), status_code=303
                )
            if status_code == 400:
                return _template_response(
                    request,
                    status_code=400,
                    message=_friendly_bad_request_message(detail),
                )
            if status_code == 403:
                message = (
                    detail
                    if isinstance(detail, str)
                    else (
                        "You do not have permission to view this page. "
                        "If you believe this is a mistake, please contact your administrator."
                    )
                )
                return _template_response(request, status_code=403, message=message)
            if status_code == 404:
                message = (
                    detail
                    if isinstance(detail, str) and detail.strip()
                    else "Page not found"
                )
                return _template_response(request, status_code=404, message=message)
            if status_code == 409:
                message = (
                    detail
                    if isinstance(detail, str) and detail.strip()
                    else "Request conflict"
                )
                return _template_response(request, status_code=409, message=message)

        detail_payload = detail
        code = f"http_{status_code}"
        message = "Request failed"
        details = None
        if isinstance(detail_payload, dict):
            code = detail_payload.get("code", code)
            message = detail_payload.get("message", message)
            details = detail_payload.get("details")
        elif isinstance(detail_payload, str):
            message = detail_payload
        else:
            details = detail_payload
        return JSONResponse(
            status_code=status_code,
            content=_error_payload(code, message, details, _request_id(request)),
        )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        return await _handle_http_exception(
            request, exc.status_code, exc.detail, getattr(exc, "headers", None)
        )

    @app.exception_handler(StarletteHTTPException)
    async def starlette_http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ):
        detail = (
            exc.detail if getattr(exc, "detail", None) is not None else "Request failed"
        )
        return await _handle_http_exception(
            request, exc.status_code, detail, getattr(exc, "headers", None)
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ):
        summary = _validation_error_summary(exc)
        logger.warning(
            "request_validation_failed",
            extra={
                "request_id": _request_id(request),
                "path": request.url.path,
                "method": request.method,
                "status": 400 if _is_html_request(request) else 422,
                "validation_errors": summary,
            },
        )
        if _is_html_request(request):
            return _template_response(
                request,
                status_code=400,
                message=_FRIENDLY_DEFAULT_BAD_REQUEST,
            )

        # Convert errors to JSON-serializable format.
        def _sanitize_input(value):
            if isinstance(value, bytes):
                return value.decode("utf-8", errors="replace")
            if isinstance(value, UploadFile):
                return value.filename or "upload"
            if isinstance(value, dict):
                return {key: _sanitize_input(val) for key, val in value.items()}
            if isinstance(value, (list, tuple, set)):
                return [_sanitize_input(item) for item in value]
            if isinstance(value, (str, int, float, bool)) or value is None:
                return value
            return str(value)

        # Sanitize the WHOLE error dict, not just "input": a custom
        # @model_validator that raises ValueError puts the raw exception in
        # ctx["error"], which is not JSON-serializable and would otherwise make
        # JSONResponse raise — turning a 422 into a 500. _sanitize_input recurses
        # into ctx and coerces any non-JSON-native value (e.g. the exception) to str.
        errors = [_sanitize_input(dict(error)) for error in exc.errors()]
        return JSONResponse(
            status_code=422,
            content=_error_payload(
                "validation_error", "Validation error", errors, _request_id(request)
            ),
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        fingerprint = record_unhandled_http_exception(exc)
        logger.exception(
            "unhandled_application_exception",
            extra={
                "request_id": _request_id(request),
                "method": request.method,
                "path": request.url.path,
                "route_path": getattr(
                    request.scope.get("route"), "path", "<unmatched>"
                ),
                "status": 500,
                "exception_fingerprint": fingerprint,
            },
        )
        if _is_html_request(request):
            return _template_response(
                request,
                status_code=500,
                message="Oops! Something went wrong on our end. Please try again later.",
            )
        return JSONResponse(
            status_code=500,
            content=_error_payload(
                "internal_error", "Internal server error", None, _request_id(request)
            ),
        )
