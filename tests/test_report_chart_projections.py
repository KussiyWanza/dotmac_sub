from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import NoReturn

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.models.catalog import ServiceType
from app.services import (
    crm_reporting,
    subscriber_growth,
    web_reports,
    web_reports_extended,
)
from app.services.billing import reporting as billing_reporting
from app.services.ui_contracts import ChartProjection, ChartSeries, StateValue
from app.web.admin import reports as report_routes


class _ZeroScalarSession:
    def scalar(self, _statement: object) -> int:
        return 0


def test_revenue_chart_distinguishes_zero_value_observation_from_no_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def project(observation_count: int) -> ChartProjection:
        monkeypatch.setattr(
            billing_reporting,
            "get_payments_revenue_summary",
            lambda *, db: billing_reporting.PaymentsRevenueSummary(
                total=Decimal("0"),
                current_month=Decimal("0"),
                previous_month=Decimal("0"),
                monthly=billing_reporting.MonthlyPaymentsRevenueSeries(
                    labels=("Jan", "Feb"),
                    values=(0.0, 0.0),
                    observation_count=observation_count,
                ),
            ),
        )
        monkeypatch.setattr(
            billing_reporting,
            "get_outstanding_receivables",
            lambda *, db: billing_reporting.OutstandingReceivablesSummary(
                amount=Decimal("0"), count=0
            ),
        )
        monkeypatch.setattr(
            billing_reporting, "get_total_invoiced", lambda *, db: Decimal("0")
        )
        monkeypatch.setattr(
            billing_reporting, "get_recurring_revenue", lambda *, db: Decimal("0")
        )
        monkeypatch.setattr(
            web_reports.billing_service.payments, "list", lambda **_kwargs: []
        )
        return web_reports.get_revenue_report_data(object()).revenue_chart

    assert project(0).is_empty
    present = project(1)
    assert present.is_present
    assert present.series[0].values == (0.0, 0.0)


def test_revenue_report_uses_compact_side_by_side_chart_and_payments() -> None:
    source = Path("templates/admin/reports/revenue.html").read_text()

    assert "data-revenue-overview-grid" in source
    assert "lg:grid-cols-2" in source
    assert "min_height=240" in source
    assert "max-h-[288px] overflow-x-hidden overflow-y-auto" in source
    assert "w-full table-fixed" in source
    assert "{% call data_table() %}" not in source


def test_admin_billing_revenue_trend_height_is_increased_by_twenty_percent() -> None:
    source = Path("templates/admin/billing/index.html").read_text()

    assert 'id="revenue-trend-chart"' in source
    assert 'style="height: 187.2px;"' in source
    assert 'style="height: 156px;"' not in source


def test_network_charts_render_empty_inventory_and_configured_zero_use_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty_facts = crm_reporting.NetworkInfrastructureFacts(
        olts=(),
        total_olts=0,
        active_olts=0,
        total_onts=0,
        connected_onts=0,
        recent_ont_activity=(),
        pools=(),
        used_ips=0,
        total_ips=0,
        active_vlans=0,
        pon_capacity=0,
        fiber_status=(),
        total_fdh=0,
        splitter_capacity=0,
    )
    monkeypatch.setattr(
        web_reports.crm_reporting_service,
        "network_infrastructure_facts",
        lambda *, db, hours=None: empty_facts,
    )
    empty = web_reports.get_network_report_data(object())
    assert empty.device_health_chart.is_empty
    assert empty.ip_pool_chart.is_empty

    pool_facts = replace(
        empty_facts,
        pools=(
            crm_reporting.NetworkPoolFacts(
                name="Customer IPv4",
                cidr="10.0.0.0/24",
                used_count=0,
                total_count=254,
            ),
        ),
        total_ips=254,
    )
    monkeypatch.setattr(
        web_reports.crm_reporting_service,
        "network_infrastructure_facts",
        lambda *, db, hours=None: pool_facts,
    )
    configured = web_reports.get_network_report_data(object())
    assert configured.ip_pool_chart.is_present
    assert configured.ip_pool_chart.series[0].values == (0.0,)


def test_churn_chart_has_an_explicit_empty_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subscriber_growth,
        "churn_summary",
        lambda *, db: subscriber_growth.ChurnSummary(
            total=0, cancelled_count=0, at_risk_count=0
        ),
    )
    monkeypatch.setattr(
        subscriber_growth,
        "monthly_churn_series",
        lambda *, db: subscriber_growth.MonthlyChurnSeries(
            labels=("Jan", "Feb"), rates=(0.0, 0.0), counts=(0, 0)
        ),
    )
    monkeypatch.setattr(subscriber_growth, "recent_cancellations", lambda *_a, **_k: [])
    monkeypatch.setattr(
        web_reports.crm_reporting_service,
        "subscription_churn_reason_counts",
        lambda *, db: (),
    )

    result = web_reports.get_churn_report_data(_ZeroScalarSession())

    assert result.churn_chart.is_empty
    assert "No cancellations" in str(result.churn_chart.message)


def test_churn_report_uses_reduced_trend_chart_height() -> None:
    source = Path("templates/admin/reports/churn.html").read_text()

    assert '"churn-trend-chart"' in source
    assert "min_height=168" in source
    assert "min_height=280" not in source


