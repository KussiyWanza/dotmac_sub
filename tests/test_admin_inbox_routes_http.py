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
import json
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from io import BytesIO
from unittest.mock import patch
from urllib.parse import parse_qs, quote, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile

from app.db import get_db
from app.models.team_inbox import InboxConversation
from app.services import (
    team_inbox_commands,
    team_inbox_filters,
    team_inbox_projection,
    team_inbox_read,
    team_inbox_read_state,
)
from app.web.admin.inbox import _detail_redirect, _read_new_conversation_uploads, router


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


def test_queue_requests_exact_total_for_numbered_pagination(captured_request):
    assert captured_request("?page=7").include_total_count is True


def test_mark_read_returns_typed_browser_result_without_redirect(db_session):
    conversation_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    command_id = uuid.uuid4()
    outcome = team_inbox_read_state.ConversationReadOutcome(
        conversation_id=conversation_id,
        person_id=actor_id,
        through_message_id=None,
        last_read_at=datetime.now(UTC),
        changed=True,
        command_id=command_id,
    )
    client = _client(db_session)

    with (
        patch("app.services.web_admin.get_actor_id", return_value=str(actor_id)),
        patch("app.web.admin.inbox._prepare_mutation"),
        patch(
            "app.web.admin.inbox.team_inbox_read_state.mark_conversation_read",
            return_value=outcome,
        ),
    ):
        response = client.post(f"/inbox/{conversation_id}/read")

    assert response.status_code == 200
    assert response.json() == {
        "conversation_id": str(conversation_id),
        "status": "success",
        "changed": True,
        "message": "Conversation marked read.",
    }


def test_reply_fallback_preserves_page_filters_and_uses_separate_notice_status():
    conversation_id = uuid.uuid4()

    response = _detail_redirect(
        conversation_id,
        status="success",
        message="Reply queued.",
        next_url=(
            "/admin/inbox?status=open&channel_type=email&sort=last_message_at"
            "&dir=desc&page=7&per_page=25&conversation_id=stale"
        ),
    )

    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query == {
        "status": ["open"],
        "channel_type": ["email"],
        "sort": ["last_message_at"],
        "dir": ["desc"],
        "page": ["7"],
        "per_page": ["25"],
        "c": [str(conversation_id)],
        "notice_status": ["success"],
        "message": ["Reply queued."],
    }


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


def test_contact_drawer_renders_previous_conversation_tab_and_route(
    db_session, subscriber
):
    previous = InboxConversation(
        subscriber_id=subscriber.id,
        channel_type="email",
        status="resolved",
        subject="Previous installation chat",
        contact_address=subscriber.email,
        last_message_at=datetime(2026, 8, 15, 9, 30, tzinfo=UTC),
        is_active=True,
    )
    current = InboxConversation(
        subscriber_id=subscriber.id,
        channel_type="email",
        status="open",
        subject="Current relocation chat",
        contact_address=subscriber.email,
        last_message_at=datetime(2026, 8, 16, 10, 0, tzinfo=UTC),
        is_active=True,
    )
    db_session.add_all((previous, current))
    db_session.commit()
    client = _client(db_session)

    with (
        patch("app.web.admin.inbox.can", return_value=True),
        patch("app.web.admin.get_current_user", return_value=None),
        patch("app.web.admin.get_sidebar_stats", return_value={}),
        patch("app.services.web_admin.get_actor_id", return_value=None),
    ):
        response = client.get(f"/inbox/{current.id}/contact")

    assert response.status_code == 200
    assert 'role="tablist"' in response.text
    assert "Conversations" in response.text
    assert "Previous installation chat" in response.text
    assert f'href="/admin/inbox?c={previous.id}"' in response.text
    # The machine-readable value is the stable cross-database contract. Keep
    # the assertion scoped to this exact history row and compare instants:
    # SQLite drops UTC metadata while the renderer emits the configured local
    # offset, so deleting tzinfo would incorrectly compare different wall
    # clocks (09:30 UTC and 10:30+01:00).
    history_time = re.search(
        rf'href="/admin/inbox\?c={previous.id}".*?'
        r'<time datetime="([^"]+)">([^<]+)</time>',
        response.text,
        re.DOTALL,
    )
    assert history_time is not None
    rendered_at = datetime.fromisoformat(history_time.group(1))
    stored_at = previous.last_message_at
    assert stored_at is not None
    if stored_at.tzinfo is None:
        stored_at = stored_at.replace(tzinfo=UTC)
    assert rendered_at.astimezone(UTC) == stored_at.astimezone(UTC)
    assert history_time.group(2).strip()


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


