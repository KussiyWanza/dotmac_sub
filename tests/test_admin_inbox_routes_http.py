"""HTTP-level tests for the admin inbox routes.

The inbox was covered at the service layer and by template string-matching, but
nothing drove the routes themselves — so the adapter seam went untested. That
is exactly where `ai_handling` and `has_ticket` were declared `str | None` and
then passed through a normalizer that keeps only real booleans, which silently
dropped both filters on every request while the service-level tests stayed
green.

These tests assert the translation, not the query: what the browser sends must
arrive at the read model with the type the read model expects.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from io import BytesIO
from unittest.mock import patch
from urllib.parse import quote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile

from app.db import get_db
from app.services import team_inbox_commands, team_inbox_filters, team_inbox_projection
from app.web.admin.inbox import _read_new_conversation_uploads, router


def _client(db_session) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db_session
    # Route-level permission guards are constructed at import time, so they are
    # overridden by identity rather than by key.
    for route in router.routes:
        for dependency in getattr(route, "dependencies", ()):
            if dependency.dependency is not None:
                app.dependency_overrides[dependency.dependency] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def captured_request(db_session):
    """Drive the queue route and capture the `InboxQueueRequest` it builds."""
    seen: list[team_inbox_projection.InboxQueueRequest] = []
    real = team_inbox_projection.build_queue_projection

    def capture(db, request):
        seen.append(request)
        return real(db, request)

    def run(query: str = "") -> team_inbox_projection.InboxQueueRequest:
        client = _client(db_session)
        with (
            patch(
                "app.services.team_inbox_projection.build_queue_projection",
                side_effect=capture,
            ),
            patch("app.web.admin.get_current_user", return_value=None),
            patch("app.web.admin.get_sidebar_stats", return_value={}),
            patch("app.services.web_admin.get_actor_id", return_value=None),
        ):
            client.get(f"/inbox{query}", follow_redirects=False)
        assert seen, "the queue route did not reach the projection owner"
        return seen[-1]

    return run


def test_ai_handling_checkbox_reaches_the_read_model_as_a_boolean(captured_request):
    assert captured_request("?ai_handling=true").ai_handling is True


def test_ai_handling_false_is_distinct_from_absent(captured_request):
    assert captured_request("?ai_handling=false").ai_handling is False
    assert captured_request("").ai_handling is None


def test_has_ticket_checkbox_reaches_the_read_model_as_a_boolean(captured_request):
    assert captured_request("?has_ticket=true").has_ticket is True


def test_has_ticket_false_is_distinct_from_absent(captured_request):
    assert captured_request("?has_ticket=false").has_ticket is False
    assert captured_request("").has_ticket is None


def test_the_tristate_filters_behave_the_same_way(captured_request):
    """`muted` and `snoozed` were already correct; the new pair now matches."""
    parsed = captured_request(
        "?muted=true&snoozed=false&ai_handling=true&has_ticket=false"
    )
    assert (parsed.muted, parsed.snoozed) == (True, False)
    assert (parsed.ai_handling, parsed.has_ticket) == (True, False)


def test_activity_window_is_parsed_from_the_browser_datetime_local(captured_request):
    parsed = captured_request(
        "?activity_from=2026-07-01T09:00&activity_to=2026-07-02T17:30"
    )
    assert parsed.activity_from == datetime(2026, 7, 1, 9, 0, tzinfo=UTC)
    assert parsed.activity_to == datetime(2026, 7, 2, 17, 30, tzinfo=UTC)


def test_an_unparsable_activity_bound_is_dropped_not_guessed(captured_request):
    assert captured_request("?activity_from=not-a-date").activity_from is None


def test_multi_team_scope_is_split_on_commas(captured_request):
    parsed = captured_request("?service_team_ids=a,%20b%20,,c")
    assert parsed.service_team_ids == ("a", "b", "c")


def test_advanced_team_filter_reaches_projection_as_typed_raw_input(captured_request):
    raw_json = '["not parsed by the adapter"]'

    parsed = captured_request(f"?filters={quote(raw_json)}")

    assert parsed.advanced_filters == team_inbox_filters.InboxAdvancedFilterPayload(
        raw_json=raw_json
    )


def test_invalid_advanced_team_filter_returns_controlled_422(db_session):
    client = _client(db_session)
    with (
        patch("app.web.admin.get_current_user", return_value=None),
        patch("app.web.admin.get_sidebar_stats", return_value={}),
        patch("app.services.web_admin.get_actor_id", return_value=None),
    ):
        response = client.get("/inbox", params={"filters": "not-json"})

    assert response.status_code == 422
    assert response.json()["detail"] == "Invalid JSON in filters payload"


def test_every_route_declares_a_permission_guard():
    """No inbox route may be reachable without an explicit permission."""
    unguarded = [
        route.path
        for route in router.routes
        if not [
            dependency
            for dependency in getattr(route, "dependencies", ())
            if dependency.dependency is not None
        ]
    ]
    assert unguarded == []


def _post_new_email_conversation(
    db_session,
    *,
    files: list[tuple[str, tuple[str, bytes, str]]] | None = None,
):
    captured: list[tuple[tuple[str, str | None, bytes], ...]] = []

    def start_conversation(db, **kwargs):
        assert db is db_session
        captured.append(tuple(kwargs["uploads"]))
        return team_inbox_commands.StartConversationOutcome(
            conversation_id="37bc5b83-dad9-4ddd-9c45-85dbb72ca35b",
            kind="queued",
            sender="support@example.test",
            contact_status="unmatched",
        )

    client = _client(db_session)
    with (
        patch(
            "app.services.team_inbox_commands.start_conversation",
            side_effect=start_conversation,
        ),
        patch("app.services.web_admin.get_actor_id", return_value=None),
    ):
        response = client.post(
            "/inbox/conversations",
            data={
                "channel_type": "email",
                "contact_address": "customer@example.test",
                "body_text": "Hello from the Inbox.",
            },
            files=files,
            follow_redirects=False,
        )
    return response, captured


def test_new_conversation_ignores_an_empty_browser_file_placeholder():
    placeholder = UploadFile(
        file=BytesIO(b""),
        filename="",
        headers={"content-type": "application/octet-stream"},
    )

    # The full parallel suite can leave an event loop active on this worker.
    # Match the repository convention for sync tests that drive async helpers:
    # use a dedicated thread instead of nesting asyncio.run().
    with ThreadPoolExecutor(max_workers=1) as executor:
        uploads = executor.submit(
            asyncio.run, _read_new_conversation_uploads([placeholder])
        ).result()

    assert uploads == []


def test_new_conversation_passes_a_selected_attachment_to_the_owner(db_session):
    response, captured = _post_new_email_conversation(
        db_session,
        files=[("files", ("evidence.txt", b"proof", "text/plain"))],
    )

    assert response.status_code == 303
    assert captured == [(("evidence.txt", "text/plain", b"proof"),)]


def test_new_conversation_keeps_a_named_empty_file_for_domain_validation(db_session):
    response, captured = _post_new_email_conversation(
        db_session,
        files=[("files", ("empty.txt", b"", "text/plain"))],
    )

    assert response.status_code == 303
    assert captured == [(("empty.txt", "text/plain", b""),)]
