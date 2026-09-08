from pathlib import Path

from app.services import ticket_sla_reports, web_reports_extended

REPORT_TEMPLATES = Path("templates/admin/reports")


def test_every_previously_uncovered_report_exposes_an_export_control() -> None:
    expected = {
        "customer_retention_tracker.html": "/admin/customer-retention/export",
        "ticket_sla.html": "/admin/reports/ticket-sla/export",
        "discounts.html": "/admin/reports/discounts/export",
        "subscriber_growth.html": "/admin/reports/extended-export/subscriber-growth",
        "usage_by_plan.html": "/admin/reports/extended-export/usage-by-plan",
        "upcoming_charges.html": "/admin/reports/extended-export/upcoming-charges",
        "revenue_per_plan.html": "/admin/reports/extended-export/revenue-per-plan",
        "invoices.html": "/admin/reports/extended-export/invoices",
        "statements.html": "/admin/reports/extended-export/statements",
        "tax.html": "/admin/reports/extended-export/tax",
        "mrr.html": "/admin/reports/extended-export/mrr",
        "new_services.html": "/admin/reports/extended-export/new-services",
        "custom_pricing.html": "/admin/reports/extended-export/custom-pricing",
        "revenue_categories.html": "/admin/reports/extended-export/revenue-categories",
    }
    for template_name, endpoint in expected.items():
        source = (REPORT_TEMPLATES / template_name).read_text(encoding="utf-8")
        assert endpoint in source


def test_usage_by_plan_exposes_accessible_client_search() -> None:
    source = (REPORT_TEMPLATES / "usage_by_plan.html").read_text(encoding="utf-8")

    assert 'id="usage-plan-search"' in source
    assert 'data-usage-plan-row' in source
    assert 'aria-live="polite"' in source


def test_subscriber_growth_export_uses_the_owned_chart_projection(monkeypatch) -> None:
    monkeypatch.setattr(
        web_reports_extended,
        "get_subscriber_growth_data",
        lambda _db, *, days: {
            "chart_labels": ["2026-08-30", "2026-08-31"],
            "chart_data": [10, 12],
        },
    )

    export = web_reports_extended.build_extended_report_export(
        object(),
        web_reports_extended.ExtendedReportExportQuery(
            kind=web_reports_extended.ExtendedReportExportKind.subscriber_growth,
            days=2,
        ),
    )

    assert export.filename == "subscriber-growth.csv"
    assert export.content.splitlines() == [
        "date,total_subscribers",
        "2026-08-30,10",
        "2026-08-31,12",
    ]


def test_upcoming_charges_export_preserves_the_redesigned_report_filters(
    monkeypatch,
) -> None:
    captured = {}

    def fake_page(_db, **kwargs):
        captured.update(kwargs)
        return {
            "charges": [
                {
                    "customer_name": "Ada Customer",
                    "plan_name": "Business 100",
                    "reference": "INV-100",
                    "mode": "prepaid",
                    "due_at": "2026-09-01",
                    "days_remaining": 1,
                    "amount_display": "NGN 25,000.00",
                    "funding_display": "NGN 5,000.00",
                    "needed_display": "NGN 20,000.00",
                    "status_label": "Upcoming",
                }
            ],
            "has_next": False,
        }

    monkeypatch.setattr(web_reports_extended, "get_upcoming_charges_data", fake_page)
    export = web_reports_extended.build_extended_report_export(
        object(),
        web_reports_extended.ExtendedReportExportQuery(
            kind=web_reports_extended.ExtendedReportExportKind.upcoming_charges,
            mode="prepaid",
            state="upcoming",
            band="high",
            include_funded=True,
        ),
    )

    assert captured == {
        "mode": "prepaid",
        "state": "upcoming",
        "band": "high",
        "include_funded": True,
        "page": 1,
        "per_page": 50,
    }
    assert "Ada Customer,Business 100,INV-100,prepaid" in export.content


def test_ticket_sla_export_preserves_the_filter_and_columns(monkeypatch) -> None:
    captured = {}

    def fake_records(_db, **kwargs):
        captured.update(kwargs)
        return [
            {
                "ticket_reference": "TKT-1",
                "title": "Slow response",
                "status": "open",
                "service_team": "Support",
                "assignee": "Agent",
                "due_at": "2026-08-31T09:00:00Z",
                "breached_at": "2026-08-31T10:00:00Z",
                "breach_duration": "1h",
                "priority": "high",
            }
        ]

    monkeypatch.setattr(ticket_sla_reports, "violation_records", fake_records)
    content = ticket_sla_reports.build_violation_export_csv(
        object(), ticket_sla_reports.TicketSlaExportQuery(open_only=True)
    )

    assert captured["open_only"] is True
    assert captured["limit"] == 10000
    assert "ticket_reference,title,status,service_team,assignee" in content
    assert "TKT-1,Slow response,open,Support,Agent" in content
