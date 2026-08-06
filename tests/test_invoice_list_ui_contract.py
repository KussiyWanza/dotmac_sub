from __future__ import annotations

import ast
import re
from datetime import date
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from app.http_query import OptionalDateQuery
from app.services.inclusive_date_range import InclusiveDateRangeError
from app.services.list_query import PageMeta
from app.services.web_billing_overview import build_invoice_list_query

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_invoice_route_delegates_query_normalization_to_list_owner():
    route_path = PROJECT_ROOT / "app/web/admin/billing_invoices.py"
    tree = ast.parse(route_path.read_text(encoding="utf-8"))
    route = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "invoices_list"
    )
    calls = {
        ast.unparse(node.func) for node in ast.walk(route) if isinstance(node, ast.Call)
    }
    args = {arg.arg: ast.unparse(arg.annotation) for arg in route.args.args}

    assert "web_billing_overview_service.build_invoice_list_query" in calls
    assert "web_billing_overview_service.build_invoices_list_data" in calls
    assert args["per_page"] == "str | None"
    assert args["start_date"] == "OptionalDateQuery"
    assert args["end_date"] == "OptionalDateQuery"


def test_invoice_export_route_delegates_csv_projection_to_list_owner():
    route_path = PROJECT_ROOT / "app/web/admin/billing_invoices.py"
    tree = ast.parse(route_path.read_text(encoding="utf-8"))
    route = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "invoices_export_csv"
    )
    calls = {
        ast.unparse(node.func) for node in ast.walk(route) if isinstance(node, ast.Call)
    }
    args = {arg.arg: ast.unparse(arg.annotation) for arg in route.args.args}

    assert "web_billing_overview_service.stream_invoices_csv" in calls
    assert "csv.writer" not in calls
    assert args["start_date"] == "OptionalDateQuery"
    assert args["end_date"] == "OptionalDateQuery"


def test_optional_date_query_accepts_empty_html_inputs_and_validates_dates():
    app = FastAPI()

    @app.get("/")
    def endpoint(value: OptionalDateQuery = None) -> dict[str, str | None]:
        return {"value": value.isoformat() if value else None}

    client = TestClient(app)

    empty_response = client.get("/", params={"value": ""})
    assert empty_response.status_code == 200
    assert empty_response.json() == {"value": None}

    valid_response = client.get("/", params={"value": "2026-08-05"})
    assert valid_response.status_code == 200
    assert valid_response.json() == {"value": "2026-08-05"}

    invalid_response = client.get("/", params={"value": "not-a-date"})
    assert invalid_response.status_code == 422


def test_invoice_query_normalizes_declared_state_and_rejects_unknown_values():
    query = build_invoice_list_query(
        account_id=None,
        partner_id=None,
        status=" ISSUED ",
        proforma_only=True,
        customer_ref=" person:customer-1 ",
        search=" INV-100 ",
        start_date=date(2026, 7, 1),
        end_date=date(2026, 7, 31),
        sort_by="total",
        sort_dir="asc",
        page=2,
        per_page="50",
    )

    assert query.search == "INV-100"
    assert query.filter_value("status") == "issued"
    assert query.filter_value("proforma_only") == "true"
    assert query.filter_value("customer_ref") == "person:customer-1"
    assert query.filter_value("start_date") == "2026-07-01"
    assert query.filter_value("end_date") == "2026-07-31"
    assert query.sort_by == "total"
    assert query.sort_dir == "asc"
    assert query.page == 2
    assert query.per_page == 50

    with pytest.raises(ValueError, match="Unsupported status filter"):
        build_invoice_list_query(
            account_id=None,
            partner_id=None,
            status="unknown",
            proforma_only=False,
            customer_ref=None,
            search=None,
            start_date=None,
            end_date=None,
        )

    with pytest.raises(
        InclusiveDateRangeError,
        match="start_date must be before or equal to end_date",
    ):
        build_invoice_list_query(
            account_id=None,
            partner_id=None,
            status=None,
            proforma_only=False,
            customer_ref=None,
            search=None,
            start_date=date(2026, 8, 2),
            end_date=date(2026, 8, 1),
        )


