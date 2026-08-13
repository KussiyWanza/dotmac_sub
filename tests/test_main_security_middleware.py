from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

from starlette.requests import Request
from starlette.responses import Response

from app.main import (
    fiber_widget_cors_middleware,
    login_rate_limit_middleware,
    security_headers_middleware,
)


def _build_request(
    *,
    path: str = "/portal/auth/login",
    method: str = "POST",
    scheme: str = "http",
    headers: list[tuple[bytes, bytes]] | None = None,
    client: tuple[str, int] = ("203.0.113.5", 12345),
) -> Request:
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": scheme,
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": b"",
        "headers": headers or [],
        "client": client,
        "server": ("testserver", 80),
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(scope, receive)


def _run_async(awaitable):
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, awaitable).result()


async def _ok(_request: Request) -> Response:
    return Response(status_code=200)


def test_security_headers_added_on_plain_http():
    request = _build_request(path="/portal/dashboard", method="GET")
    response = _run_async(security_headers_middleware(request, _ok))
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    # No HSTS over plain http.
    assert "Strict-Transport-Security" not in response.headers


def test_hsts_added_when_request_is_https_via_forwarded_proto():
    request = _build_request(
        path="/portal/dashboard",
        method="GET",
        headers=[(b"x-forwarded-proto", b"https")],
    )
    response = _run_async(security_headers_middleware(request, _ok))
    assert "max-age=63072000" in response.headers["Strict-Transport-Security"]
    assert "includeSubDomains" in response.headers["Strict-Transport-Security"]


def test_login_rate_limit_allows_then_blocks(monkeypatch):
    # Deterministic, isolated key per test run via a unique IP.
    ip = "198.51.100.77"
    monkeypatch.setenv("LOGIN_RATE_LIMIT_MAX", "3")
    monkeypatch.setenv("LOGIN_RATE_LIMIT_WINDOW_SECONDS", "300")
    headers = [(b"x-forwarded-for", ip.encode())]

    statuses = []
    for _ in range(4):
        request = _build_request(path="/api/v1/auth/login", headers=headers)
        resp = _run_async(login_rate_limit_middleware(request, _ok))
        statuses.append(resp.status_code)

    assert statuses[:3] == [200, 200, 200]
    assert statuses[3] == 429


def test_login_rate_limit_ignores_non_login_paths(monkeypatch):
    monkeypatch.setenv("LOGIN_RATE_LIMIT_MAX", "1")
    request = _build_request(path="/api/v1/me/invoices", method="POST")
    # Even repeated, a non-login path is never throttled here.
    for _ in range(3):
        resp = _run_async(login_rate_limit_middleware(request, _ok))
        assert resp.status_code == 200


def test_login_rate_limit_ignores_get(monkeypatch):
    monkeypatch.setenv("LOGIN_RATE_LIMIT_MAX", "1")
    for _ in range(3):
        request = _build_request(path="/auth/login", method="GET")
        resp = _run_async(login_rate_limit_middleware(request, _ok))
        assert resp.status_code == 200


def test_fiber_widget_cors_allows_only_exact_fiber_origin():
    allowed_headers = [(b"origin", b"https://fiber.dotmac.ng")]
    request = _build_request(
        path="/widget/fiber/session",
        method="OPTIONS",
        headers=allowed_headers,
    )

    response = _run_async(fiber_widget_cors_middleware(request, _ok))

    assert response.status_code == 204
    assert response.headers["Access-Control-Allow-Origin"] == (
        "https://fiber.dotmac.ng"
    )
    assert "X-Visitor-Token" in response.headers["Access-Control-Allow-Headers"]


def test_fiber_widget_cors_rejects_other_origins_before_route():
    request = _build_request(
        path="/widget/fiber/session",
        method="OPTIONS",
        headers=[(b"origin", b"https://example.com")],
    )

    async def must_not_run(_request: Request) -> Response:
        raise AssertionError("disallowed preflight must not reach the route")

    response = _run_async(fiber_widget_cors_middleware(request, must_not_run))

    assert response.status_code == 403
