import logging
import os
import time
import uuid
from contextvars import ContextVar

from jose import JWTError, jwt
from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.metrics import REQUEST_COUNT, REQUEST_ERRORS, REQUEST_LATENCY

logger = logging.getLogger(__name__)
_SKIP_OBSERVABILITY_PATHS = {"/health", "/metrics"}

# The current request's x-request-id, for propagation onto outbound
# integration calls (crm, erp). Set by ObservabilityMiddleware; empty outside
# a request (e.g. Celery workers), in which case outbound headers omit it.
request_id_var: ContextVar[str] = ContextVar("request_id", default="")


def get_request_id() -> str:
    """The active request's x-request-id, or '' when not in a request."""
    return request_id_var.get()


def _extract_bearer_token(request: Request) -> str | None:
    auth = request.headers.get("authorization")
    if not auth:
        return None
    parts = auth.split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None


def _jwt_secret() -> str | None:
    """The same secret `auth_flow` signs with, reached the same way.

    Environment, then the value held at boot. Reading only the environment
    meant that a deployment keeping its JWT secret in OpenBao — which is every
    deployment that configures one — silently failed to decode any token here,
    so `actor_id` was absent from every log line and metric label while
    authentication itself worked perfectly. A verification that quietly gives
    up looks identical to unauthenticated traffic.
    """

    from dotmac_kernel.secret_sources import get_secret as held_secret

    return os.getenv("JWT_SECRET") or held_secret("jwt_secret")


def _jwt_algorithm() -> str:
    return os.getenv("JWT_ALGORITHM", "HS256")


def _extract_actor_id_from_jwt(token: str | None) -> str | None:
    if not token:
        return None
    secret = _jwt_secret()
    if not secret:
        return None
    try:
        payload = jwt.decode(token, secret, algorithms=[_jwt_algorithm()])
    except JWTError:
        return None
    subject = payload.get("sub")
    if subject:
        return str(subject)
    return None


def _request_path(request: Request) -> str:
    route = request.scope.get("route")
    if route is not None:
        path = getattr(route, "path", None)
        if isinstance(path, str):
            return path
    return request.url.path


def _should_skip_observability(path: str) -> bool:
    return path in _SKIP_OBSERVABILITY_PATHS


class ObservabilityMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        scope.setdefault("state", {})["request_id"] = request_id
        request_id_var.set(request_id)
        path = _request_path(request)
        if _should_skip_observability(path):
            await self.app(scope, receive, send)
            return
        token = _extract_bearer_token(request)
        actor_id = scope["state"].get("actor_id") or _extract_actor_id_from_jwt(token)
        start = time.monotonic()
        status_code = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = MutableHeaders(scope=message)
                headers["x-request-id"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            duration_ms = (time.monotonic() - start) * 1000.0
            REQUEST_COUNT.labels(request.method, path, str(status_code)).inc()
            REQUEST_LATENCY.labels(request.method, path, str(status_code)).observe(
                duration_ms / 1000.0
            )
            REQUEST_ERRORS.labels(request.method, path, str(status_code)).inc()
            logger.exception(
                "request_failed",
                extra={
                    "request_id": request_id,
                    "actor_id": actor_id,
                    "path": path,
                    "method": request.method,
                    "status": status_code,
                    "duration_ms": round(duration_ms, 2),
                },
            )
            raise

        duration_ms = (time.monotonic() - start) * 1000.0
        REQUEST_COUNT.labels(request.method, path, str(status_code)).inc()
        REQUEST_LATENCY.labels(request.method, path, str(status_code)).observe(
            duration_ms / 1000.0
        )
        if status_code >= 500:
            REQUEST_ERRORS.labels(request.method, path, str(status_code)).inc()
        logger.info(
            "request_completed",
            extra={
                "request_id": request_id,
                "actor_id": actor_id,
                "path": path,
                "method": request.method,
                "status": status_code,
                "duration_ms": round(duration_ms, 2),
            },
        )