def test_invoice_full_and_htmx_views_share_the_list_contract_partial():
    page = (PROJECT_ROOT / "templates/admin/billing/invoices.html").read_text(
        encoding="utf-8"
    )
    table = (PROJECT_ROOT / "templates/admin/billing/_invoices_table.html").read_text(
        encoding="utf-8"
    )
    list_partial = (
        PROJECT_ROOT / "templates/admin/billing/_invoices_list.html"
    ).read_text(encoding="utf-8")

    assert '{% include "admin/billing/_invoices_list.html" %}' in page
    assert '{% include "admin/billing/_invoices_table.html" %}' in list_partial
    assert 'hx-push-url="true"' in list_partial
    assert 'name="sort" value="{{ list_query.sort_by }}"' in list_partial
    assert 'type="date" name="start_date"' in list_partial
    assert 'type="date" name="end_date"' in list_partial
    assert 'name="date_range"' not in list_partial
    assert "list_query.url('/admin/billing/invoices'" in table
    assert 'aria-sort="' in table
    assert 'aria-current="page"' in table
    assert 'role="status"' in table
    assert "/admin/billing/invoices?page=" not in table
    assert "range(1, total_pages + 1)" not in table
    assert "page_meta.start_item" in table
    table_start = table.index("<table")
    assert table.index('aria-label="Invoices per page"') < table_start
    assert table.index("Showing <span") < table_start
    assert table.index('aria-label="Invoice pagination"') > table_start


def test_invoice_customer_typeahead_refreshes_only_after_selection():
    list_partial = (
        PROJECT_ROOT / "templates/admin/billing/_invoices_list.html"
    ).read_text(encoding="utf-8")

    assert '@typeahead:selected="$el.requestSubmit()"' in list_partial
    hidden_customer_ref = re.search(
        r'<input\s+type="hidden"\s+name="customer_ref"[^>]*>',
        list_partial,
        re.DOTALL,
    )
    assert hidden_customer_ref is not None
    assert "hx-" not in hidden_customer_ref.group(0)


def test_invoice_list_exposes_loading_and_inline_failure_feedback():
    page = (PROJECT_ROOT / "templates/admin/billing/invoices.html").read_text(
        encoding="utf-8"
    )
    list_partial = (
        PROJECT_ROOT / "templates/admin/billing/_invoices_list.html"
    ).read_text(encoding="utf-8")

    assert 'hx-indicator="#invoice-list-loading"' in page
    assert "@htmx:response-error" in page
    assert "@htmx:send-error" in page
    assert "@htmx:timeout" in page
    assert 'id="invoice-list-loading"' in list_partial
    assert 'role="status"' in list_partial
    assert 'aria-live="polite"' in list_partial
    assert ':aria-busy="invoiceListLoading.toString()"' in list_partial
    assert 'id="invoice-list-error"' in list_partial
    assert 'role="alert"' in list_partial
    assert "retryInvoiceListRequest()" in list_partial
    assert list_partial.index('id="invoice-filters"') < list_partial.index(
        'id="invoice-list-error"'
    )
    assert list_partial.index('id="invoice-list-error"') < list_partial.index(
        'id="invoices-table"'
    )


def test_invoice_bulk_ui_consumes_server_contract_and_review_confirmation():
    page = (PROJECT_ROOT / "templates/admin/billing/invoices.html").read_text(
        encoding="utf-8"
    )
    table = (PROJECT_ROOT / "templates/admin/billing/_invoices_table.html").read_text(
        encoding="utf-8"
    )

    assert "invoice_bulk_action_contract.actions" in page
    assert "action.eligible_ids" in page
    assert "action.ineligible_reasons" in page
    assert "submitSelectionReview(actionKey)" in page
    assert "/bulk/review/${actionKey" in page
    assert "window.confirm" not in page
    assert "previewAction(" not in page
    assert "executeAction(" not in page
    assert "data-invoice-bulk-action" in page
    assert "bulkIssue()" not in page
    assert "bulkPrepareAndExportPdf" not in page
    assert "data-bulk-contract" in table
    assert "invoice_bulk_action_contract.selection_enabled" in table
    for hardcoded_action_color in (
        "bg-blue-600",
        "bg-emerald-600",
        "bg-indigo-600",
        "bg-violet-600",
        "bg-fuchsia-600",
        "bg-purple-600",
    ):
        assert hardcoded_action_color not in page


def test_invoice_table_contract_renders_with_empty_results():
    list_query = build_invoice_list_query(
        account_id=None,
        partner_id=None,
        status=None,
        proforma_only=False,
        customer_ref=None,
        search="missing",
        start_date=None,
        end_date=None,
        sort_by="invoice_number",
        sort_dir="asc",
    )
    page_meta = PageMeta.from_query(list_query, total_items=0)
    templates = Jinja2Templates(directory=str(PROJECT_ROOT / "templates"))

    html = templates.env.get_template("admin/billing/_invoices_table.html").render(
        invoices=[],
        invoice_status_presentations={},
        invoice_bulk_action_contract={"selection_enabled": False, "actions": []},
        list_query=list_query,
        page_meta=page_meta,
    )

    assert "No invoices found" in html
    assert 'aria-sort="ascending"' in html
    assert "Showing invoices 0 to 0 of 0." in html
