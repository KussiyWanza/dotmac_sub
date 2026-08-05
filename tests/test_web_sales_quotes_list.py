"""Admin quotes list is routed through list_query (Carbon/WCAG list standard)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.db import get_db
from app.models.party import Party
from app.models.sales import Lead, Quote
from app.services import sales, web_sales
from app.web.admin.sales import router


def _lead(db_session, *, title: str = "Quote list Lead") -> Lead:
    party = Party(display_name=title, party_type="person", status="active")
    db_session.add(party)
    db_session.flush()
    lead = Lead(
        party_id=party.id,
        party_bound_at=datetime.now(UTC),
        party_binding_source="pytest",
        party_binding_reason="Quote list unit fixture",
        title=title,
    )
    db_session.add(lead)
    db_session.flush()
    return lead


def _client(db_session) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/admin")
    app.dependency_overrides[get_db] = lambda: db_session
    for route in router.routes:
        for dependency in getattr(route, "dependencies", ()):
            if dependency.dependency is not None:
                app.dependency_overrides[dependency.dependency] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


def test_quote_list_definition_declares_its_capabilities():
    definition = web_sales.QUOTE_LIST_DEFINITION
    assert set(definition.sortable_keys) == {"created_at", "updated_at"}
    assert set(definition.filterable_keys) == {"status", "lead_id"}
    assert definition.default_sort == "created_at"


def test_build_quotes_list_context_exposes_list_query_and_page_meta(db_session):
    ctx = web_sales.build_quotes_list_context(
        db_session, status=None, lead_id=None, search=None, page=1, per_page=25
    )
    assert "list_query" in ctx
    assert "page_meta" in ctx
    assert ctx["page"] == ctx["page_meta"].page
    assert ctx["total"] == ctx["page_meta"].total_items


def test_build_quotes_list_context_normalizes_stale_params(db_session):
    ctx = web_sales.build_quotes_list_context(
        db_session,
        status="not-a-status",
        lead_id="not-a-uuid",
        search=None,
        sort_by="status",  # filterable, not sortable
        sort_dir="sideways",
        page=1,
        per_page=999,
    )
    query = ctx["list_query"]
    assert query.sort_by == "created_at"
    assert query.sort_dir == "desc"
    assert query.per_page == 25
    assert query.filter_value("status") is None
    assert query.filter_value("lead_id") is None
    assert ctx["canonicalization_needed"] is True


def test_quote_list_has_a_stable_id_tie_breaker_across_pages(db_session, subscriber):
    created = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    db_session.add_all(
        [
            Quote(id=UUID(int=value), subscriber_id=subscriber.id, created_at=created)
            for value in (4, 1, 3, 2)
        ]
    )
    db_session.commit()

    first = sales.quotes.list(db_session, None, None, None, "created_at", "desc", 2, 0)
    second = sales.quotes.list(db_session, None, None, None, "created_at", "desc", 2, 2)

    assert [row.id.int for row in first + second] == [1, 2, 3, 4]


def test_quote_id_search_count_matches_returned_rows(db_session, subscriber):
    quote = Quote(subscriber_id=subscriber.id)
    db_session.add(quote)
    db_session.commit()
    search = str(quote.id)

    ctx = web_sales.build_quotes_list_context(
        db_session,
        status=None,
        lead_id=None,
        search=search,
        page=1,
        per_page=25,
    )

    assert ctx["total"] == 1
    assert [row.id for row in ctx["quotes"]] == [quote.id]


def test_status_lead_and_whitespace_only_filters_do_not_enter_search_branch(
    db_session,
):
    lead = _lead(db_session)
    quote = Quote(lead_id=lead.id, status="sent")
    db_session.add(quote)
    db_session.flush()

    with patch(
        "app.services.sales.service._quote_search_predicate",
        side_effect=AssertionError("search predicate must not run"),
    ):
        by_status = sales.quotes.query(
            db_session,
            sales.QuoteListQueryInput(status="sent"),
        )
        by_lead = sales.quotes.query(
            db_session,
            sales.QuoteListQueryInput(lead_id=str(lead.id)),
        )
        whitespace = sales.quotes.query(
            db_session,
            sales.QuoteListQueryInput(search_term="  \t \n  "),
        )

    assert by_status.items == by_lead.items == (quote,)
    assert whitespace.query.search_term is None


def test_quote_list_http_preserves_visible_filter_sort_and_pagination_state(
    db_session,
):
    lead = _lead(db_session, title="HTTP Quote State Lead")
    quote = Quote(lead_id=lead.id, status="sent")
    db_session.add(quote)
    db_session.commit()
    client = _client(db_session)

    with (
        patch("app.web.admin.get_current_user", return_value=None),
        patch("app.web.admin.get_sidebar_stats", return_value={}),
    ):
        response = client.get(
            "/admin/sales/quotes",
            params={
                "search": "HTTP Quote State",
                "status": "sent",
                "lead_id": str(lead.id),
                "sort": "updated_at",
                "dir": "asc",
                "page": 1,
                "per_page": 10,
            },
            follow_redirects=True,
        )

    assert response.status_code == 200
    assert str(quote.id) in response.text
    assert 'type="text" name="search" value="HTTP Quote State"' in response.text
    filter_form = response.text.split(
        '<form method="get" action="/admin/sales/quotes"', 1
    )[1].split("</form>", 1)[0]
    assert filter_form.count('name="search"') == 1
    assert f'value="{lead.id}" selected' in response.text
    assert 'value="sent" selected' in response.text
    assert "search=HTTP+Quote+State" in response.text
    assert "status=sent" in response.text
    assert f"lead_id={lead.id}" in response.text
    assert 'href="/admin/sales/quotes"' in response.text


def test_invalid_and_stale_quote_filters_redirect_to_canonical_state(db_session):
    client = _client(db_session)

    with (
        patch("app.web.admin.get_current_user", return_value=None),
        patch("app.web.admin.get_sidebar_stats", return_value={}),
    ):
        response = client.get(
            "/admin/sales/quotes",
            params={"status": "stale", "lead_id": str(UUID(int=999))},
            follow_redirects=True,
        )

    assert response.status_code == 200
    assert response.url.path == "/admin/sales/quotes"
    assert "status" not in response.url.params
    assert "lead_id" not in response.url.params


def test_quote_database_failure_renders_truthful_retry_state_without_writes(
    db_session,
    caplog,
):
    lead = _lead(db_session, title="Failure State Lead")
    quote = Quote(lead_id=lead.id, status="draft")
    db_session.add(quote)
    db_session.commit()
    before = db_session.query(Quote).count()
    client = _client(db_session)

    with (
        patch("app.web.admin.get_current_user", return_value=None),
        patch("app.web.admin.get_sidebar_stats", return_value={}),
        patch(
            "app.web.admin.sales.web_sales_service.build_quotes_list_context",
            side_effect=OperationalError("SELECT quotes", {}, Exception("boom")),
        ),
    ):
        response = client.get(
            "/admin/sales/quotes?search=private-customer-term&status=draft"
        )

    assert response.status_code == 200
    assert "Quotes could not be loaded. No CRM data was changed." in response.text
    assert "Quotes are temporarily unavailable" in response.text
    assert "No quotes found" not in response.text
    assert "Retry" in response.text
    assert db_session.query(Quote).count() == before
    route_logs = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name == "app.web.admin.sales"
    )
    assert "sales_quotes_list_load_failed" in route_logs
    assert "private-customer-term" not in route_logs