def test_reply_htmx_request_returns_typed_completion_event_without_redirect():
    conversation_id = uuid.uuid4()
    notification_id = uuid.uuid4()
    outcome = team_inbox_commands.ReplyOutcome(
        conversation_id=str(conversation_id),
        kind="queued",
        sender="support@example.test",
        message_id=str(uuid.uuid4()),
        notification_id=notification_id,
    )
    client = _client(object())

    with (
        patch("app.web.admin.inbox._prepare_mutation"),
        patch("app.services.team_inbox_commands.reply", return_value=outcome),
        patch("app.services.web_admin.get_actor_id", return_value=None),
    ):
        response = client.post(
            f"/inbox/{conversation_id}/reply",
            data={"body_text": "We are checking this now."},
            headers={"HX-Request": "true"},
            follow_redirects=False,
        )

    assert response.status_code == 204
    assert "location" not in response.headers
    event = json.loads(response.headers["HX-Trigger"])["inbox-reply-completed"]
    assert event == {
        "conversation_id": str(conversation_id),
        "status": "success",
        "message": "Reply queued from support@example.test.",
        "message_id": str(outcome.message_id),
    }


def test_message_fragment_route_renders_one_authoritative_message():
    conversation_id = uuid.uuid4()
    message_id = uuid.uuid4()
    message = team_inbox_read.InboxTimelineMessage(
        id=str(message_id),
        channel_type="email",
        direction="internal",
        subject=None,
        body="Targeted fragment body",
        from_address=None,
        to_addresses=[],
        cc_addresses=[],
        sent_at=None,
        received_at=None,
        created_at=datetime.now(UTC),
        metadata=None,
        attachments=[],
        sender=None,
    )
    projection = team_inbox_projection.InboxMessageFragmentProjection(
        conversation_id=conversation_id,
        message_id=message_id,
        message=message,
    )
    client = _client(object())

    with patch(
        "app.web.admin.inbox.team_inbox_projection.get_message_fragment_projection",
        return_value=projection,
    ):
        response = client.get(f"/inbox/{conversation_id}/messages/{message_id}")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "private, no-store"
    assert f'data-inbox-message-id="{message_id}"' in response.text
    assert "Targeted fragment body" in response.text


def test_queue_row_route_deletes_a_row_that_no_longer_matches_filters():
    conversation_id = uuid.uuid4()
    seen: list[team_inbox_projection.InboxQueueRequest] = []

    def project(_db, *, conversation_id, request):
        seen.append(request)
        return team_inbox_projection.InboxQueueRowProjection(
            conversation_id=conversation_id,
            row=None,
            list_query=team_inbox_projection.INBOX_LIST_DEFINITION.build_query(
                search=None,
                filters={"needs_response": "true"},
            ),
            agent_options=(),
            selected_id=str(conversation_id),
        )

    client = _client(object())
    with (
        patch(
            "app.web.admin.inbox.team_inbox_projection.get_queue_row_projection",
            side_effect=project,
        ),
        patch("app.services.web_admin.get_actor_id", return_value=None),
    ):
        response = client.get(
            f"/inbox/{conversation_id}/queue-row",
            params={"needs_response": "true", "c": str(conversation_id)},
        )

    assert response.status_code == 200
    assert response.headers["HX-Reswap"] == "delete"
    assert response.headers["Cache-Control"] == "private, no-store"
    assert seen[0].needs_response is True
    assert seen[0].selected_conversation_id == str(conversation_id)


def test_reply_htmx_command_error_stays_in_workspace_with_failure_event():
    conversation_id = uuid.uuid4()
    client = _client(object())

    with (
        patch("app.web.admin.inbox._prepare_mutation"),
        patch(
            "app.services.team_inbox_commands.reply",
            side_effect=team_inbox_commands.InboxCommandError(
                "The channel is temporarily unavailable."
            ),
        ),
        patch("app.services.web_admin.get_actor_id", return_value=None),
    ):
        response = client.post(
            f"/inbox/{conversation_id}/reply",
            data={"body_text": "Please try this reply."},
            headers={"HX-Request": "true"},
            follow_redirects=False,
        )

    assert response.status_code == 204
    assert "location" not in response.headers
    event = json.loads(response.headers["HX-Trigger"])["inbox-reply-completed"]
    assert event == {
        "conversation_id": str(conversation_id),
        "status": "error",
        "message": "The channel is temporarily unavailable.",
    }


def test_reply_htmx_busy_error_is_retryable_without_http_500():
    conversation_id = uuid.uuid4()
    client = _client(object())

    with (
        patch("app.web.admin.inbox._prepare_mutation"),
        patch(
            "app.services.team_inbox_commands.reply",
            side_effect=team_inbox_commands.ConversationBusyError(),
        ),
        patch("app.services.web_admin.get_actor_id", return_value=None),
    ):
        response = client.post(
            f"/inbox/{conversation_id}/reply",
            data={
                "body_text": "Please retry this reply.",
                "idempotency_key": "stable-browser-send-key",
            },
            headers={"HX-Request": "true"},
            follow_redirects=False,
        )

    assert response.status_code == 204
    assert response.headers["Retry-After"] == "1"
    event = json.loads(response.headers["HX-Trigger"])["inbox-reply-completed"]
    assert event == {
        "conversation_id": str(conversation_id),
        "status": "error",
        "message": "Another conversation update is completing. Please retry.",
    }


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
