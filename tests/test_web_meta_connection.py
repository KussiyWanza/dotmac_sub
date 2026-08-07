from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from starlette.requests import Request

from app.services import web_integrations_meta_social
from app.web.admin import meta_connection as meta_connection_web


def _request() -> Request:
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/admin/crm/meta",
            "headers": [],
            "query_string": b"",
            "server": ("testserver", 80),
            "scheme": "http",
        }
    )
    request.state.csrf_token = "test-csrf"
    return request


def _page() -> web_integrations_meta_social.MetaSocialConfigPage:
    return web_integrations_meta_social.MetaSocialConfigPage(
        installation_id="installation-id",
        installation_state="disabled",
        connector_version="1.0.0",
        auth_mode="individual",
        auth_mode_options=(
            {"id": "oauth", "label": "Meta OAuth"},
            {"id": "individual", "label": "Individual tokens"},
        ),
        app_id="123456789",
        facebook_page_id="75592117926",
        instagram_account_id="17841403813819361",
        graph_version="v21.0",
        webhook_url="https://sub.example/api/v1/webhooks/meta",
        meta_oauth_token_bound=False,
        facebook_token_bound=True,
        instagram_token_bound=True,
        signing_secret_bound=True,
        verify_token_bound=True,
        meta_oauth_token_ref_masked="",
        facebook_token_ref_masked="bao://secr…oken",
        instagram_token_ref_masked="bao://secr…oken",
        signing_secret_ref_masked="bao://secr…cret",
        verify_token_ref_masked="bao://secr…oken",
    )


def test_meta_connection_route_projects_typed_safe_configuration(monkeypatch) -> None:
    from app.web import admin

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        web_integrations_meta_social,
        "build_config_page",
        lambda _db: _page(),
    )
    monkeypatch.setattr(admin, "get_current_user", lambda _request: SimpleNamespace())
    monkeypatch.setattr(admin, "get_sidebar_stats", lambda _db: {})
    monkeypatch.setattr(meta_connection_web, "can", lambda *_args: True)

    def render(template_name, context):
        captured["template_name"] = template_name
        captured["context"] = context
        return context

    monkeypatch.setattr(meta_connection_web.templates, "TemplateResponse", render)

    context = meta_connection_web.meta_connection(_request(), db=object())

    assert captured["template_name"] == "admin/inbox/meta_connection.html"
    assert context["facebook_token_bound"] is True
    assert context["instagram_token_bound"] is True
    assert "access_token" not in context
    assert "instagram_login_access_token" not in context


def test_meta_connection_template_never_prefills_secret_references() -> None:
    template = Path("templates/admin/inbox/meta_connection.html").read_text(
        encoding="utf-8"
    )

    for field in (
        "meta_oauth_access_token_ref",
        "facebook_page_access_token_ref",
        "instagram_login_access_token_ref",
        "webhook_signing_secret_ref",
        "webhook_verify_token_ref",
    ):
        assert f'name="{field}" value=""' in template
    assert 'include "components/forms/csrf_input.html"' in template
    assert "Raw tokens are rejected" in template
