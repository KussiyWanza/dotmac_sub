from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from app.api import chat_widget
from app.schemas.chat import FiberChatSessionCreate
from app.services import team_inbox_widget


def _request(origin: str = "https://fiber.dotmac.ng") -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "path": "/widget/fiber/session",
            "raw_path": b"/widget/fiber/session",
            "query_string": b"",
            "headers": [(b"origin", origin.encode())],
            "client": ("203.0.113.20", 12345),
            "server": ("selfcare.dotmac.io", 443),
        }
    )


def _payload() -> dict[str, object]:
    return {
        "form_version": "fiber-chat-v1",
        "client_session_id": str(uuid4()),
        "full_name": "Fiber Visitor",
        "email": "visitor@example.com",
        "phone": "08031234567",
        "message": "Please check coverage at my address.",
        "page_url": "https://fiber.dotmac.ng/coverage/",
        "referrer_url": "https://www.google.com/",
        "started_at": (datetime.now(UTC) - timedelta(seconds=5)).isoformat(),
        "company_website": "",
    }


def test_fiber_widget_session_contract_maps_to_owner_command(db_session, monkeypatch):
    captured = []

    def broker(_db, *, command):
        captured.append(command)
        return team_inbox_widget.FiberWidgetSessionOutcome(
            session_id="session-1",
            visitor_token="opaque-test-token",
            conversation_id=str(uuid4()),
            message_id=str(uuid4()),
            ws_url="/ws/inbox",
            api_base="/widget",
            resolution_status="unmatched",
            replayed=False,
        )

    monkeypatch.setattr(
        chat_widget.team_inbox_widget,
        "broker_fiber_visitor_session_committed",
        broker,
    )
    monkeypatch.setattr(
        chat_widget,
        "allow_operation",
        lambda *_args, **_kwargs: SimpleNamespace(
            allowed=True,
            retry_after_seconds=None,
        ),
    )

    response = chat_widget.fiber_widget_session_create(
        FiberChatSessionCreate.model_validate(_payload()),
        _request(),
        db_session,
    )

    assert response.api_base == "/widget"
    assert response.resolution_status == "unmatched"
    assert captured[0].email == "visitor@example.com"
    assert captured[0].page_url == "https://fiber.dotmac.ng/coverage/"


def test_fiber_widget_session_rejects_wrong_origin(db_session, monkeypatch):
    monkeypatch.setattr(
        chat_widget.team_inbox_widget,
        "broker_fiber_visitor_session_committed",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("owner must not run")
        ),
    )

    with pytest.raises(HTTPException) as exc:
        chat_widget.fiber_widget_session_create(
            FiberChatSessionCreate.model_validate(_payload()),
            _request("https://example.com"),
            db_session,
        )

    assert exc.value.status_code == 403


def test_fiber_widget_honeypot_is_fail_closed_by_schema():
    payload = _payload()
    payload["company_website"] = "spam.example"

    with pytest.raises(ValidationError):
        FiberChatSessionCreate.model_validate(payload)
