from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from app.models.billing import (
    InvoiceDiscountAction,
    InvoiceDiscountSource,
    InvoiceDiscountType,
    InvoiceStatus,
)
from app.models.sales import QuoteDiscountAction, QuoteDiscountType, QuoteStatus
from app.services import invoice_discounts
from app.services import web_document_discount_report as report_service
from app.services.sales import quote_discount_reporting
from app.web.admin import billing_invoices as billing_routes
from app.web.admin import sales as sales_routes


def _request(path: str, query: bytes = b"") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [],
            "query_string": query,
            "scheme": "https",
            "server": ("example.test", 443),
        }
    )


def test_invoice_report_delegates_and_discloses_quote_inheritance(monkeypatch):
    invoice_id = uuid4()
    quote_id = uuid4()
    actor_id = uuid4()
    history_id = uuid4()
    captured: list[invoice_discounts.InvoiceDiscountHistoryQuery] = []

    def list_history(_db, query):
        captured.append(query)
        return invoice_discounts.InvoiceDiscountHistoryResult(
            items=(
                invoice_discounts.InvoiceDiscountHistoryItem(
                    history_id=history_id,
                    invoice_id=invoice_id,
                    invoice_number="INV-100",
                    revision=1,
                    customer_name="Example Customer",
                    currency="NGN",
                    original_subtotal=Decimal("100000"),
                    discount_type=InvoiceDiscountType.percentage,
                    discount_value=Decimal("10"),
                    discount_amount=Decimal("10000"),
                    discounted_subtotal=Decimal("90000"),
                    total_after_discount=Decimal("96750"),
                    reason="Approved concession",
                    actor_system_user_id=actor_id,
                    actor_name="Admin User",
                    applied_at=datetime(2026, 8, 7, 10, 0, tzinfo=UTC),
                    action=InvoiceDiscountAction.inherited,
                    source=InvoiceDiscountSource.quote,
                    source_quote_id=quote_id,
                    invoice_status=InvoiceStatus.issued,
                ),
            ),
            total_count=1,
            page=1,
            page_size=25,
        )

    monkeypatch.setattr(
        invoice_discounts, "list_invoice_discount_history", list_history
    )
    monkeypatch.setattr(
        invoice_discounts,
        "invoice_discount_actor_options",
        lambda _db: (
            invoice_discounts.InvoiceDiscountActorOption(
                system_user_id=actor_id, label="Admin User"
            ),
        ),
    )
    monkeypatch.setattr(
        report_service.display_format,
        "format_timestamp",
        lambda _value, _db: "2026-08-07 11:00 WAT",
    )

    report = report_service.build_document_discount_report(
        SimpleNamespace(),
        report_service.DocumentDiscountReportQuery(
            tab=report_service.DiscountReportTab.invoices,
            source=InvoiceDiscountSource.quote,
        ),
    )

    assert captured[0].source is InvoiceDiscountSource.quote
    assert report.total_count == 1
    assert report.rows[0].source_label == "Inherited from quote"
    assert report.rows[0].source_quote_url == f"/admin/sales/quotes/{quote_id}"
    assert report.rows[0].discount_amount_display == "NGN 10,000.00"
    assert report.rows[0].applied_at_display == "2026-08-07 11:00 WAT"


