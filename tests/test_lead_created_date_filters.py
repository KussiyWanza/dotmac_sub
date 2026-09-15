"""Created-date behavior through the same typed owner used by Admin Leads."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.db import get_db
from app.models.sales import Lead
from app.services import sales, web_sales
from app.services.sales import service
from app.web.admin.sales import router


def _client(db_session):
    app = FastAPI()
    app.include_router(router, prefix="/admin")
    app.dependency_overrides[get_db] = lambda: db_session
    for route in router.routes:
        for dependency in getattr(route, "dependencies", ()):
            if dependency.dependency is not None:
                app.dependency_overrides[dependency.dependency] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


def _lead(
    db, subscriber, created, *, title="Dated opportunity", status="new", active=True
):
    lead = Lead(
        subscriber_id=subscriber.id,
        title=title,
        status=status,
        created_at=created,
        updated_at=datetime(2026, 9, 15, tzinfo=UTC),
        is_active=active,
        estimated_value=Decimal("1000"),
        currency="NGN",
    )
    db.add(lead)
    db.flush()
    return lead


@pytest.mark.parametrize("preset,days", [("last_7_days", 7), ("last_30_days", 30)])
@pytest.mark.parametrize(
    "today", [date(2026, 9, 15), date(2026, 1, 2), date(2024, 3, 1)]
)
def test_relative_normalization_uses_calendar_days(preset, days, today):
    result = sales.normalize_lead_date_range(
        sales.LeadListQueryInput(
            date_preset=preset, date_from="2000-01-01", date_to="2000-01-02"
        ),
        today=today,
    )
    assert result.preset.value == preset
    assert result.date_from == today - timedelta(days=days - 1)
    assert result.date_to == today
    assert result.created_from == datetime.combine(
        result.date_from, datetime.min.time(), tzinfo=UTC
    )
    assert result.created_to_exclusive == datetime.combine(
        today + timedelta(days=1), datetime.min.time(), tzinfo=UTC
    )


@pytest.mark.parametrize(
    "preset,start,end",
    [
        (None, "2026-09-01", "2026-09-15"),
        ("unknown", "2026-09-01", "2026-09-15"),
        ("custom", None, "2026-09-15"),
        ("custom", "2026-09-01", None),
        ("custom", "bad", "2026-09-15"),
        ("custom", "2026-02-30", "2026-09-15"),
        ("custom", "2026-09-15", "2026-09-01"),
        ("custom", "2026-09-01", "9999-12-31"),
        ("custom", "20260901", "2026-09-15"),
    ],
)
def test_invalid_date_scopes_are_safe_and_unfiltered(preset, start, end):
    result = sales.normalize_lead_date_range(
        sales.LeadListQueryInput(date_preset=preset, date_from=start, date_to=end)
    )
    assert result == sales.LeadListDateRange()
    assert result.created_from is result.created_to_exclusive is None


def test_custom_day_boundaries_count_summary_and_existing_filters(
    db_session, subscriber
):
    start = datetime(2026, 9, 1, tzinfo=UTC)
    end = datetime(2026, 9, 2, tzinfo=UTC)
    _lead(db_session, subscriber, start - timedelta(microseconds=1))
    first = _lead(db_session, subscriber, start)
    last = _lead(db_session, subscriber, end - timedelta(microseconds=1), status="won")
    _lead(db_session, subscriber, end)
    _lead(db_session, subscriber, start, active=False)
    scope = dict(date_preset="custom", date_from="2026-09-01", date_to="2026-09-01")
    result = sales.leads.query(db_session, sales.LeadListQueryInput(**scope))
    assert {lead.id for lead in result.items} == {first.id, last.id}
    assert result.total_count == result.summary.total_leads == 2
    assert result.summary.open_leads == result.summary.won_leads == 1
    assert result.summary.pipeline_value == Decimal("1000")
    filtered = sales.leads.query(
        db_session,
        sales.LeadListQueryInput(
            **scope, status="new", search_term="Dated opportunity"
        ),
    )
    assert filtered.items == (first,)
    assert filtered.total_count == filtered.summary.total_leads == 1


@pytest.mark.parametrize("preset,days", [("last_7_days", 7), ("last_30_days", 30)])
def test_presets_filter_the_database_at_exact_midnights(
    db_session, subscriber, monkeypatch, preset, days
):
    now = datetime(2026, 9, 15, 12, tzinfo=UTC)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    start = datetime(2026, 9, 15, tzinfo=UTC) - timedelta(days=days - 1)
    next_day = datetime(2026, 9, 16, tzinfo=UTC)
    _lead(db_session, subscriber, start - timedelta(microseconds=1))
    included_start = _lead(db_session, subscriber, start)
    included_end = _lead(db_session, subscriber, next_day - timedelta(microseconds=1))
    _lead(db_session, subscriber, next_day)
    monkeypatch.setattr(service, "datetime", FixedDateTime)
    result = sales.leads.query(db_session, sales.LeadListQueryInput(date_preset=preset))
    assert {lead.id for lead in result.items} == {included_start.id, included_end.id}
    assert result.total_count == result.summary.total_leads == 2


def test_legacy_list_call_remains_all_time(db_session, subscriber):
    old = _lead(db_session, subscriber, datetime(2020, 1, 1, tzinfo=UTC))
    rows = sales.leads.list(
        db_session,
        pipeline_id=None,
        stage_id=None,
        owner_agent_id=None,
        status=None,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=25,
        offset=0,
    )
    assert old in rows
    assert sales.leads.query(db_session, sales.LeadListQueryInput()).items == (old,)


def test_custom_http_state_survives_pagination_and_sort(db_session, subscriber):
    for _ in range(12):
        _lead(db_session, subscriber, datetime(2026, 9, 10, tzinfo=UTC))
    client = _client(db_session)
    with (
        patch("app.web.admin.get_current_user", return_value=None),
        patch("app.web.admin.get_sidebar_stats", return_value={}),
    ):
        response = client.get(
            "/admin/sales/leads",
            params={
                "date_preset": "custom",
                "date_from": "2026-09-01",
                "date_to": "2026-09-15",
                "search": "Dated opportunity",
                "status": "new",
                "sort": "updated_at",
                "dir": "asc",
                "page": 2,
                "per_page": 10,
            },
            follow_redirects=True,
        )
    assert response.status_code == 200
    assert response.url.params["page"] == "2"
    assert response.url.params["date_to"] == "2026-09-15"
    assert 'value="custom" selected' in response.text
    assert 'name="date_from" value="2026-09-01"' in response.text
    assert 'name="date_to" value="2026-09-15"' in response.text
    assert 'name="page" value="1"' in response.text
    assert 'href="/admin/sales/leads"' in response.text
    assert "date_preset=custom" in response.text
    assert "date_from=2026-09-01" in response.text
    assert "Matching Leads" in response.text


def test_relative_urls_do_not_freeze_a_bookmarked_window(db_session):
    ctx = web_sales.build_leads_list_context(
        db_session,
        status=None,
        pipeline_id=None,
        stage_id=None,
        lead_source=None,
        search=None,
        page=1,
        per_page=25,
        date_preset="last_7_days",
        date_from="2000-01-01",
        date_to="2000-01-02",
    )
    query = ctx["list_query"]
    assert query.filter_value("date_preset") == "last_7_days"
    assert query.filter_value("date_from") is None
    assert query.filter_value("date_to") is None
    assert ctx["canonicalization_needed"] is True


def test_invalid_custom_http_request_canonicalizes_without_crashing(db_session):
    with (
        patch("app.web.admin.get_current_user", return_value=None),
        patch("app.web.admin.get_sidebar_stats", return_value={}),
    ):
        response = _client(db_session).get(
            "/admin/sales/leads?date_preset=custom&date_from=2026-09-15&date_to=2026-09-01",
            follow_redirects=True,
        )
    assert response.status_code == 200
    assert "date_preset" not in response.url.params
    assert "date_from" not in response.url.params


def test_database_failure_keeps_date_scope_and_truthful_retry(db_session):
    with (
        patch("app.web.admin.get_current_user", return_value=None),
        patch("app.web.admin.get_sidebar_stats", return_value={}),
        patch(
            "app.web.admin.sales.web_sales_service.build_leads_list_context",
            side_effect=OperationalError("SELECT leads", {}, Exception("unavailable")),
        ),
    ):
        response = _client(db_session).get(
            "/admin/sales/leads?date_preset=custom&date_from=2026-09-01&date_to=2026-09-15"
        )
    assert response.status_code == 200
    expected_state = web_sales.build_leads_failure_context(
        search=None,
        page=1,
        per_page=25,
        date_preset="custom",
        date_from="2026-09-01",
        date_to="2026-09-15",
    )
    assert expected_state["api_error"]
    assert expected_state["api_error"] in response.text
    assert "date_from=2026-09-01" in response.text
    assert "date_to=2026-09-15" in response.text
    ctx = web_sales.build_leads_failure_context(
        search=None, page=1, per_page=25, date_preset="last_30_days"
    )
    assert parse_qs(urlsplit(ctx["retry_url"]).query)["date_preset"] == ["last_30_days"]
    assert ctx["lead_stats"]["total"] is None
