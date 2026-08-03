from __future__ import annotations

import csv
import io
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import event

from app.models.billing import Invoice, InvoiceStatus
from app.models.subscriber import Reseller, Subscriber, SubscriberCategory
from app.services import web_billing_overview as web_billing_overview_service
from app.services.web_billing_overview import (
    build_invoices_list_data,
    build_overview_data,
    render_invoices_csv,
)


def _reset_overview_cache() -> None:
    web_billing_overview_service._overview_cache.clear()


def test_build_overview_data_uses_short_ttl_cache(db_session, monkeypatch):
    _reset_overview_cache()
    calls = {"count": 0}

    class _FakeReporting:
        @staticmethod
        def get_dashboard_stats(_db, **_kwargs):
            calls["count"] += 1
            return {
                "stats": {"payments_count": 1},
                "period_comparison": [],
                "payment_method_breakdown": {"labels": [], "values": []},
                "daily_payments": {"labels": [], "values": []},
            }

    monkeypatch.setattr(
        "app.services.billing.reporting.billing_reporting",
        _FakeReporting,
    )

    first = build_overview_data(db_session, period="this_month")
    second = build_overview_data(db_session, period="this_month")

    assert first["stats"]["payments_count"] == 1
    assert second["stats"]["payments_count"] == 1
    assert calls["count"] == 1


def test_build_overview_data_adds_default_currency_displays(db_session, monkeypatch):
    _reset_overview_cache()

    class _FakeReporting:
        @staticmethod
        def get_dashboard_stats(_db, **_kwargs):
            return {
                "stats": {
                    "payments_amount": 1200,
                    "total_revenue": 800,
                    "unpaid_invoices_amount": 400,
                },
                "period_comparison": [],
                "payment_method_breakdown": {"labels": [], "values": []},
                "daily_payments": {"labels": [], "values": []},
            }

    monkeypatch.setattr(
        "app.services.billing.reporting.billing_reporting",
        _FakeReporting,
    )

    result = build_overview_data(db_session, period="this_month")

    assert result["default_currency"] == "NGN"
    assert result["stats"]["payments_amount_display"] == "NGN 1,200.00"
    assert result["stats"]["total_revenue_display"] == "NGN 800.00"
    assert result["stats"]["unpaid_invoices_amount_display"] == "NGN 400.00"


def test_build_overview_data_cache_is_scoped_by_filters(db_session, monkeypatch):
    _reset_overview_cache()
    calls = {"count": 0}

    class _FakeReporting:
        @staticmethod
        def get_dashboard_stats(_db, **_kwargs):
            calls["count"] += 1
            return {
                "stats": {"payments_count": calls["count"]},
                "period_comparison": [],
                "payment_method_breakdown": {"labels": [], "values": []},
                "daily_payments": {"labels": [], "values": []},
            }

    monkeypatch.setattr(
        "app.services.billing.reporting.billing_reporting",
        _FakeReporting,
    )

    first = build_overview_data(db_session, period="this_month")
    second = build_overview_data(db_session, period="last_month")

    assert first["stats"]["payments_count"] == 1
    assert second["stats"]["payments_count"] == 2
    assert calls["count"] == 2


def test_empty_invoice_totals_use_display_owner_default_currency(
    db_session, monkeypatch
):
    monkeypatch.setattr(
        "app.services.display_format.default_currency",
        lambda _db: "USD",
    )

    result = build_invoices_list_data(
        db_session,
        account_id=None,
        partner_id=None,
        status=None,
        customer_ref=None,
        search=None,
        start_date=None,
        end_date=None,
        page=1,
        per_page=25,
    )

    assert result["status_totals"]["all"]["display"] == "USD 0.00"
    assert result["status_totals"]["draft"]["due_display"] == "USD 0.00"


def _create_invoice(
    db_session,
    *,
    account_id,
    invoice_number: str,
    total: str,
    balance_due: str,
    status: InvoiceStatus,
    created_at: datetime,
    currency: str = "NGN",
):
    invoice = Invoice(
        account_id=account_id,
        invoice_number=invoice_number,
        status=status,
        currency=currency,
        subtotal=Decimal(total),
        tax_total=Decimal("0.00"),
        total=Decimal(total),
        balance_due=Decimal(balance_due),
        created_at=created_at,
    )
    db_session.add(invoice)
    db_session.commit()
    db_session.refresh(invoice)
    return invoice


def test_invoices_list_returns_status_totals_and_payment_split(db_session, subscriber):
    now = datetime.now(UTC)
    _create_invoice(
        db_session,
        account_id=subscriber.id,
        invoice_number="INV-100",
        total="100.00",
        balance_due="25.00",
        status=InvoiceStatus.partially_paid,
        created_at=now,
    )
    _create_invoice(
        db_session,
        account_id=subscriber.id,
        invoice_number="INV-101",
        total="80.00",
        balance_due="0.00",
        status=InvoiceStatus.paid,
        created_at=now,
    )

    result = build_invoices_list_data(
        db_session,
        account_id=None,
        partner_id=None,
        status=None,
        customer_ref=None,
        search=None,
        start_date=None,
        end_date=None,
        page=1,
        per_page=25,
    )

    assert result["status_totals"]["partially_paid"]["count"] == 1
    assert result["status_totals"]["paid"]["count"] == 1
    assert result["status_totals"]["all"]["count"] == 2
    assert result["status_totals"]["all"]["due_total"] == 25.0
    assert result["status_totals"]["all"]["received_total"] == 155.0
    presentations = result["invoice_status_presentations"]
    for invoice in result["invoices"]:
        assert presentations[str(invoice.id)].value == invoice.status.value


