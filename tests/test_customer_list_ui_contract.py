from __future__ import annotations

import ast
from html.parser import HTMLParser
from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.services.list_query import PageMeta
from app.services.status_presentation import account_status_presentation
from app.services.web_customer_lists import build_customer_list_query

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class _FirstTagParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.attributes: dict[str, str | None] = {}

    def handle_starttag(self, _tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if not self.attributes:
            self.attributes = dict(attrs)


def test_customer_route_delegates_query_normalization_to_list_owner():
    route_path = PROJECT_ROOT / "app/web/admin/customers.py"
    tree = ast.parse(route_path.read_text(encoding="utf-8"))
    route = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "customers_list"
    )

    calls = {
        ast.unparse(node.func) for node in ast.walk(route) if isinstance(node, ast.Call)
    }
    args = {arg.arg: ast.unparse(arg.annotation) for arg in route.args.args}

    assert "web_customer_lists_service.build_customer_list_query" in calls
    assert "web_customer_lists_service.build_customers_index_context" in calls
    assert args["per_page"] == "str | None"


def test_customer_table_consumes_contract_urls_and_accessibility_state():
    template = (PROJECT_ROOT / "templates/admin/customers/_table.html").read_text(
        encoding="utf-8"
    )

    assert "list_query.url('/admin/customers'" in template
    assert 'aria-sort="' in template
    assert 'role="status"' in template
    assert 'aria-live="polite"' in template
    assert 'aria-current="page"' in template
    assert 'aria-label="Select all customers on this page"' in template
    assert "/admin/customers?page=" not in template
    assert "range(1, total_pages + 1)" not in template
    assert "status_presentation_badge(customer.status_presentation" in template
    assert "customer.raw.status" not in template
    assert "cust_status" not in template
    assert "customer.ipv4" in template
    assert "customer.ipv4_label" not in template
    assert "customer.name_presentation.display_text" in template
    assert "customer.name_presentation.full_text" in template
    assert "customer.name_presentation.is_truncated" in template
    assert "max-w-[188px]" in template
    assert 'aria-label="{{ customer.name }}"' in template
    assert 'aria-label="Select {{ customer.name }}"' in template
    assert (
        "{% if can_activate_subscriptions and "
        "customer.suspended_subscription_count %}" in template
    )
    assert (
        "{% if can_suspend_subscriptions and customer.active_subscription_count %}"
        in template
    )


def test_customer_status_surfaces_consume_semantic_presentation_owner():
    detail = (PROJECT_ROOT / "templates/admin/customers/detail.html").read_text(
        encoding="utf-8"
    )
    restricted = (
        PROJECT_ROOT / "templates/customer/dashboard/restricted.html"
    ).read_text(encoding="utf-8")
    mobile_chip = (PROJECT_ROOT / "mobile/lib/src/widgets/status_chip.dart").read_text(
        encoding="utf-8"
    )

    assert "customer_status_presentation" in detail
    assert "account_status_presentations" in detail
    assert "subscription_status_presentations" in detail
    assert "status_labels" not in detail
    assert "status_ring" not in detail
    assert "account_status_presentation" in restricted
    assert "account_status_display" not in restricted
    assert "StatusChip.fromPresentation" in mobile_chip
    assert "forSubscription" not in mobile_chip


def test_customer_detail_header_shows_only_the_subscriber_number():
    record_component = (PROJECT_ROOT / "templates/components/ui/record.html").read_text(
        encoding="utf-8"
    )
    subscriber_hero = record_component.split("{% macro subscriber_hero", maxsplit=1)[1]

    assert "summary.subscriber_number" in subscriber_hero
    assert "summary.account_number" not in subscriber_hero


def test_customer_filter_form_keeps_canonical_query_state_in_browser_history():
    template = (PROJECT_ROOT / "templates/admin/customers/index.html").read_text(
        encoding="utf-8"
    )

    assert 'hx-push-url="true"' in template
    assert 'name="sort" value="{{ list_query.sort_by }}"' in template
    assert 'name="dir" value="{{ list_query.sort_dir }}"' in template
    assert "e.detail.parameters.page = '1'" in template
    assert "stateSource.dataset.currentSort" in template
    assert "dynamic-table-config.js" not in template
    assert "data-dynamic-table" not in template
    assert "/api/v1/tables/customers" not in template


