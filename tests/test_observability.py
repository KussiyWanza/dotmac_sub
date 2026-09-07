from __future__ import annotations

import asyncio

import httpx
from fastapi import FastAPI
from starlette.requests import Request

from app import observability as observability_module


def _build_request(path: str) -> Request:
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(scope, receive)


def test_should_skip_observability_for_health_and_metrics():
    assert observability_module._should_skip_observability("/health") is True
    assert observability_module._should_skip_observability("/metrics") is True
    assert observability_module._should_skip_observability("/admin/billing") is False


def test_request_path_prefers_route_template():
    request = _build_request("/admin/network/olts/123")
    request.scope["route"] = type(
        "Route", (), {"path": "/admin/network/olts/{olt_id}"}
    )()

    assert observability_module._request_path(request) == "/admin/network/olts/{olt_id}"


class _MetricRecorder:
    def __init__(self) -> None:
        self.labels_seen: list[tuple[str, ...]] = []

    def labels(self, *labels: str):
        self.labels_seen.append(labels)
        return self

    def inc(self) -> None:
        return None

    def observe(self, _value: float) -> None:
        return None


def test_metric_path_collapses_unmatched_requests():
    request = _build_request("/random/customer-specific-value")

    assert observability_module._metric_path(request) == "<unmatched>"


def test_middleware_records_route_template_after_routing(monkeypatch):
    count = _MetricRecorder()
    latency = _MetricRecorder()
    monkeypatch.setattr(observability_module, "REQUEST_COUNT", count)
    monkeypatch.setattr(observability_module, "REQUEST_LATENCY", latency)

    app = FastAPI()

    @app.get("/items/{item_id}")
    async def item(item_id: str):
        return {"item_id": item_id}

    instrumented = observability_module.ObservabilityMiddleware(app)

    async def request_item() -> httpx.Response:
        transport = httpx.ASGITransport(app=instrumented)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.get("/items/2d74d343-a023-4f7f-b9db-a3ce4df9e626")

    response = asyncio.run(request_item())

    assert response.status_code == 200
    assert count.labels_seen == [("GET", "/items/{item_id}", "200")]
    assert latency.labels_seen == [("GET", "/items/{item_id}", "200")]