def test_quote_report_delegates_to_quote_history_owner(monkeypatch):
    quote_id = uuid4()
    actor_id = uuid4()
    captured: list[quote_discount_reporting.QuoteDiscountHistoryQuery] = []

    def list_history(_db, query):
        captured.append(query)
        return quote_discount_reporting.QuoteDiscountHistoryResult(
            items=(
                quote_discount_reporting.QuoteDiscountHistoryItem(
                    history_id=uuid4(),
                    quote_id=quote_id,
                    revision=2,
                    customer_name="Example Lead",
                    currency="USD",
                    original_subtotal=Decimal("500"),
                    discount_type=QuoteDiscountType.fixed_amount,
                    discount_value=Decimal("25"),
                    discount_amount=Decimal("25"),
                    discounted_subtotal=Decimal("475"),
                    total_after_discount=Decimal("475"),
                    reason=None,
                    actor_system_user_id=actor_id,
                    actor_name="Admin User",
                    applied_at=datetime(2026, 8, 7, 10, 0, tzinfo=UTC),
                    action=QuoteDiscountAction.changed,
                    quote_status=QuoteStatus.sent,
                ),
            ),
            total_count=1,
            page=1,
            page_size=25,
        )

    monkeypatch.setattr(
        quote_discount_reporting, "list_quote_discount_history", list_history
    )
    monkeypatch.setattr(
        quote_discount_reporting,
        "quote_discount_actor_options",
        lambda _db: (
            quote_discount_reporting.QuoteDiscountActorOption(
                system_user_id=actor_id, label="Admin User"
            ),
        ),
    )
    monkeypatch.setattr(
        report_service.display_format,
        "format_timestamp",
        lambda _value, _db: "2026-08-07 11:00 WAT",
    )

    report = report_service.build_document_discount_report(
        SimpleNamespace(),
        report_service.DocumentDiscountReportQuery(
            tab=report_service.DiscountReportTab.quotes,
            quote_status=QuoteStatus.sent,
        ),
    )

    assert captured[0].quote_status is QuoteStatus.sent
    assert report.rows[0].source_label == "Quote discount"
    assert report.rows[0].source_quote_url is None
    assert report.rows[0].discount_value_display == "USD 25.00"
    assert report.list_query.filter_value("tab") == "quotes"


def test_legacy_discount_urls_redirect_to_the_report_and_preserve_filters():
    invoice_response = billing_routes.invoice_discounts_report_redirect(
        _request(
            "/billing/invoice-discounts",
            b"customer=Acme&discount_type=percentage",
        )
    )
    quote_response = sales_routes.quote_discounts_report_redirect(
        _request("/sales/quote-discounts", b"quote_status=sent")
    )

    assert invoice_response.status_code == 307
    assert invoice_response.headers["location"] == (
        "/admin/reports/discounts?tab=invoices&customer=Acme&discount_type=percentage"
    )
    assert quote_response.status_code == 307
    assert quote_response.headers["location"] == (
        "/admin/reports/discounts?tab=quotes&quote_status=sent"
    )


def test_discount_report_template_renders_the_typed_projection(monkeypatch):
    monkeypatch.setattr(
        invoice_discounts,
        "list_invoice_discount_history",
        lambda _db, _query: invoice_discounts.InvoiceDiscountHistoryResult(
            items=(), total_count=0, page=1, page_size=25
        ),
    )
    monkeypatch.setattr(
        invoice_discounts, "invoice_discount_actor_options", lambda _db: ()
    )
    report = report_service.build_document_discount_report(
        SimpleNamespace(), report_service.DocumentDiscountReportQuery()
    )
    templates = Jinja2Templates(directory="templates")
    html = templates.env.get_template("admin/reports/discounts.html").render(
        request=_request("/admin/reports/discounts"),
        report=report,
        selected_tab=report_service.DiscountReportTab.invoices,
        error=None,
        current_user=None,
        sidebar_stats={},
        active_page="reports-discounts",
        active_menu="reports",
    )

    assert "Invoice Discounts" in html
    assert "Quote Discounts" in html
    assert "Inherited from quote" in html
    assert "No Invoices discount history matches these filters." in html


def test_custom_pricing_is_not_labelled_as_document_discounts():
    route_source = Path("app/web/admin/reports.py").read_text(encoding="utf-8")
    template_source = Path("templates/admin/reports/custom_pricing.html").read_text(
        encoding="utf-8"
    )
    assert '"name": "Custom Pricing"' in route_source
    assert "Custom Pricing & Discounts" not in template_source