def test_invoices_list_status_totals_are_grouped_by_currency(db_session, subscriber):
    now = datetime.now(UTC)
    _create_invoice(
        db_session,
        account_id=subscriber.id,
        invoice_number="INV-NGN",
        total="100.00",
        balance_due="25.00",
        status=InvoiceStatus.issued,
        created_at=now,
        currency="NGN",
    )
    _create_invoice(
        db_session,
        account_id=subscriber.id,
        invoice_number="INV-USD",
        total="80.00",
        balance_due="10.00",
        status=InvoiceStatus.issued,
        created_at=now,
        currency="USD",
    )

    result = build_invoices_list_data(
        db_session,
        account_id=None,
        partner_id=None,
        status=None,
        customer_ref=None,
        search=None,
        start_date=None,
        end_date=None,
        page=1,
        per_page=25,
    )

    issued = result["status_totals"]["issued"]
    assert issued["amounts"] == {"NGN": Decimal("100.00"), "USD": Decimal("80.00")}
    assert issued["due_amounts"] == {"NGN": Decimal("25.00"), "USD": Decimal("10.00")}
    assert issued["received_amounts"] == {
        "NGN": Decimal("75.00"),
        "USD": Decimal("70.00"),
    }
    assert issued["display"] == "NGN 100.00, USD 80.00"
    assert issued["due_display"] == "NGN 25.00, USD 10.00"


def test_invoices_list_search_filters_invoice_numbers(db_session, subscriber):
    now = datetime.now(UTC)
    _create_invoice(
        db_session,
        account_id=subscriber.id,
        invoice_number="INV-MATCH-1",
        total="30.00",
        balance_due="30.00",
        status=InvoiceStatus.issued,
        created_at=now,
    )
    _create_invoice(
        db_session,
        account_id=subscriber.id,
        invoice_number="INV-OTHER-2",
        total="50.00",
        balance_due="50.00",
        status=InvoiceStatus.issued,
        created_at=now,
    )

    result = build_invoices_list_data(
        db_session,
        account_id=None,
        partner_id=None,
        status=None,
        customer_ref=None,
        search="MATCH",
        start_date=None,
        end_date=None,
        page=1,
        per_page=25,
    )

    assert result["total"] == 1
    assert len(result["invoices"]) == 1
    assert result["invoices"][0].invoice_number == "INV-MATCH-1"


def test_invoice_status_summary_preserves_other_status_tabs(db_session, subscriber):
    now = datetime.now(UTC)
    _create_invoice(
        db_session,
        account_id=subscriber.id,
        invoice_number="INV-STATUS-DRAFT",
        total="30.00",
        balance_due="30.00",
        status=InvoiceStatus.draft,
        created_at=now,
    )
    _create_invoice(
        db_session,
        account_id=subscriber.id,
        invoice_number="INV-STATUS-ISSUED",
        total="50.00",
        balance_due="50.00",
        status=InvoiceStatus.issued,
        created_at=now,
    )

    result = build_invoices_list_data(
        db_session,
        status="issued",
    )

    assert result["total"] == 1
    assert result["invoices"][0].invoice_number == "INV-STATUS-ISSUED"
    assert result["status_totals"]["all"]["count"] == 2
    assert result["status_totals"]["draft"]["count"] == 1
    assert result["status_totals"]["issued"]["count"] == 1


def test_invoices_list_filters_by_inclusive_start_and_end_dates(db_session, subscriber):
    now = datetime.now(UTC)
    _create_invoice(
        db_session,
        account_id=subscriber.id,
        invoice_number="INV-RECENT",
        total="45.00",
        balance_due="45.00",
        status=InvoiceStatus.issued,
        created_at=now - timedelta(days=1),
    )
    _create_invoice(
        db_session,
        account_id=subscriber.id,
        invoice_number="INV-OLD",
        total="60.00",
        balance_due="60.00",
        status=InvoiceStatus.issued,
        created_at=now - timedelta(days=50),
    )

    result = build_invoices_list_data(
        db_session,
        account_id=None,
        partner_id=None,
        status=None,
        customer_ref=None,
        search=None,
        start_date=(now - timedelta(days=7)).date(),
        end_date=now.date(),
        page=1,
        per_page=25,
    )

    numbers = {item.invoice_number for item in result["invoices"]}
    assert "INV-RECENT" in numbers
    assert "INV-OLD" not in numbers


