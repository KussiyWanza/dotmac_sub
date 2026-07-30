from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.web.customer import routes as customer_routes

REPO_ROOT = Path(__file__).resolve().parents[1]
ROUTES_PATH = REPO_ROOT / "app/web/customer/routes.py"
TEMPLATE_PATH = REPO_ROOT / "templates/customer/support/new.html"


def _function_source(function_name: str) -> str:
    source = ROUTES_PATH.read_text()
    module = ast.parse(source)
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            segment = ast.get_source_segment(source, node)
            if segment is not None:
                return segment
    raise AssertionError(f"Function {function_name} not found in routes.py")


def test_support_detail_redirect_preserves_ticket_next_url() -> None:
    source = _function_source("customer_support_detail")

    assert "quote_plus(ticket_id)" in source
    assert '"/portal/auth/login?next=/portal/support/' in source


def test_support_create_failure_preserves_form_and_uses_error_template() -> None:
    source = _function_source("customer_support_create")

    assert '"customer/support/new.html"' in source
    assert "status_code=400" in source
    assert '"form_values"' in source
    assert '"/portal/support/{ticket_id}"' in source
    assert '"region": region' in source


def test_support_create_requires_and_validates_canonical_region() -> None:
    source = _function_source("customer_support_create")
    template = TEMPLATE_PATH.read_text()

    assert 'region: str = Form("")' in source
    assert "canonical_region_option(db, region)" in source
    assert '"Select a valid Region."' in source
    assert 'name="region" required' in template
    assert "form_values.region == region" in template


@pytest.mark.parametrize("submitted", ("", "forged"))
def test_support_create_maps_invalid_region_to_safe_preserved_form_error(
    monkeypatch, submitted
) -> None:
    monkeypatch.setattr(
        customer_routes,
        "get_current_customer_from_request",
        lambda request, db: {"id": "customer-1"},
    )
    monkeypatch.setattr(
        customer_routes.support_ticket_settings,
        "canonical_region_option",
        lambda db, region: None,
    )
    monkeypatch.setattr(
        customer_routes.crm_portal,
        "ticket_create_context",
        lambda request, db, customer: {
            "request": request,
            "customer": customer,
            "region_options": ["north"],
        },
    )
    monkeypatch.setattr(
        customer_routes.templates,
        "TemplateResponse",
        lambda template, context, status_code: SimpleNamespace(
            template=template,
            context=context,
            status_code=status_code,
        ),
    )

    response = customer_routes.customer_support_create(
        request=object(),
        title="Internet down",
        description="Please investigate",
        priority="normal",
        region=submitted,
        attachments=[],
        db=object(),
    )

    assert response.status_code == 400
    assert response.context["crm_error_message"] == "Select a valid Region."
    assert response.context["form_values"]["region"] == submitted


def test_support_comment_failure_renders_detail_template_and_success_redirect() -> None:
    source = _function_source("customer_support_add_comment")

    assert '"customer/support/detail.html"' in source
    assert "status_code=400" in source
    assert '"/portal/support/{ticket_id}"' in source


def test_support_list_route_delegates_to_crm_portal() -> None:
    support_source = _function_source("customer_support")

    assert "crm_portal.tickets_list_context" in support_source
    assert '"customer/support/index.html"' in support_source


def test_customer_portal_chat_session_uses_portal_session() -> None:
    source = _function_source("customer_portal_chat_session")

    assert "get_current_customer_from_request" in source
    assert "require_user_auth" not in source
    assert "broker_customer_session" in source


def test_customer_portal_chat_session_brokers_with_portal_subscriber(
    monkeypatch,
) -> None:
    request = object()
    db = object()
    calls = {}

    monkeypatch.setattr(
        customer_routes,
        "get_current_customer_from_request",
        lambda actual_request, actual_db: {
            "subscriber_id": "sub-123",
            "account_id": "acct-ignored",
        },
    )

    def _broker(actual_db, subscriber_id):
        calls["db"] = actual_db
        calls["subscriber_id"] = subscriber_id
        return {"session_id": "sess-1", "visitor_token": "tok-1"}

    monkeypatch.setattr(
        customer_routes.chat_session_service,
        "broker_customer_session",
        _broker,
    )

    result = customer_routes.customer_portal_chat_session(request, db)

    assert result == {"session_id": "sess-1", "visitor_token": "tok-1"}
    assert calls == {"db": db, "subscriber_id": "sub-123"}
