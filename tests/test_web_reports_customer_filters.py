from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from app.models.bandwidth import BandwidthSample
from app.models.subscriber import SubscriberStatus, UserType
from app.services import web_reports


def _subscriber(email: str, status: SubscriberStatus, created_at: datetime):
    return SimpleNamespace(
        email=email,
        status=status,
        is_active=status == SubscriberStatus.active,
        metadata_=None,
        splynx_customer_id=None,
        account_start_date=None,
        created_at=created_at,
    )


def test_customer_report_is_visible_from_reports_hub():
    route_source = Path("app/web/admin/reports.py").read_text(encoding="utf-8")
    page_template = Path("templates/admin/reports/subscribers.html").read_text(
        encoding="utf-8"
    )

    assert '"name": "Customer Report"' in route_source
    assert '"url": "/admin/reports/customers"' in route_source
    assert '"/customers"' in route_source
    # The export is gated with the same permission as the page (customer:read).
    assert (
        '"/customers/export", '
        'dependencies=[Depends(require_permission("customer:read"))]' in route_source
    )
    assert "Customer Report - Admin" in page_template
    assert 'action="/admin/reports/customers"' in page_template
    assert 'action="/admin/reports/customers/export"' in page_template


def test_customer_report_breakdowns_scroll_only_beyond_fifteen_records():
    page_template = Path("templates/admin/reports/subscribers.html").read_text(
        encoding="utf-8"
    )

    assert "grid grid-cols-1 items-start gap-6 lg:grid-cols-2" in page_template
    assert "plan_distribution|length > 15" in page_template
    assert "regional_breakdown|length > 15" in page_template
    assert (
        page_template.count(
            'style="max-height: 29.25rem; overflow-x: hidden; overflow-y: auto;"'
        )
        == 2
    )


def test_customer_growth_chart_height_is_reduced_by_forty_percent():
    page_template = Path("templates/admin/reports/subscribers.html").read_text(
        encoding="utf-8"
    )

    assert 'id="subscriber-growth-chart"' in page_template
    assert 'style="min-height: 58.8px;"' in page_template
    assert 'style="min-height: 98px;"' not in page_template


def test_matching_customers_scrolls_beyond_fifteen_and_has_page_search():
    page_template = Path("templates/admin/reports/subscribers.html").read_text(
        encoding="utf-8"
    )

    assert "customers|length > 15" in page_template
    assert "max-height: 48rem; overflow-y: auto;" in page_template
    assert 'id="matching-customers-search"' in page_template
    assert "Search displayed customers..." in page_template
    assert "data-matching-customer-row" in page_template
    assert 'id="matching-customers-no-results"' in page_template
    assert 'class="h-[43.75rem] overflow-auto"' not in page_template


def test_by_status_card_height_is_reduced_by_thirty_percent():
    page_template = Path("templates/admin/reports/subscribers.html").read_text(
        encoding="utf-8"
    )

    assert 'style="height: 21.7rem;"' in page_template
    assert 'style="min-height: 126px;"' in page_template
    assert 'style="min-height: 180px;"' not in page_template
    assert page_template.count('class="h-[31rem] [&>div]:h-full"') == 1


def test_customer_report_includes_usage_for_filtered_period(
    db_session, subscriber, subscription
):
    subscriber.first_name = "Usage"
    subscriber.last_name = "Customer"
    subscriber.status = SubscriberStatus.active
    subscriber.user_type = UserType.customer
    subscriber.created_at = datetime(2026, 1, 10, tzinfo=UTC)
    db_session.add(
        BandwidthSample(
            subscription_id=subscription.id,
            rx_bps=4_000_000,
            tx_bps=1_000_000,
            sample_at=datetime(2026, 1, 20, 12, tzinfo=UTC),
        )
    )
    db_session.commit()

    data = web_reports.get_subscribers_report_data(
        db_session,
        date_from="2026-01-01",
        date_to="2026-01-31",
        status="active",
    )

    customer = data["customers"][0]
    assert customer.email == subscriber.email
    assert customer.period_avg_mbps == 5
    assert customer.period_usage_gb > 0
    assert data["total_usage_gb"] == customer.period_usage_gb
    assert data["usage_date_from"] == "2026-01-01"
    assert data["usage_date_to"] == "2026-01-31"

    csv_content = web_reports.build_subscribers_export_csv(
        db_session,
        date_from="2026-01-01",
        date_to="2026-01-31",
        status="active",
    )
    assert "period_usage_gb,period_avg_mbps,period_active_services" in csv_content
    assert "Usage Customer" in csv_content
    assert ",5.0,1" in csv_content


def test_recent_signups_use_a_view_model_without_mutating_subscriber(
    db_session, subscriber
):
    subscriber.status = SubscriberStatus.active
    subscriber.is_active = True
    subscriber.user_type = UserType.customer
    subscriber.created_at = datetime(2026, 1, 10, tzinfo=UTC)
    db_session.commit()

    data = web_reports.get_subscribers_report_data(db_session)

    recent = next(
        row for row in data["recent_subscribers"] if row.name == subscriber.name
    )
    assert recent.derived_status == SubscriberStatus.active
    assert subscriber.status == SubscriberStatus.active
    assert not hasattr(subscriber, "derived_status")
