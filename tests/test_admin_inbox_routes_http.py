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

from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db import get_db
from app.services import team_inbox_projection
from app.web.admin.inbox import router


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