def test_customer_export_button_renders_accessible_checkbox_options():
    templates = Jinja2Templates(directory=str(PROJECT_ROOT / "templates"))

    html = templates.env.get_template("admin/customers/_export_controls.html").render()

    assert "Export CSV" not in html
    assert "@click=\"exportMenuOpen = !exportMenuOpen; exportError = ''\"" in html
    assert 'aria-haspopup="dialog"' in html
    assert 'aria-controls="customer-export-options"' in html
    assert 'id="customer-export-options"' in html
    assert 'role="dialog"' in html
    assert "Choose export columns" in html
    assert 'type="checkbox"' in html
    assert "exportOptions" in html
    assert "Advanced" in html
    assert "advancedExportOptions" in html
    assert "toggleExportOption(option.sourceColumn" in html
    assert "exportSelectedCustomers()" in html
    assert "exportLoading ? 'Preparing…' : 'Export'" in html
    assert 'aria-live="polite"' in html
    assert 'role="alert"' in html


def test_customer_multi_column_exports_project_the_complete_backend_csv():
    template = (PROJECT_ROOT / "templates/admin/customers/index.html").read_text(
        encoding="utf-8"
    )

    assert '{% include "admin/customers/_export_controls.html" %}' in template
    assert "selectedExportColumns: ['all']" in template
    assert "label: 'Full customer CSV'" in template
    assert "description: 'All available customer details'" in template
    assert "this.selectedExportColumns = checked ? ['all'] : [];" in template
    assert "filter((column) => column !== 'all')" in template
    assert "window.location.href = this.customerExportUrl();" in template
    assert "fetch(this.customerExportUrl()," in template
    assert "credentials: 'same-origin'" in template
    assert "parseCustomerCsv(await response.text())" in template
    assert "selectedColumnIndexes" in template
    assert "headers.indexOf(option.sourceColumn)" in template
    assert "selectedOptions.map((option) => option.csvHeader)" in template
    assert "...this.advancedExportOptions" in template
    assert "row.map((value) => this.exportCsvCell(value)).join(',')" in template
    assert "spreadsheetSafeText" in template
    assert "downloadCustomerCsv(content, filename)" in template
    assert "customers_selected_fields_" in template
    for source_column in (
        "name",
        "email",
        "phone",
        "is_active",
        "type",
        "created_at",
        "id",
        "account_number",
        "subscriber_number",
        "subscription_plans",
        "service_statuses",
        "locations",
        "pppoe_usernames",
        "service_ip_addresses",
        "nas_devices",
        "contact_completeness",
    ):
        assert f"sourceColumn: '{source_column}'" in template


def test_customer_bulk_message_requires_in_modal_preview_before_queueing():
    template = (PROJECT_ROOT / "templates/admin/customers/index.html").read_text(
        encoding="utf-8"
    )

    assert "messagePreview: null" in template
    assert "messagePreviewError: ''" in template
    assert "messagePreviewLoading: false" in template
    assert "messagePreviewReady: false" in template
    assert "Delivery Preview" in template
    assert "All filtered customers selected" in template
    assert "x-show=\"selectionMode === 'filtered'\"" in template
    assert "preview_only: true" in template
    assert "confirmed: true" in template
    assert "confirm(previewMessage)" not in template
    assert "queueConfirmedBulkMessage()" in template
    assert "invalidateMessagePreview()" in template
    assert '@change="invalidateMessagePreview()"' in template
    assert '@input="invalidateMessagePreview()"' in template
    assert "previewRequestId !== this.messagePreviewRequestId" in template


def test_customer_bulk_actions_use_server_contract_and_explicit_selection_scope():
    route = (PROJECT_ROOT / "app/web/admin/customers.py").read_text(encoding="utf-8")
    page = (PROJECT_ROOT / "templates/admin/customers/index.html").read_text(
        encoding="utf-8"
    )
    table = (PROJECT_ROOT / "templates/admin/customers/_table.html").read_text(
        encoding="utf-8"
    )

    assert "build_customer_bulk_action_contract" in route
    assert "customer_bulk_action_contract.actions" in page
    assert 'x-show="hasSelection()"' in page
    assert "Select all ${filteredTotal} customers matching these filters" in page
    assert "mode: 'filtered', filters: this.currentFilters()" in page
    assert "mode: 'selected', ids: this.selectedIds.map" in page
    assert "expected_count: preview.matched_count" in page
    assert "expected_scope_token: preview.scope_token" in page
    assert "data-customer-bulk-action" in page
    assert "[data-select-all]')?.focus()" in page
    assert "customer_ids: this.selectedIds" not in page
    assert "customers in the current filtered result" not in page
    assert "customer_bulk_action_contract.selection_enabled" in table
    assert ":disabled=\"selectionMode === 'filtered'\"" in table


