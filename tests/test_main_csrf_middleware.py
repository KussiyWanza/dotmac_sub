from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from starlette.requests import Request
from starlette.responses import Response

from app.csrf import CSRF_COOKIE_NAME
from app.main import (
    api_sync_pressure_guard_middleware,
    csrf_middleware,
    security_headers_middleware,
    view_as_readonly_middleware,
)


def _build_request(
    *,
    path: str = "/admin/billing",
    method: str = "GET",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Request:
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": b"",
        "headers": headers or [],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(scope, receive)


def _run_async(awaitable):
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(asyncio.run, awaitable)
        return future.result()


def test_csrf_middleware_returns_204_when_no_response_and_request_still_connected(
    monkeypatch, caplog
):
    request = _build_request()

    async def _connected() -> bool:
        return False

    monkeypatch.setattr(request, "is_disconnected", _connected)

    async def call_next(_request: Request) -> Response:
        raise RuntimeError("No response returned.")

    response = _run_async(csrf_middleware(request, call_next))

    assert response.status_code == 204
    assert "reload_or_shutdown" in caplog.text
    assert any(record.levelname == "INFO" for record in caplog.records)


def test_csrf_middleware_returns_204_for_actual_disconnect(monkeypatch):
    request = _build_request()

    async def _disconnected() -> bool:
        return True

    monkeypatch.setattr(request, "is_disconnected", _disconnected)

    async def call_next(_request: Request) -> Response:
        raise RuntimeError("No response returned.")

    response = _run_async(csrf_middleware(request, call_next))

    assert response.status_code == 204


def test_view_as_readonly_middleware_returns_204_when_no_response(monkeypatch):
    request = _build_request(path="/admin/customers")

    async def _disconnected() -> bool:
        return True

    monkeypatch.setattr(request, "is_disconnected", _disconnected)

    async def call_next(_request: Request) -> Response:
        raise RuntimeError("No response returned.")

    response = _run_async(view_as_readonly_middleware(request, call_next))

    assert response.status_code == 204


def test_security_headers_middleware_returns_204_when_no_response(monkeypatch):
    request = _build_request(path="/admin/customers")

    async def _disconnected() -> bool:
        return True

    monkeypatch.setattr(request, "is_disconnected", _disconnected)

    async def call_next(_request: Request) -> Response:
        raise RuntimeError("No response returned.")

    response = _run_async(security_headers_middleware(request, call_next))

    assert response.status_code == 204


def test_api_sync_pressure_guard_blocks_listed_sync_ip(monkeypatch):
    request = _build_request(
        path="/api/v1/sync/customers",
        headers=[(b"x-forwarded-for", b"149.102.158.167")],
    )
    calls: list[tuple[str, int, int]] = []

    def fake_allow_operation(key: str, *, limit: int, window_seconds: int, now=None):
        calls.append((key, limit, window_seconds))
        return SimpleNamespace(allowed=False, retry_after_seconds=17)

    monkeypatch.setenv("API_SYNC_PRESSURE_OFFENDER_LIMIT", "12")
    monkeypatch.setenv("API_SYNC_PRESSURE_WINDOW_SECONDS", "30")
    monkeypatch.setattr(
        "app.services.rate_limiter_adapter.allow_operation", fake_allow_operation
    )

    async def call_next(_request: Request) -> Response:
        raise AssertionError("blocked API sync traffic must not reach downstream")

    response = _run_async(api_sync_pressure_guard_middleware(request, call_next))

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "17"
    assert calls == [("api-v1-pressure:listed:149.102.158.167", 12, 30)]


def test_api_sync_pressure_guard_uses_bounded_feed_bucket(monkeypatch):
    request = _build_request(
        path="/api/v1/payments/sync",
        headers=[(b"x-forwarded-for", b"149.102.158.167")],
    )
    calls: list[tuple[str, int, int]] = []

    def fake_allow_operation(key: str, *, limit: int, window_seconds: int, now=None):
        calls.append((key, limit, window_seconds))
        return SimpleNamespace(allowed=True, retry_after_seconds=None)

    monkeypatch.setenv("API_SYNC_PRESSURE_FEED_LIMIT", "75")
    monkeypatch.setattr(
        "app.services.rate_limiter_adapter.allow_operation", fake_allow_operation
    )

    async def call_next(_request: Request) -> Response:
        return Response("ok", status_code=200)

    response = _run_async(api_sync_pressure_guard_middleware(request, call_next))

    assert response.status_code == 200
    assert calls == [("api-v1-pressure:feed:149.102.158.167", 75, 60)]


def test_api_sync_pressure_guard_allows_admin_without_rate_limit(monkeypatch):
    request = _build_request(path="/admin/customers")

    def fake_allow_operation(*args, **kwargs):
        raise AssertionError("admin paths must not hit the API pressure limiter")

    monkeypatch.setattr(
        "app.services.rate_limiter_adapter.allow_operation", fake_allow_operation
    )

    async def call_next(_request: Request) -> Response:
        return Response("ok", status_code=200)

    response = _run_async(api_sync_pressure_guard_middleware(request, call_next))

    assert response.status_code == 200


def test_api_sync_pressure_guard_uses_general_bucket_for_other_api_ips(monkeypatch):
    request = _build_request(
        path="/api/v1/subscribers",
        headers=[(b"x-forwarded-for", b"203.0.113.9")],
    )
    calls: list[tuple[str, int, int]] = []

    def fake_allow_operation(key: str, *, limit: int, window_seconds: int, now=None):
        calls.append((key, limit, window_seconds))
        return SimpleNamespace(allowed=True, retry_after_seconds=None)

    monkeypatch.setenv("API_SYNC_PRESSURE_PER_IP_LIMIT", "240")
    monkeypatch.setattr(
        "app.services.rate_limiter_adapter.allow_operation", fake_allow_operation
    )

    async def call_next(_request: Request) -> Response:
        return Response("ok", status_code=200)

    response = _run_async(api_sync_pressure_guard_middleware(request, call_next))

    assert response.status_code == 200
    assert calls == [("api-v1-pressure:general:203.0.113.9", 240, 60)]


def test_csrf_middleware_exempts_customer_logout_without_token():
    request = _build_request(path="/portal/auth/logout", method="POST")

    async def call_next(_request: Request) -> Response:
        return Response(status_code=303, headers={"location": "/portal/auth/login"})

    response = _run_async(csrf_middleware(request, call_next))

    assert response.status_code == 303
    assert response.headers["location"] == "/portal/auth/login"


def test_attendance_check_in_requires_csrf_header():
    request = _build_request(path="/admin/dashboard/attendance/check-in", method="POST")

    async def call_next(_request: Request) -> Response:
        raise AssertionError("attendance POST without CSRF must not reach its route")

    response = _run_async(csrf_middleware(request, call_next))

    assert response.status_code == 403


def test_attendance_check_out_accepts_matching_double_submit_csrf():
    token = "attendance-csrf-token"
    request = _build_request(
        path="/admin/dashboard/attendance/check-out",
        method="POST",
        headers=[
            (b"cookie", f"{CSRF_COOKIE_NAME}={token}".encode()),
            (b"x-csrf-token", token.encode()),
        ],
    )

    async def call_next(_request: Request) -> Response:
        return Response(status_code=200)

    response = _run_async(csrf_middleware(request, call_next))

    assert response.status_code == 200
