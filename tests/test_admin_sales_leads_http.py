"""HTTP contract coverage for the searchable admin Lead list."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db import get_db
from app.models.party import Party
from app.models.sales import Lead
from app.web.admin.sales import router


def _party_lead(db_session, *, name: str, title: str, status: str) -> Lead:
    party = Party(display_name=name, party_type="person", status="active")
    db_session.add(party)
    db_session.flush()
    lead = Lead(
        party_id=party.id,
        party_bound_at=datetime.now(UTC),
        party_binding_source="pytest",
        party_binding_reason="Admin Lead HTTP regression fixture",
        title=title,
        status=status,
    )
    db_session.add(lead)
    db_session.commit()
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


def test_lead_search_and_status_filter_preserve_url_rows_count_and_safe_state(
    db_session,
):
    matching = _party_lead(
        db_session,
        name="HTTP John Customer",
        title="HTTP matching opportunity",
        status="qualified",
    )
    other = _party_lead(
        db_session,
        name="HTTP Other Customer",
        title="HTTP other opportunity",
        status="new",
    )
    client = _client(db_session)

    with (
        patch("app.web.admin.get_current_user", return_value=None),
        patch("app.web.admin.get_sidebar_stats", return_value={}),
    ):
        searched = client.get(
            "/admin/sales/leads?search=HTTP%20John",
            follow_redirects=True,
        )
        filtered = client.get(
            "/admin/sales/leads?search=HTTP%20John&status=qualified",
            follow_redirects=True,
        )

    assert searched.status_code == 200
    assert searched.url.params["search"] == "HTTP John"
    assert str(matching.id) in searched.text
    assert str(other.id) not in searched.text
    assert "Showing 1 to 1 of 1 leads" in searched.text
    assert "Leads could not be loaded" not in searched.text

    assert filtered.status_code == 200
    assert filtered.url.params["search"] == "HTTP John"
    assert filtered.url.params["status"] == "qualified"
    assert str(matching.id) in filtered.text
    assert str(other.id) not in filtered.text
    assert "Showing 1 to 1 of 1 leads" in filtered.text