def test_legacy_customer_data_api_delegates_to_customer_list_owner():
    table_service = (PROJECT_ROOT / "app/services/table_config.py").read_text(
        encoding="utf-8"
    )
    table_api = (PROJECT_ROOT / "app/api/tables.py").read_text(encoding="utf-8")

    assert "_apply_customers_page_filters" not in table_service
    assert (
        "web_customer_lists.build_customer_list_query_from_legacy_params"
        in table_service
    )
    assert "web_customer_lists.build_customer_list_page" in table_service
    assert "TableConfigurationService.build_data_projection" in table_api


def test_subscriber_compatibility_api_delegates_without_a_live_parallel_screen():
    table_service = (PROJECT_ROOT / "app/services/table_config.py").read_text(
        encoding="utf-8"
    )
    subscriber_owner = (
        PROJECT_ROOT / "app/services/web_subscriber_lists.py"
    ).read_text(encoding="utf-8")
    templates = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (PROJECT_ROOT / "templates").rglob("*.html")
    )

    assert "web_subscriber_lists.build_subscriber_list_page" in table_service
    assert "_ensure_subscriber_numbers" not in table_service
    assert "_apply_scalar_filters" not in table_service
    assert "subscriber_service.subscribers.query" in subscriber_owner
    assert 'data-table-key="subscribers"' not in templates
    assert 'data-dynamic-table="subscribers"' not in templates


def test_customer_table_contract_renders_with_empty_results():
    list_query = build_customer_list_query(
        search="missing",
        status=None,
        customer_type=None,
        nas_id=None,
        pop_site_id=None,
        sort_by="name",
        sort_dir="asc",
        page=1,
        per_page=25,
    )
    page_meta = PageMeta.from_query(list_query, total_items=0)
    templates = Jinja2Templates(directory=str(PROJECT_ROOT / "templates"))

    html = templates.env.get_template("admin/customers/_table.html").render(
        customers=[],
        list_query=list_query,
        page_meta=page_meta,
        search=list_query.search,
    )

    assert "No customers match the current search and filters." in html
    assert 'aria-sort="ascending"' in html
    assert "No customers found" in html


def test_customer_infrastructure_filter_is_lazy_and_bounded():
    template = (PROJECT_ROOT / "templates/admin/customers/index.html").read_text(
        encoding="utf-8"
    )
    service = (PROJECT_ROOT / "app/services/web_customer_lists.py").read_text(
        encoding="utf-8"
    )

    assert "/admin/customers/infrastructure-options" in template
    assert "x-data='infrastructurePicker({" in template
    assert 'x-data="infrastructurePicker({' not in template
    assert '@input="queueLookup()"' in template
    assert "window.setTimeout(() => this.lookup(), 300)" in template
    assert "@input.debounce.300ms" not in template
    assert "Infrastructure search could not be loaded" in template
    assert "Choose an infrastructure type first." in template
    assert ':disabled="!type"' not in template
    assert ":placeholder=" not in template
    assert "this.open = this.search.trim().length > 0" in template
    assert "limit: '20'" in template
    assert "nas_options" not in template
    assert "pop_site_options" not in template
    assert ".limit(bounded_limit)" in service
    assert "if len(term) < 2:" in service


def test_customer_infrastructure_picker_renders_complete_setup_expression():
    template = (PROJECT_ROOT / "templates/admin/customers/index.html").read_text(
        encoding="utf-8"
    )
    component_position = template.index("infrastructurePicker({")
    attribute_start = template.rfind("x-data=", 0, component_position)
    attribute_end = template.index("})'", component_position) + len("})'")
    attribute_template = template[attribute_start:attribute_end]

    templates = Jinja2Templates(directory=str(PROJECT_ROOT / "templates"))
    rendered = templates.env.from_string(f"<div {attribute_template}></div>").render(
        infrastructure_type="",
        infrastructure_id="",
        selected_infrastructure=None,
    )
    parser = _FirstTagParser()
    parser.feed(rendered)

    expression = parser.attributes["x-data"]
    assert expression is not None
    assert 'type: ""' in expression
    assert 'id: ""' in expression
    assert "selected: null" in expression
    assert expression.strip().endswith("})")


def test_semantic_status_macro_renders_owner_label_tone_and_icon():
    templates = Jinja2Templates(directory=str(PROJECT_ROOT / "templates"))
    template = templates.env.from_string(
        """
        {% from "components/ui/macros.html" import status_presentation_badge %}
        {{ status_presentation_badge(presentation, size="sm") }}
        """
    )

    html = template.render(
        presentation=account_status_presentation("suspended"),
    )

    assert "Suspended" in html
    assert "status-tone-warning" in html
    assert "M12 9v2" in html
    assert 'aria-label="Suspended status: warning"' in html