def test_invoices_list_filters_by_partner(db_session):
    reseller_a = Reseller(name="Partner A")
    reseller_b = Reseller(name="Partner B")
    db_session.add_all([reseller_a, reseller_b])
    db_session.commit()

    account_a = Subscriber(
        first_name="Inv",
        last_name="A",
        email="inv-a@example.com",
        reseller_id=reseller_a.id,
    )
    account_b = Subscriber(
        first_name="Inv",
        last_name="B",
        email="inv-b@example.com",
        reseller_id=reseller_b.id,
    )
    db_session.add_all([account_a, account_b])
    db_session.commit()

    now = datetime.now(UTC)
    _create_invoice(
        db_session,
        account_id=account_a.id,
        invoice_number="INV-PA-1",
        total="75.00",
        balance_due="75.00",
        status=InvoiceStatus.issued,
        created_at=now,
    )
    _create_invoice(
        db_session,
        account_id=account_b.id,
        invoice_number="INV-PB-1",
        total="95.00",
        balance_due="95.00",
        status=InvoiceStatus.issued,
        created_at=now,
    )

    result = build_invoices_list_data(
        db_session,
        account_id=None,
        partner_id=str(reseller_a.id),
        status=None,
        customer_ref=None,
        search=None,
        start_date=None,
        end_date=None,
        page=1,
        per_page=25,
    )

    assert result["total"] == 1
    assert len(result["invoices"]) == 1
    assert result["invoices"][0].invoice_number == "INV-PA-1"
    assert result["selected_partner_id"] == str(reseller_a.id)


def test_render_invoices_csv_contains_customer_name_due_and_received_columns(
    db_session, subscriber
):
    now = datetime.now(UTC)
    invoice = _create_invoice(
        db_session,
        account_id=subscriber.id,
        invoice_number="INV-CSV-1",
        total="150.00",
        balance_due="40.00",
        status=InvoiceStatus.partially_paid,
        created_at=now,
    )

    rows = list(csv.reader(io.StringIO(render_invoices_csv([invoice]))))

    assert rows[0][:8] == [
        "invoice_id",
        "invoice_number",
        "customer_name",
        "status",
        "total",
        "balance_due",
        "payment_received",
        "currency",
    ]
    assert rows[1][1:8] == [
        "INV-CSV-1",
        "Test User",
        "partially_paid",
        "150.00",
        "40.00",
        "110.00",
        "NGN",
    ]
    assert str(subscriber.id) not in rows[1]


def test_render_invoices_csv_uses_business_customer_name_and_csv_escaping(
    db_session, subscriber
):
    subscriber.company_name = "Dotmac, \u0141\u00f3d\u017a"
    subscriber.category = SubscriberCategory.business
    db_session.commit()
    invoice = _create_invoice(
        db_session,
        account_id=subscriber.id,
        invoice_number="INV-CSV-BUSINESS",
        total="50.00",
        balance_due="50.00",
        status=InvoiceStatus.issued,
        created_at=datetime.now(UTC),
    )

    rows = list(csv.reader(io.StringIO(render_invoices_csv([invoice]))))

    assert rows[1][2] == "Dotmac, \u0141\u00f3d\u017a"
    assert len(rows[1]) == len(rows[0])


def test_stream_invoices_csv_matches_rendered_and_yields_incrementally(
    db_session, subscriber
):
    now = datetime.now(UTC)
    for idx in range(3):
        _create_invoice(
            db_session,
            account_id=subscriber.id,
            invoice_number=f"INV-STREAM-{idx}",
            total="100.00",
            balance_due="25.00",
            status=InvoiceStatus.partially_paid,
            created_at=now - timedelta(minutes=idx),
        )

    list_query = web_billing_overview_service.build_invoice_list_query(
        account_id=None,
        partner_id=None,
        status=None,
        proforma_only=False,
        customer_ref=None,
        search=None,
        start_date=None,
        end_date=None,
    )
    scope = web_billing_overview_service.list_invoices_for_scope(
        db_session, list_query=list_query
    )
    expected = render_invoices_csv(scope)

    statements: list[str] = []

    def _record_statement(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    bind = db_session.get_bind()
    db_session.expire_all()
    event.listen(bind, "before_cursor_execute", _record_statement)
    try:
        chunks = list(
            web_billing_overview_service.stream_invoices_csv(
                db_session, list_query=list_query
            )
        )
    finally:
        event.remove(bind, "before_cursor_execute", _record_statement)

    # Streamed output is byte-identical to the eager renderer...
    assert "".join(chunks) == expected
    # ...and it is emitted one row at a time (header + one chunk per invoice),
    # never as a single materialized body.
    assert len(chunks) == len(scope) + 1
    assert chunks[0].startswith("invoice_id,")
    assert all("INV-STREAM-" in chunk for chunk in chunks[1:])
    assert all("Test User" in chunk for chunk in chunks[1:])
    assert str(subscriber.id) not in "".join(chunks)
    assert (
        sum(statement.lstrip().upper().startswith("SELECT") for statement in statements)
        == 1
    )
