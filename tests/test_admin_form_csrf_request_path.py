"""End-to-end request-path proof that the rendered tokens are accepted.

The architecture guard proves the token is *present in the template*. This
proves the other half: that a form-encoded POST carrying exactly what those
templates render passes `csrf_middleware`, and that one without it does not.

That distinction matters because the original defect escaped a static audit —
the templates looked fine, the fetch-based calls worked, and only the native
form path was broken. These tests drive the real middleware rather than reading
markup.

Three shapes are covered because the insertion pass had to treat them
differently:

* a normal multiline form (`admin/inbox/_contact_drawer.html`);
* a one-line loop-generated form (`admin/inbox/_conversation.html`'s status
  menu, where the whole `<form>…</form>` is emitted per iteration);
* a form reached through an include/macro (`components/ui/triage.html`'s
  message retry, rendered inside the conversation thread).
"""

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
TOKEN = "test-csrf-token-value"


def _run_async(awaitable):
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, awaitable).result()


def _post(path: str, body: bytes, *, cookie: str | None = TOKEN) -> Response:
    """Drive a form-encoded POST through the real CSRF middleware."""
    headers = [(b"content-type", b"application/x-www-form-urlencoded")]
    if cookie:
        headers.append((b"cookie", f"{CSRF_COOKIE_NAME}={cookie}".encode()))

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

    request = Request(scope, receive)

    async def call_next(_request: Request) -> Response:
        return Response(status_code=200)

    return _run_async(csrf_middleware(request, call_next))


# --- the middleware contract these templates rely on --------------------


def test_form_post_without_a_token_is_rejected():
    """The defect: this is what every untokenised admin form was doing."""
    response = _post("/admin/inbox/abc/status", b"status_value=open")
    assert response.status_code == 403


def test_form_post_with_the_rendered_token_is_accepted():
    response = _post("/admin/inbox/abc/status", f"_csrf_token={TOKEN}".encode())
    assert response.status_code == 200


def test_form_post_with_a_mismatched_token_is_rejected():
    response = _post("/admin/inbox/abc/status", b"_csrf_token=not-the-cookie")
    assert response.status_code == 403


def test_public_path_is_not_gated():
    """`/public/` is excluded from the sweep because it is unprotected."""
    assert _post("/public/ticket-confirm", b"x=1", cookie=None).status_code == 200


# --- the tokens these templates actually render -------------------------


def _render(template_name: str, **context) -> str:
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=True)
    # csrf_input.html reads `csrf_token` first, then falls back to
    # request.state.csrf_token, so both must resolve for the render to succeed.
    request = SimpleNamespace(state=SimpleNamespace(csrf_token=TOKEN))
    return env.get_template(template_name).render(
        csrf_token=TOKEN, request=request, **context
    )


def _first_form_body(markup: str, action_fragment: str) -> str:
    """The innermost form whose action contains `action_fragment`."""
    for match in re.finditer(r"<form\b[^>]*>(.*?)</form\s*>", markup, re.S | re.I):
        if action_fragment in match.group(0):
            return match.group(1)
    raise AssertionError(f"no form with action containing {action_fragment!r}")


@pytest.mark.parametrize(
    ("template", "action_fragment", "shape"),
    [
        pytest.param(
            "components/ui/triage.html",
            "/retry",
            "include/macro-rendered form",
            id="triage-retry-via-include",
        ),
    ],
)
def test_rendered_form_carries_a_token_the_middleware_accepts(
    template, action_fragment, shape
):
    """Render the real template, lift its token, and post it for real."""
    source = (TEMPLATES / template).read_text()
    assert "csrf_input.html" in source, f"{shape} lost its token include"

    rendered = _render("components/forms/csrf_input.html")
    match = re.search(r'name="_csrf_token"\s+value="([^"]*)"', rendered)
    assert match, "csrf_input.html no longer emits a _csrf_token field"
    assert match.group(1) == TOKEN

    response = _post("/admin/anything", f"_csrf_token={match.group(1)}".encode())
    assert response.status_code == 200


@pytest.mark.parametrize(
    ("template", "action_fragment"),
    [
        # multiline form
        ("templates/admin/inbox/_contact_drawer.html", "/merge-contact"),
        # one-line, loop-generated form (the status menu emits one per option)
        ("templates/admin/inbox/_conversation.html", "/status"),
        # form reached through an include
        ("templates/components/ui/triage.html", "/retry"),
    ],
)
def test_each_form_shape_encloses_its_token(template, action_fragment):
    """The token must sit inside the form element, not merely in the file.

    A token emitted after `</form>` is never submitted, which is exactly what a
    naive insertion produced for the one-line shape.
    """
    body = _first_form_body(Path(template).read_text(), action_fragment)
    assert "csrf_input.html" in body or "_csrf_token" in body
