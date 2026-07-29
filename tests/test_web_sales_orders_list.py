"""Admin sales-orders list is routed through list_query (Carbon/WCAG standard)."""

from __future__ import annotations

from app.services import web_sales


def test_sales_order_list_definition_declares_its_capabilities():
    definition = web_sales.SALES_ORDER_LIST_DEFINITION
    assert set(definition.sortable_keys) == {"order_number", "total", "created_at"}
    assert set(definition.filterable_keys) == {
        "status",
        "payment_status",
        "source_type",
        "owner_agent_id",
        "lead_source",
        "period",
        "from_date",
        "to_date",
    }
    assert definition.default_sort == "created_at"


def test_build_sales_orders_list_context_exposes_list_query_and_page_meta(db_session):
    ctx = web_sales.build_sales_orders_list_context(
        db_session,
        status=None,
        payment_status=None,
        source_type=None,
        search=None,
        page=1,
        per_page=25,
    )
    assert "list_query" in ctx
    assert "page_meta" in ctx
    assert ctx["page"] == ctx["page_meta"].page
    assert ctx["total"] == ctx["page_meta"].total_items


def test_build_sales_orders_list_context_normalizes_stale_params(db_session):
    ctx = web_sales.build_sales_orders_list_context(
        db_session,
        status="not-a-status",
        payment_status=None,
        source_type="weird",
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
    assert query.filter_value("source_type") is None
    assert ctx["canonicalization_needed"] is True


def test_sales_order_context_clamps_stale_page_into_query(db_session):
    ctx = web_sales.build_sales_orders_list_context(
        db_session,
        status=None,
        payment_status=None,
        source_type=None,
        search=None,
        page=999,
        per_page=25,
    )
    assert ctx["list_query"].page == ctx["page_meta"].page == 1
    assert ctx["canonicalization_needed"] is True