def test_churn_reasons_keep_labels_below_chart_without_duplicate_legend() -> None:
    source = Path("templates/admin/reports/churn.html").read_text()

    chart_position = source.index('id="churn-reasons-chart"')
    breakdown_position = source.index(
        "{% for reason, count in churn_reasons.items() %}"
    )
    assert chart_position < breakdown_position
    assert "{ legend: { display: false } }" in source
    assert "{ plugins: { legend: { display: false } } }" not in source


def test_recent_cancellations_card_keeps_its_natural_height() -> None:
    source = Path("templates/admin/reports/churn.html").read_text()

    assert "grid grid-cols-1 items-start gap-6 lg:grid-cols-2" in source
    assert "recent_cancellations | length > 10" not in source
    assert "max-height: 520px" not in source


def test_revenue_category_query_failure_is_unavailable_not_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*, db: object) -> NoReturn:
        raise SQLAlchemyError("database unavailable")

    monkeypatch.setattr(billing_reporting, "get_revenue_by_service_type", fail)
    rollbacks: list[bool] = []
    monkeypatch.setattr(report_routes, "_base_context", lambda *_a, **_k: {})
    monkeypatch.setattr(
        report_routes.templates,
        "TemplateResponse",
        lambda _template, context, status_code=200: {
            **context,
            "status_code": status_code,
        },
    )
    session = SimpleNamespace(rollback=lambda: rollbacks.append(True))

    result = report_routes.reports_revenue_categories(
        request=SimpleNamespace(), db=session
    )

    assert result["revenue_mix_chart"].is_unavailable
    assert not result["category_count"].is_present
    assert result["category_count"].placeholder == "Unavailable"
    assert result["status_code"] == 503
    assert rollbacks == [True]


def test_revenue_category_projection_is_empty_or_present_from_owner_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        billing_reporting, "get_revenue_by_service_type", lambda *, db: ()
    )
    empty = web_reports_extended.get_revenue_categories_data(object())
    assert empty.revenue_mix_chart.is_empty
    assert empty.category_count.value == 0

    monkeypatch.setattr(
        billing_reporting,
        "get_revenue_by_service_type",
        lambda *, db: (
            billing_reporting.RevenueByServiceTypeRow(
                service_type=ServiceType.residential,
                invoice_count=3,
                total=Decimal("1250.00"),
            ),
        ),
    )
    present = web_reports_extended.get_revenue_categories_data(object())
    assert present.revenue_mix_chart.is_present
    assert present.revenue_mix_chart.labels == (ServiceType.residential.value,)
    assert present.total_revenue.value == 1250.0


def test_revenue_category_template_composes_chart_and_table_side_by_side() -> None:
    template = report_routes.templates.env.get_template(
        "admin/reports/revenue_categories.html"
    )
    chart = ChartProjection.present(
        labels=("residential", "business", "Uncategorized"),
        series=(ChartSeries(label="Revenue", values=(100.0, 50.0, 25.0)),),
    )

    html = template.render(
        request=SimpleNamespace(
            url=SimpleNamespace(path="/admin/reports/revenue-categories"),
            state=SimpleNamespace(csrf_token=""),
        ),
        categories=(
            web_reports_extended.RevenueCategoryReportRow(
                name="residential", invoice_count=3, revenue=100.0
            ),
        ),
        category_count=StateValue.present(1),
        total_revenue=StateValue.present(100.0),
        revenue_mix_chart=chart,
        current_user=SimpleNamespace(name="Admin", email="admin@example.com"),
        sidebar_stats=SimpleNamespace(app_name="DotMac Subs"),
        unread_notifications=0,
        active_page="reports-revenue-categories",
        active_menu="reports",
        page_title="Revenue by Category",
    )

    assert "data-revenue-category-detail-grid" in html
    assert "lg:grid-cols-5" in html
    assert "lg:col-span-2" in html
    assert "lg:col-span-3" in html
    assert 'style="min-height: 250px;"' in html
    assert "Revenue Categories" in html
    assert "100.00" in html


def test_report_chart_macro_always_renders_a_visible_state() -> None:
    template = report_routes.templates.env.from_string(
        """
        {% from "components/ui/macros.html" import report_chart_card %}
        {% call report_chart_card("Revenue Trend", chart, "test-chart") %}
        <script>window.chartWouldInitialize = true;</script>
        {% endcall %}
        """
    )
    empty_html = template.render(chart=ChartProjection.empty("No revenue yet."))
    unavailable_html = template.render(
        chart=ChartProjection.unavailable("Revenue data is unavailable.")
    )
    present_html = template.render(
        chart=ChartProjection.present(
            labels=("Jan",),
            series=(ChartSeries(label="Revenue", values=(1.0,)),),
        )
    )

    assert "Revenue Trend" in empty_html
    assert 'data-report-chart-state="empty"' in empty_html
    assert "No revenue yet." in empty_html
    assert 'id="test-chart"' not in empty_html
    assert 'data-report-chart-state="unavailable"' in unavailable_html
    assert 'role="alert"' in unavailable_html
    assert "Revenue data is unavailable." in unavailable_html
    assert 'data-report-chart-state="present"' in present_html
    assert 'id="test-chart"' in present_html
    assert "chartWouldInitialize" in present_html


def test_technician_secondary_cards_use_compact_independent_heights() -> None:
    template = Path("templates/admin/reports/technician.html").read_text(
        encoding="utf-8"
    )

    assert "grid grid-cols-1 items-start gap-6 lg:grid-cols-2" in template
    assert 'chart-container mb-3" style="min-height: 160px;' in template
    assert "justify-between border-b border-slate-100 py-1" in template
