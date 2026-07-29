from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

import httpx
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError

from app.errors import _validation_error_summary, register_error_handlers
from app.observability import ObservabilityMiddleware


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(ObservabilityMiddleware)
    register_error_handlers(app)

    api_router = APIRouter(prefix="/api/v1")

    @app.get("/web-http-403")
    async def web_http_403():
        raise HTTPException(status_code=403, detail="Forbidden area")

    @app.get("/web-http-409")
    async def web_http_409():
        raise HTTPException(status_code=409, detail="Record already exists")

    @app.get("/web-crash")
    async def web_crash():
        raise RuntimeError("boom")

    @app.get("/redirect-error-known")
    async def redirect_error_known():
        return {"ok": True}

    @app.get("/redirect-error-unknown")
    async def redirect_error_unknown():
        return {"ok": True}

    @api_router.get("/http-403")
    async def api_http_403():
        raise HTTPException(status_code=403, detail="Forbidden api")

    @api_router.get("/needs-int")
    async def api_needs_int(value: int):
        return {"value": value}

    app.include_router(api_router)
    return app


def _request(app: FastAPI, method: str, path: str, **kwargs) -> httpx.Response:
    async def _run() -> httpx.Response:
        transport = httpx.ASGITransport(
            app=app,  # type: ignore[arg-type]
            raise_app_exceptions=False,
        )
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.request(method, path, **kwargs)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(asyncio.run, _run())
        return future.result()


def test_web_404_renders_html_template() -> None:
    resp = _request(
        _build_app(), "GET", "/missing-page", headers={"accept": "text/html"}
    )
    assert resp.status_code == 404
    assert "text/html" in resp.headers.get("content-type", "")
    assert "Page not found" in resp.text
    assert "Reference ID:" in resp.text


def test_web_http_exception_renders_html_template() -> None:
    resp = _request(
        _build_app(), "GET", "/web-http-403", headers={"accept": "text/html"}
    )
    assert resp.status_code == 403
    assert "text/html" in resp.headers.get("content-type", "")
    assert "Forbidden area" in resp.text


def test_web_500_renders_html_template() -> None:
    resp = _request(_build_app(), "GET", "/web-crash", headers={"accept": "text/html"})
    assert resp.status_code == 500
    assert "text/html" in resp.headers.get("content-type", "")
    assert "Server error" in resp.text


def test_api_http_exception_returns_json() -> None:
    resp = _request(
        _build_app(), "GET", "/api/v1/http-403", headers={"accept": "text/html"}
    )
    assert resp.status_code == 403
    assert "application/json" in resp.headers.get("content-type", "")
    body = resp.json()
    assert body["code"] == "http_403"
    assert body["message"] == "Forbidden api"
    assert "request_id" in body


def test_api_validation_error_returns_json() -> None:
    resp = _request(_build_app(), "GET", "/api/v1/needs-int", params={"value": "abc"})
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "validation_error"
    assert body["message"] == "Validation error"
    assert isinstance(body["details"], list)


def test_validation_error_summary_excludes_raw_input() -> None:
    exc = RequestValidationError(
        [
            {
                "loc": ("body", "secret_field"),
                "msg": "Field required",
                "type": "missing",
                "input": "do-not-log",
            }
        ]
    )

    assert _validation_error_summary(exc) == [
        {"loc": ["body", "secret_field"], "type": "missing", "msg": "Field required"}
    ]


def test_htmx_request_stays_json_not_template() -> None:
    resp = _request(
        _build_app(),
        "GET",
        "/web-http-409",
        headers={"accept": "text/html", "HX-Request": "true"},
    )
    assert resp.status_code == 409
    assert "application/json" in resp.headers.get("content-type", "")
    assert resp.json()["code"] == "http_409"


def test_redirect_error_known_token_converts_to_template() -> None:
    app = _build_app()

    @app.get("/redirect-known")
    async def redirect_known():
        from fastapi.responses import RedirectResponse

        return RedirectResponse("/somewhere?error=not_found", status_code=303)

    resp = _request(
        app,
        "GET",
        "/redirect-known",
        headers={"accept": "text/html"},
        follow_redirects=False,
    )
    assert resp.status_code == 404
    assert "text/html" in resp.headers.get("content-type", "")
    assert "could not be found" in resp.text


def test_redirect_error_unknown_token_keeps_redirect() -> None:
    app = _build_app()

    @app.get("/redirect-unknown")
    async def redirect_unknown():
        from fastapi.responses import RedirectResponse

        return RedirectResponse("/somewhere?error=post_failed", status_code=303)

    resp = _request(
        app,
        "GET",
        "/redirect-unknown",
        headers={"accept": "text/html"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers.get("location") == "/somewhere?error=post_failed"


def test_html_http_303_preserves_location_redirect() -> None:
    app = _build_app()

    @app.get("/reseller/dashboard")
    async def reseller_dashboard():
        raise HTTPException(
            status_code=303,
            detail="See Other",
            headers={"Location": "/reseller/auth/login"},
        )

    resp = _request(
        app,
        "GET",
        "/reseller/dashboard",
        headers={"accept": "text/html"},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers.get("location") == "/reseller/auth/login"


def test_web_404_reseller_path_renders_without_500() -> None:
    # Regression: routing /reseller errors to reseller/errors/404.html crashed
    # with 500 because that template extended layouts/reseller.html (which uses
    # current_user, absent from the error context). It must render standalone.
    resp = _request(
        _build_app(), "GET", "/reseller/missing-page", headers={"accept": "text/html"}
    )
    assert resp.status_code == 404
    assert "text/html" in resp.headers.get("content-type", "")
    assert "/reseller/dashboard" in resp.text


def test_web_404_portal_path_renders_without_500() -> None:
    resp = _request(
        _build_app(), "GET", "/portal/missing-page", headers={"accept": "text/html"}
    )
    assert resp.status_code == 404
    assert "text/html" in resp.headers.get("content-type", "")


def test_html_3xx_http_exception_preserves_location_redirect():
    """An auth guard that raises 303 + Location must reach the browser as a
    redirect. The shared handler previously dropped ``exc.headers`` and
    answered with a JSON body carrying no destination, so reseller-portal
    auth redirects silently broke behind the registered handlers.
    """
    from fastapi import status
    from fastapi.testclient import TestClient

    app = FastAPI()
    register_error_handlers(app)

    @app.get("/reseller/dashboard")
    def _guarded():
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/reseller/auth/login"},
        )

    client = TestClient(app)
    response = client.get(
        "/reseller/dashboard",
        headers={"accept": "text/html"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/reseller/auth/login"
