from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_invoice_discount_writer_is_a_flush_only_participant() -> None:
    source = _source("app/services/invoice_discounts.py")
    stage = source[
        source.index("def stage_invoice_discount(") : source.index(
            "def _current_resolved("
        )
    ]
    assert "db.flush()" in stage
    assert "db.commit()" not in stage
    assert "db.rollback()" not in stage
    assert "begin_nested" not in stage


def test_invoice_discount_is_inside_typed_creation_boundaries() -> None:
    drafts = _source("app/services/invoice_draft_authoring.py")
    deposits = _source("app/services/quote_deposits.py")
    assert "InvoiceDiscountInput | None" in drafts
    assert "stage_invoice_discount(" in drafts
    assert "StageInvoiceDiscountCommand(" in deposits
    assert "source=InvoiceDiscountSource.quote" in deposits
    assert "commit=False" in deposits


def test_invoice_discount_history_has_database_and_orm_immutability() -> None:
    model = _source("app/models/billing.py")
    migration = _source("alembic/versions/483_invoice_discount_history.py")
    assert '@event.listens_for(InvoiceDiscountHistory, "before_update")' in model
    assert '@event.listens_for(InvoiceDiscountHistory, "before_delete")' in model
    assert "trg_invoice_discount_history_append_only" in migration
    assert "BEFORE UPDATE OR DELETE" in migration


def test_invoice_discount_routes_only_adapt_typed_services() -> None:
    billing_route = _source("app/web/admin/billing_invoices.py")
    sales_route = _source("app/web/admin/sales.py")
    report_route = _source("app/web/admin/reports.py")
    projection = _source("app/services/web_document_discount_report.py")
    assert '"/invoice-discounts"' in billing_route
    assert '"/quote-discounts"' in sales_route
    assert "/admin/reports/discounts?tab=invoices" in billing_route
    assert "/admin/reports/discounts?tab=quotes" in sales_route
    assert '"/discounts"' in report_route
    assert "DocumentDiscountReportQuery(" in report_route
    assert "build_document_discount_report(" in report_route
    assert "list_invoice_discount_history(" in projection
    assert "list_quote_discount_history(" in projection
    assert "InvoiceDiscountHistory(" not in report_route
    assert "QuoteDiscountHistory(" not in report_route


def test_invoice_discount_ui_exposes_controls_and_history_filters() -> None:
    form = _source("templates/admin/billing/invoice_form.html")
    history = _source("templates/admin/reports/discounts.html")
    for field in ("discount_type", "discount_value", "discount_reason"):
        assert f'name="{field}"' in form
    assert "Final discount amount" in form
    assert "Applied by" in form
    assert "Date applied" in form
    for field in (
        "date_from",
        "date_to",
        "search",
        "salesperson_id",
        "discount_type",
    ):
        assert f'name="{field}"' in history
    assert "invoice_status" in history
    assert "Inherited from quote" in history
    assert "View source Quote" in history


def test_discount_history_has_one_rendered_report_location() -> None:
    invoice_list = _source("templates/admin/billing/invoices.html")
    quote_list = _source("templates/admin/sales/quotes/index.html")
    quote_detail = _source("templates/admin/sales/quotes/detail.html")
    assert "/admin/billing/invoice-discounts" not in invoice_list
    assert "/admin/sales/quote-discounts" not in quote_list
    assert "/admin/sales/quote-discounts" not in quote_detail
    assert not (ROOT / "templates/admin/billing/invoice_discounts.html").exists()
    assert not (ROOT / "templates/admin/sales/quotes/discounts.html").exists()
