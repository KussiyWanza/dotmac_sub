from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_payment_templates_consume_shared_status_presentation() -> None:
    paths = (
        "templates/admin/billing/payments.html",
        "templates/admin/billing/payment_detail.html",
        "templates/admin/customers/detail.html",
        "templates/admin/resellers/detail.html",
        "templates/reseller/accounts/invoice_detail.html",
    )

    for path in paths:
        assert "status_presentation_badge" in _read(path), path

    payment_list = _read("templates/admin/billing/payments.html")
    payment_detail = _read("templates/admin/billing/payment_detail.html")
    reseller_invoice = _read("templates/reseller/accounts/invoice_detail.html")

    assert "payment_status_variants" not in payment_detail
    assert "status_val == 'failed'" not in payment_list
    assert "pstatus_lower" not in reseller_invoice
    assert "['succeeded', 'completed', 'paid']" not in reseller_invoice


def test_customer_mobile_payment_surface_renders_server_presentation() -> None:
    model = _read("mobile/lib/src/models/invoice.dart")
    screen = _read("mobile/lib/src/features/billing/invoices_screen.dart")

    assert "json['status_presentation']" in model
    assert "StatusChip.fromPresentation(" in screen
    assert "p.statusPresentation" in screen
    assert "StatusChip(p.status)" not in screen


def test_payment_list_uses_explicit_created_date_filters() -> None:
    payment_list = _read("templates/admin/billing/payments.html")

    assert 'type="date" name="start_date"' in payment_list
    assert 'type="date" name="end_date"' in payment_list
    assert 'name="date_range"' not in payment_list
    assert "list_query.url('/admin/billing/payments/export.csv')" in payment_list
    assert "&start_date={{ start_date }}" in payment_list
    assert "&end_date={{ end_date }}" in payment_list


def test_payments_table_controls_render_above_table() -> None:
    template = _read("templates/admin/billing/payments.html")

    controls_position = template.index('data-testid="payments-table-controls"')
    table_position = template.index('<div class="overflow-x-auto">', controls_position)
    pagination_position = template.index('data-testid="payments-pagination"')

    assert controls_position < table_position < pagination_position
    assert template.count("Showing <span") == 1
    assert template.count("{% for size in [10, 25, 50, 100] %}") == 1
