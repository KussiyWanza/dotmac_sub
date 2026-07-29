from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import Mock
from uuid import uuid4

from fastapi import Request

from app.services import crm_portal


def _ticket(
    id="t-1",
    subscriber_id="sub-1",
    number="TCK-1",
    title="Title",
    description="desc",
    status="open",
    priority="normal",
    created="2026-01-01T00:00:00+00:00",
    updated="2026-01-02T00:00:00+00:00",
    due="2026-01-03T00:00:00+00:00",
    resolved=None,
    closed=None,
) -> Mock:
    """A stand-in for a local support Ticket ORM row."""
    t = Mock()
    t.id = id
    t.subscriber_id = subscriber_id
    t.number = number
    t.title = title
    t.description = description
    t.status = status
    t.priority = priority
    t.created_at = datetime.fromisoformat(created)
    t.updated_at = datetime.fromisoformat(updated)
    t.due_at = datetime.fromisoformat(due) if due else None
    t.resolved_at = datetime.fromisoformat(resolved) if resolved else None
    t.closed_at = datetime.fromisoformat(closed) if closed else None
    return t


def _comment(body="body", is_internal=False, author_person_id=None) -> Mock:
    c = Mock()
    c.body = body
    c.is_internal = is_internal
    c.author_person_id = author_person_id
    c.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    return c


def test_cache_get_returns_none_when_redis_unavailable(monkeypatch) -> None:
    monkeypatch.setattr("app.services.crm_portal.get_session_redis", lambda: None)

    assert crm_portal._cache_get("crm:key") is None


def test_cache_get_handles_redis_errors(monkeypatch) -> None:
    redis_client = Mock()
    redis_client.get.side_effect = RuntimeError("redis down")
    monkeypatch.setattr(
        "app.services.crm_portal.get_session_redis", lambda: redis_client
    )

    assert crm_portal._cache_get("crm:key") is None


def test_cache_set_handles_redis_errors(monkeypatch) -> None:
    redis_client = Mock()
    redis_client.setex.side_effect = RuntimeError("redis down")
    monkeypatch.setattr(
        "app.services.crm_portal.get_session_redis", lambda: redis_client
    )

    crm_portal._cache_set("crm:key", "value", 60)

    redis_client.setex.assert_called_once_with("crm:key", 60, "value")


def test_resolve_crm_subscriber_id_returns_cached_value(monkeypatch) -> None:
    monkeypatch.setattr("app.services.crm_portal._cache_get", lambda _key: "crm-sub-1")

    assert crm_portal.resolve_crm_subscriber_id(Mock(), "sub-1") == "crm-sub-1"


def test_resolve_crm_subscriber_id_returns_none_for_cached_miss(monkeypatch) -> None:
    monkeypatch.setattr("app.services.crm_portal._cache_get", lambda _key: "__none__")

    assert crm_portal.resolve_crm_subscriber_id(Mock(), "sub-1") is None


def test_resolve_crm_subscriber_id_caches_missing_subscriber(monkeypatch) -> None:
    db = Mock()
    db.get.return_value = None
    cache_sets: list[tuple[str, str, int]] = []
    subscriber_id = str(uuid4())

    monkeypatch.setattr("app.services.crm_portal._cache_get", lambda _key: None)
    monkeypatch.setattr(
        "app.services.crm_portal._cache_set",
        lambda key, value, ttl: cache_sets.append((key, value, ttl)),
    )

    assert crm_portal.resolve_crm_subscriber_id(db, subscriber_id) is None
    assert cache_sets == [
        (
            f"crm:sub_map:{subscriber_id}",
            "__none__",
            crm_portal._CACHE_SUBSCRIBER_MAP,
        )
    ]


def test_resolve_crm_subscriber_id_caches_crm_mapping(
    monkeypatch, db_session, subscriber
) -> None:
    subscriber.splynx_customer_id = 321
    db_session.commit()
    cache_sets: list[tuple[str, str, int]] = []

    client = Mock()
    client.resolve_subscriber_id.return_value = "crm-sub-321"

    monkeypatch.setattr("app.services.crm_portal._cache_get", lambda _key: None)
    monkeypatch.setattr(
        "app.services.crm_portal._cache_set",
        lambda key, value, ttl: cache_sets.append((key, value, ttl)),
    )
    monkeypatch.setattr("app.services.crm_portal.capability_client", lambda *_: client)

    resolved = crm_portal.resolve_crm_subscriber_id(db_session, str(subscriber.id))

    assert resolved == "crm-sub-321"
    assert cache_sets == [
        (
            f"crm:sub_map:{subscriber.id}",
            "crm-sub-321",
            crm_portal._CACHE_SUBSCRIBER_MAP,
        )
    ]


