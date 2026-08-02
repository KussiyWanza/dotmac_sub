"""CSRF request-path coverage for native dispatch work-order forms."""

from __future__ import annotations

import asyncio
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from jinja2 import Environment, FileSystemLoader
from starlette.requests import Request
from starlette.responses import Response

from app.csrf import CSRF_COOKIE_NAME
from app.main import csrf_middleware

TEMPLATES = Path("templates")
TOKEN = "dispatch-work-order-csrf-token"


def _run_async(awaitable):
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, awaitable).result()


def _post(path: str, body: bytes) -> Response:
    headers = [
        (b"content-type", b"application/x-www-form-urlencoded"),
        (b"cookie", f"{CSRF_COOKIE_NAME}={TOKEN}".encode()),
    ]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }
    sent = {"body": body}

    async def receive():
        return {"type": "http.request", "body": sent["body"], "more_body": False}

    async def call_next(_request: Request) -> Response:
        return Response(status_code=200)

    return _run_async(csrf_middleware(Request(scope, receive), call_next))


def _rendered_token() -> str:
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=True)
    request = SimpleNamespace(state=SimpleNamespace(csrf_token=TOKEN))
    rendered = env.get_template("components/forms/csrf_input.html").render(
        request=request,
        csrf_token=TOKEN,
    )
    match = re.search(r'name="_csrf_token"\s+value="([^"]*)"', rendered)
    assert match
    return match.group(1)


@pytest.mark.parametrize(
    "path",
    [
        "/admin/dispatch/work-orders",
        "/admin/dispatch/work-orders/sub-csrf-test",
        "/admin/dispatch/work-orders/sub-csrf-test/queue",
    ],
)
def test_dispatch_form_post_without_token_is_rejected(path):
    assert _post(path, b"title=No+token").status_code == 403


@pytest.mark.parametrize(
    "path",
    [
        "/admin/dispatch/work-orders",
        "/admin/dispatch/work-orders/sub-csrf-test",
        "/admin/dispatch/work-orders/sub-csrf-test/queue",
    ],
)
def test_dispatch_form_post_with_rendered_token_is_accepted(path):
    token = _rendered_token()
    assert _post(path, f"_csrf_token={token}".encode()).status_code == 200


def test_create_update_and_queue_forms_enclose_the_csrf_component():
    list_source = (TEMPLATES / "admin/dispatch/work_orders.html").read_text()
    detail_source = (TEMPLATES / "admin/dispatch/work_order_detail.html").read_text()

    for source, action_fragment in (
        (list_source, 'action="/admin/dispatch/work-orders"'),
        (
            detail_source,
            'action="/admin/dispatch/work-orders/{{ work_order.public_id }}"',
        ),
        (
            detail_source,
            'action="/admin/dispatch/work-orders/{{ work_order.public_id }}/queue"',
        ),
    ):
        forms = re.findall(r"<form\b[^>]*>.*?</form\s*>", source, re.S | re.I)
        form = next(form for form in forms if action_fragment in form)
        assert "components/forms/csrf_input.html" in form
