"""Admin leads list is routed through list_query (Carbon/WCAG list standard)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from app.models.sales import Lead
from app.services import sales, web_sales


def test_lead_list_definition_declares_its_capabilities():
    definition = web_sales.LEAD_LIST_DEFINITION
    # Sortable keys mirror the leads.list order_by whitelist.
    assert set(definition.sortable_keys) == {"created_at", "updated_at"}
    assert set(definition.filterable_keys) == {
        "status",
        "lead_source",
        "pipeline_id",
        "stage_id",
        "owner_agent_id",
    }
    assert definition.default_sort == "created_at"


def test_build_leads_list_context_exposes_list_query_and_page_meta(db_session):
    ctx = web_sales.build_leads_list_context(
        db_session,
        status=None,
        pipeline_id=None,
        stage_id=None,
        lead_source=None,
        search=None,
        page=1,
        per_page=25,
    )
    assert "list_query" in ctx
    assert "page_meta" in ctx
    assert ctx["page"] == ctx["page_meta"].page
    assert ctx["total"] == ctx["page_meta"].total_items
    assert ctx["list_query"].page == ctx["page_meta"].page


def test_build_leads_list_context_normalizes_stale_params(db_session):
    ctx = web_sales.build_leads_list_context(
        db_session,
        status="not-a-status",
        pipeline_id="not-a-uuid",
        stage_id="also-not-a-uuid",
        lead_source="not-a-source",
        search=None,
        sort_by="status",  # filterable, not sortable → falls back
        sort_dir="sideways",
        page=1,
        per_page=999,
    )
    query = ctx["list_query"]
    assert query.sort_by == "created_at"
    assert query.sort_dir == "desc"
    assert query.per_page == 25
    # Unknown filter values are cleared, not applied.
    assert query.filter_value("status") is None
    assert query.filter_value("lead_source") is None
    assert query.filter_value("pipeline_id") is None
    assert query.filter_value("stage_id") is None
    assert query.filter_value("owner_agent_id") is None
    assert ctx["canonicalization_needed"] is True


def test_lead_list_has_a_stable_id_tie_breaker_across_pages(db_session, subscriber):
    created = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    rows = [
        Lead(
            id=UUID(int=value),
            subscriber_id=subscriber.id,
            title=f"Lead {value}",
            created_at=created,
        )
        for value in (4, 1, 3, 2)
    ]
    db_session.add_all(rows)
    db_session.commit()

    first = sales.leads.list(
        db_session, None, None, None, None, None, "created_at", "desc", 2, 0
    )
    second = sales.leads.list(
        db_session, None, None, None, None, None, "created_at", "desc", 2, 2
    )

    assert [row.id.int for row in first + second] == [1, 2, 3, 4]


def test_lead_context_clamps_stale_page_into_canonical_query(db_session):
    ctx = web_sales.build_leads_list_context(
        db_session,
        status=None,
        pipeline_id=None,
        stage_id=None,
        lead_source=None,
        search=None,
        page=999,
        per_page=25,
    )
    assert ctx["list_query"].page == ctx["page_meta"].page == 1
    assert ctx["canonicalization_needed"] is True