def test_resolve_crm_subscriber_id_uses_short_ttl_for_lookup_miss(
    monkeypatch, db_session, subscriber
) -> None:
    subscriber.splynx_customer_id = 654
    db_session.commit()
    cache_sets: list[tuple[str, str, int]] = []

    client = Mock()
    client.resolve_subscriber_id.return_value = None

    monkeypatch.setattr("app.services.crm_portal._cache_get", lambda _key: None)
    monkeypatch.setattr(
        "app.services.crm_portal._cache_set",
        lambda key, value, ttl: cache_sets.append((key, value, ttl)),
    )
    monkeypatch.setattr("app.services.crm_portal.capability_client", lambda *_: client)

    resolved = crm_portal.resolve_crm_subscriber_id(db_session, str(subscriber.id))

    assert resolved is None
    assert cache_sets == [(f"crm:sub_map:{subscriber.id}", "__none__", 300)]


def test_resolve_crm_subscriber_ids_deduplicates_and_skips_blanks(monkeypatch) -> None:
    mappings = {"sub-1": "crm-1", "sub-2": "crm-1", "sub-3": "crm-3"}
    monkeypatch.setattr(
        "app.services.crm_portal.resolve_crm_subscriber_id",
        lambda _db, subscriber_id: mappings.get(subscriber_id),
    )

    resolved = crm_portal.resolve_crm_subscriber_ids(
        Mock(),
        ["sub-1", " ", "sub-2", None, "sub-3"],
    )

    assert resolved == ["crm-1", "crm-3"]


def test_reseller_open_tickets_count_returns_none_when_crm_unavailable(
    monkeypatch,
    db_session,
) -> None:
    client = Mock()
    client.list_tickets.side_effect = crm_portal.CRMClientError("down")
    monkeypatch.setattr(
        "app.services.crm_portal.resolve_crm_subscriber_id",
        lambda _db, _account_id: "crm-sub-1",
    )
    monkeypatch.setattr("app.services.crm_portal.capability_client", lambda *_: client)

    count = crm_portal.reseller_open_tickets_count(
        db_session,
        "reseller-1",
        ["account-1"],
    )

    assert count is None


# ── Customer Portal: Tickets (sourced from the local support module) ──────


def test_ticket_portal_keeps_priority_display_maps_only() -> None:
    for priority in ["lower", "low", "medium", "normal", "high", "urgent"]:
        assert priority in crm_portal.TICKET_PRIORITY_DISPLAY
        assert priority in crm_portal.TICKET_PRIORITY_COLORS


def test_ticket_to_dict_includes_native_sla_resolution_timestamps() -> None:
    ticket = _ticket(
        due="2026-01-03T00:00:00+00:00",
        resolved="2026-01-04T00:00:00+00:00",
        closed="2026-01-05T00:00:00+00:00",
    )

    payload = crm_portal._ticket_to_dict(ticket)

    assert payload["due_at"] == "2026-01-03T00:00:00+00:00"
    assert payload["resolved_at"] == "2026-01-04T00:00:00+00:00"
    assert payload["closed_at"] == "2026-01-05T00:00:00+00:00"
    assert payload["status_presentation"] == {
        "value": "open",
        "label": "Open",
        "tone": "info",
        "icon": "info",
    }


def test_tickets_list_context_skips_blank_subscriber_ids(monkeypatch) -> None:
    called = {"list": False}

    def _list(db, subscriber_id, limit=100):
        called["list"] = True
        return []

    monkeypatch.setattr("app.services.support.Tickets.list", _list)

    context = crm_portal.tickets_list_context(
        Mock(spec=Request), Mock(), {"id": "cust-1"}, ["", "   "]
    )

    assert context["tickets"] == []
    assert context["crm_error"] is False
    assert context["priority_display"] == crm_portal.TICKET_PRIORITY_DISPLAY
    assert called["list"] is False


def test_tickets_list_context_merges_multiple_allowed_accounts(monkeypatch) -> None:
    by_sid = {"sub-1": [_ticket(id="t-1")], "sub-2": [_ticket(id="t-2")]}

    monkeypatch.setattr(
        "app.services.support.Tickets.list",
        lambda db, subscriber_id, limit=100: by_sid.get(subscriber_id, []),
    )

    context = crm_portal.tickets_list_context(
        Mock(spec=Request), Mock(), {"id": "cust-1"}, ["sub-1", "sub-2"]
    )

    assert sorted(t["id"] for t in context["tickets"]) == ["t-1", "t-2"]
    assert context["crm_error"] is False
    assert context["tickets"][0]["status_presentation"]["value"] == "open"


def test_tickets_list_context_returns_error_context_on_failure(monkeypatch) -> None:
    def _boom(db, subscriber_id, limit=100):
        raise RuntimeError("db down")

    monkeypatch.setattr("app.services.support.Tickets.list", _boom)

    context = crm_portal.tickets_list_context(
        Mock(spec=Request), Mock(), {"id": "cust-1"}, ["sub-1"]
    )

    assert context["tickets"] == []
    assert context["crm_error"] is True
    assert "status_display" not in context


def test_ticket_detail_context_rejects_ticket_with_wrong_subscriber(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "app.services.support.Tickets.get",
        lambda db, ticket_id: _ticket(id="ticket-1", subscriber_id="other-sub"),
    )
    comments = Mock()
    monkeypatch.setattr("app.services.support.TicketComments.list", comments)

    context = crm_portal.ticket_detail_context(
        Mock(spec=Request), Mock(), {"id": "cust-1"}, ["sub-1"], "ticket-1"
    )

    assert context["ticket"] is None
    assert context["crm_error"] is True
    assert context["crm_error_message"] == "Ticket not found."
    comments.assert_not_called()


def test_ticket_detail_context_returns_not_found_on_lookup_error(monkeypatch) -> None:
    def _boom(db, ticket_id):
        raise ValueError("invalid id")

    monkeypatch.setattr("app.services.support.Tickets.get", _boom)

    context = crm_portal.ticket_detail_context(
        Mock(spec=Request), Mock(), {"id": "cust-1"}, ["sub-1"], "bad-id"
    )

    assert context["ticket"] is None
    assert context["crm_error"] is True
    assert context["crm_error_message"] == "Ticket not found."


def test_ticket_detail_context_filters_internal_comments(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.support.Tickets.get",
        lambda db, ticket_id: _ticket(id="ticket-1", subscriber_id="sub-1"),
    )
    monkeypatch.setattr(
        "app.services.support.TicketComments.list",
        lambda db, ticket_id: [
            _comment(body="Visible", is_internal=False),
            _comment(body="Hidden", is_internal=True),
        ],
    )

    context = crm_portal.ticket_detail_context(
        Mock(spec=Request), Mock(), {"id": "cust-1"}, ["sub-1"], "ticket-1"
    )

    assert context["ticket"]["id"] == "ticket-1"
    assert [c["body"] for c in context["comments"]] == ["Visible"]
    assert context["ticket"]["status_presentation"]["value"] == "open"


def test_ticket_create_context_exposes_priority_choices() -> None:
    context = crm_portal.ticket_create_context(Mock(spec=Request), {"id": "cust-1"})

    assert context["active_page"] == "support"
    assert context["priorities"] == list(crm_portal.TICKET_PRIORITY_DISPLAY.keys())


def test_handle_ticket_create_normalizes_unknown_priority(monkeypatch) -> None:
    sid = str(uuid4())
    captured: dict[str, str] = {}

    def _create(db, payload, actor_id=None, dispatch_event_after_commit=True):
        captured["priority"] = payload.priority
        return _ticket(id="ticket-1", subscriber_id=sid)

    monkeypatch.setattr("app.services.support.Tickets.create", _create)
    monkeypatch.setattr(
        crm_portal.db_session_adapter, "release_read_transaction", lambda db: None
    )

    result = crm_portal.handle_ticket_create(
        Mock(), {}, sid, "Slow internet", "Please investigate.", "not-a-priority"
    )

    assert result["success"] is True
    assert result["ticket"]["id"] == "ticket-1"
    assert captured["priority"] == "normal"


def test_handle_ticket_create_returns_link_error_without_valid_subscriber() -> None:
    result = crm_portal.handle_ticket_create(
        Mock(), {}, "not-a-uuid", "Slow internet", "", "normal"
    )

    assert result == {
        "success": False,
        "error": "Unable to link your account to the support system.",
    }


def test_handle_ticket_create_returns_error_on_failure(monkeypatch) -> None:
    sid = str(uuid4())

    def _boom(db, payload, actor_id=None):
        raise RuntimeError("db down")

    monkeypatch.setattr("app.services.support.Tickets.create", _boom)

    result = crm_portal.handle_ticket_create(
        Mock(), {}, sid, "Slow internet", "", "normal"
    )

    assert result == {
        "success": False,
        "error": "Unable to create ticket. Please try again later.",
    }


def test_handle_ticket_comment_rejects_ticket_with_wrong_subscriber(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "app.services.support.Tickets.get",
        lambda db, ticket_id: _ticket(id="ticket-1", subscriber_id="other-sub"),
    )
    create = Mock()
    monkeypatch.setattr("app.services.support.TicketComments.create", create)

    result = crm_portal.handle_ticket_comment(
        Mock(), {}, ["sub-1"], "ticket-1", "Please update me."
    )

    assert result == {"success": False, "error": "Ticket not found."}
    create.assert_not_called()


def test_handle_ticket_comment_returns_not_found_on_lookup_error(monkeypatch) -> None:
    def _boom(db, ticket_id):
        raise ValueError("invalid id")

    monkeypatch.setattr("app.services.support.Tickets.get", _boom)

    result = crm_portal.handle_ticket_comment(
        Mock(), {}, ["sub-1"], "ticket-1", "Please update me."
    )

    assert result == {"success": False, "error": "Ticket not found."}


def test_handle_ticket_comment_success(monkeypatch) -> None:
    db = Mock()
    monkeypatch.setattr(
        "app.services.support.Tickets.get",
        lambda _db, ticket_id: _ticket(id="ticket-1", subscriber_id="sub-1"),
    )
    captured: dict[str, object] = {}

    def _create(_db, ticket_id, payload, actor_id=None):
        captured["body"] = payload.body
        captured["is_internal"] = payload.is_internal

    monkeypatch.setattr("app.services.support.Tickets.create_comment", _create)
    monkeypatch.setattr(
        crm_portal.db_session_adapter, "release_read_transaction", lambda db: None
    )

    result = crm_portal.handle_ticket_comment(
        db, {}, ["sub-1"], "ticket-1", "Please update me."
    )

    assert result == {"success": True}
    assert captured["body"] == "Please update me."
    assert captured["is_internal"] is False
    db.commit.assert_not_called()


def test_handle_ticket_comment_returns_error_on_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.support.Tickets.get",
        lambda db, ticket_id: _ticket(id="ticket-1", subscriber_id="sub-1"),
    )

    def _boom(db, ticket, payload, actor_id=None):
        raise RuntimeError("db down")

    monkeypatch.setattr("app.services.support.Tickets.create_comment", _boom)
    monkeypatch.setattr(
        crm_portal.db_session_adapter, "release_read_transaction", lambda db: None
    )

    result = crm_portal.handle_ticket_comment(
        Mock(), {}, ["sub-1"], "ticket-1", "Please update me."
    )

    assert result == {
        "success": False,
        "error": "Unable to add comment. Please try again later.",
    }


def test_resolve_crm_subscriber_id_prefers_stored_id(
    monkeypatch, db_session, subscriber
) -> None:
    stored = uuid4()
    subscriber.crm_subscriber_id = stored
    db_session.commit()
    cache_sets: list[tuple[str, str, int]] = []

    client = Mock()
    monkeypatch.setattr("app.services.crm_portal._cache_get", lambda _key: None)
    monkeypatch.setattr(
        "app.services.crm_portal._cache_set",
        lambda key, value, ttl: cache_sets.append((key, value, ttl)),
    )
    monkeypatch.setattr("app.services.crm_portal.capability_client", lambda *_: client)

    resolved = crm_portal.resolve_crm_subscriber_id(db_session, str(subscriber.id))

    assert resolved == str(stored)
    client.resolve_subscriber_id.assert_not_called()


def test_resolve_crm_subscriber_id_persists_fallback_result(
    monkeypatch, db_session, subscriber
) -> None:
    subscriber.splynx_customer_id = 987
    db_session.commit()
    crm_uuid = str(uuid4())

    client = Mock()
    client.resolve_subscriber_id.return_value = crm_uuid

    monkeypatch.setattr("app.services.crm_portal._cache_get", lambda _key: None)
    monkeypatch.setattr(
        "app.services.crm_portal._cache_set", lambda key, value, ttl: None
    )
    monkeypatch.setattr("app.services.crm_portal.capability_client", lambda *_: client)

    resolved = crm_portal.resolve_crm_subscriber_id(db_session, str(subscriber.id))
    db_session.refresh(subscriber)

    assert resolved == crm_uuid
    assert str(subscriber.crm_subscriber_id) == crm_uuid
