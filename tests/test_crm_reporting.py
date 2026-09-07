from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.collections import DunningCase
from app.models.network import OLTDevice, OntUnit, OnuOnlineStatus
from app.models.provisioning import (
    AppointmentStatus,
    InstallAppointment,
    ServiceOrder,
    ServiceOrderStatus,
)
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services import crm_reporting, provisioning_managers, web_reports
from app.services.sot_registry.registry import registry_validation_errors
from app.services.ui_contracts import ChartProjection
from app.web.admin import reports as report_routes


def test_expected_operational_inventory_is_complete_and_exclusions_stay_excluded():
    assert set(crm_reporting.REPORT_DEFINITIONS) == set(crm_reporting.CrmReportSlug)
    assert len(crm_reporting.REPORT_DEFINITIONS) == 13

    hub_names = {
        link["name"]
        for section in report_routes.REPORT_HUB_SECTIONS
        for link in section["links"]
    }
    active_report_titles = {
        definition.title
        for slug, definition in crm_reporting.REPORT_DEFINITIONS.items()
        if slug is not crm_reporting.CrmReportSlug.CRM_PERFORMANCE
    }
    assert active_report_titles <= hub_names
    assert "CRM Performance" not in hub_names
    assert "Quarterly Report" not in hub_names
    assert "Customer Retention" in hub_names
    assert "Lead Performance" in hub_names
    assert "Sales Order Performance" in hub_names
    links = {
        link["name"]: link
        for section in report_routes.REPORT_HUB_SECTIONS
        for link in section["links"]
    }
    assert links["Lead Performance"]["url"] == "/admin/reports/sales/leads"
    assert links["Lead Performance"]["permission"] == "crm:lead:read"
    assert links["Sales Order Performance"]["url"] == "/admin/reports/sales/orders"
    assert links["Sales Order Performance"]["permission"] == "crm:sales_order:read"

    service_source = Path("app/services/crm_reporting.py").read_text(encoding="utf-8")
    assert "CustomerRetentionEngagement" not in service_source
    assert "retention notes" not in service_source.lower()

    data_flow_guide = Path("docs/designs/CRM_REPORT_DATA_FLOW_GUIDE.md").read_text(
        encoding="utf-8"
    )
    for required_flow in (
        "Shared operational projection",
        "Billing aggregation",
        "Customer/network observations",
        "Team inbox metrics",
        "NCC ownership",
    ):
        assert required_flow in data_flow_guide

    analytics_migration = Path(
        "alembic/versions/567_inbox_agent_analytics_indexes.py"
    ).read_text(encoding="utf-8")
    for index_name in (
        "ix_inbox_conversations_agent_analytics",
        "ix_inbox_assignments_agent_analytics",
        "ix_inbox_status_event_agent_analytics",
        "ix_inbox_messages_agent_analytics",
    ):
        assert index_name in analytics_migration
    assert "CREATE INDEX CONCURRENTLY" in analytics_migration


@pytest.mark.parametrize(
    "slug",
    [
        slug
        for slug in crm_reporting.CrmReportSlug
        if slug is not crm_reporting.CrmReportSlug.CRM_PERFORMANCE
    ],
)
def test_every_operational_report_has_a_typed_empty_state(db_session, slug):
    report = crm_reporting.get_report(
        db_session,
        slug=slug,
        query=crm_reporting.CrmReportQuery(),
    )

    assert report.definition.slug == slug
    assert report.total >= 0
    assert len(report.columns) > 0
    expected_header = (report.export_columns or report.columns)[0]
    assert crm_reporting.build_csv(report).startswith(expected_header)


def test_retention_page_fetches_twenty_rows_at_a_time(db_session):
    subscribers = [
        Subscriber(
            first_name="Retention",
            last_name=f"Customer {index:02d}",
            email=f"retention-{uuid4().hex}@example.com",
        )
        for index in range(30)
    ]
    paid_customer = Subscriber(
        first_name="Paid",
        last_name="Customer",
        email=f"paid-{uuid4().hex}@example.com",
    )
    db_session.add_all([*subscribers, paid_customer])
    db_session.flush()
    db_session.add_all(
        DunningCase(account_id=subscriber.id) for subscriber in subscribers
    )
    db_session.commit()

    first = crm_reporting.get_customer_retention_page(
        db_session, query=crm_reporting.CustomerRetentionPageQuery(page=1)
    )
    second = crm_reporting.get_customer_retention_page(
        db_session, query=crm_reporting.CustomerRetentionPageQuery(page=2)
    )

    assert len(first.rows) == 20
    assert first.total_count == 30
    assert first.has_next
    assert second.page == 2
    assert len(second.rows) == 10
    assert str(paid_customer.id) not in {
        row.customer_id for row in (*first.rows, *second.rows)
    }
    assert {row.customer_id for row in first.rows}.isdisjoint(
        row.customer_id for row in second.rows
    )


def test_agent_performance_period_defaults_and_presets_use_lagos_boundaries():
    now = datetime(2026, 8, 28, 23, 30, tzinfo=UTC)

    month = crm_reporting.resolve_agent_performance_period(
        preset=crm_reporting.AgentPerformancePeriodPreset.MONTH,
        now=now,
    )
    today = crm_reporting.resolve_agent_performance_period(
        preset=crm_reporting.AgentPerformancePeriodPreset.TODAY,
        now=now,
    )
    week = crm_reporting.resolve_agent_performance_period(
        preset=crm_reporting.AgentPerformancePeriodPreset.WEEK,
        now=now,
    )

    assert (month.start_date, month.end_date) == (
        date(2026, 8, 1),
        date(2026, 8, 31),
    )
    assert month.start_at == datetime(2026, 7, 31, 23, 0, tzinfo=UTC)
    assert month.end_at == datetime(2026, 8, 31, 23, 0, tzinfo=UTC)
    assert today.start_date == today.end_date == date(2026, 8, 29)
    assert (week.start_date, week.end_date) == (
        date(2026, 8, 24),
        date(2026, 8, 30),
    )


def test_admin_and_personal_agent_reports_render_display_name_not_uuid(
    db_session, monkeypatch
):
    person_id = uuid4()
    team_id = uuid4()
    analytics = crm_reporting.team_inbox_metrics.InboxAgentPerformanceAnalyticsPage(
        rows=(
            crm_reporting.team_inbox_metrics.InboxAgentPerformanceAnalyticsRow(
                person_id=person_id,
                agent_name="Ada Agent",
                service_team_id=team_id,
                service_team_name="Support",
                assigned_conversation_count=4,
                resolved_conversation_count=3,
                active_assignment_count=1,
                average_resolution_seconds=1800,
                average_first_response_seconds=300,
            ),
        ),
        summary=crm_reporting.team_inbox_metrics.InboxAgentPerformanceSummary(
            agent_count=1,
            assigned_conversation_count=4,
            resolved_conversation_count=3,
            active_assignment_count=1,
            average_resolution_seconds=1800,
            average_first_response_seconds=300,
        ),
        total=1,
        page=1,
        per_page=50,
        generated_at=datetime.now(UTC),
    )
    monkeypatch.setattr(
        crm_reporting.team_inbox_metrics,
        "agent_performance_analytics",
        lambda *_args, **_kwargs: analytics,
    )
    query = crm_reporting.CrmReportQuery(
        date_from=date(2026, 8, 1),
        date_to=date(2026, 8, 31),
        person_id=person_id,
    )

    for slug in (
        crm_reporting.CrmReportSlug.AGENT_PERFORMANCE,
        crm_reporting.CrmReportSlug.MY_PERFORMANCE,
    ):
        report = crm_reporting.get_report(db_session, slug=slug, query=query)
        assert report.rows[0][0] == "Ada Agent"
        assert str(person_id) not in report.rows[0]


def test_network_report_uses_uncapped_counts_and_observed_ont_status(db_session):
    for index in range(101):
        db_session.add(
            OLTDevice(
                name=f"OLT {index}",
                hostname=f"olt-{index}",
                mgmt_ip=f"10.0.0.{index + 1}",
                is_active=True,
            )
        )
    db_session.add_all(
        [
            OntUnit(
                serial_number="ONLINE-ONT",
                is_active=True,
                olt_status=OnuOnlineStatus.online,
            ),
            OntUnit(
                serial_number="OFFLINE-ONT",
                is_active=True,
                olt_status=OnuOnlineStatus.offline,
            ),
        ]
    )
    db_session.commit()

    report = web_reports.get_network_report_data(db_session)

    assert report.total_olts == 101
    assert report.active_olts == 101
    assert report.total_onts == 2
    assert report.connected_onts == 1
    assert all(isinstance(item, crm_reporting.NetworkOltFacts) for item in report.olts)
    assert any(item.mgmt_ip == "10.0.0.1" for item in report.olts)
    assert all(
        isinstance(item, crm_reporting.NetworkOntFacts)
        for item in report.recent_ont_activity
    )
    assert sum(item.is_online for item in report.recent_ont_activity) == 1
    assert report.device_health_chart.is_present


def test_network_route_wires_all_infrastructure_capacity_metrics(
    db_session, monkeypatch
):
    import app.web.admin as admin_web

    report_data = web_reports.NetworkReportData(
        total_olts=2,
        active_olts=1,
        total_onts=8,
        connected_onts=7,
        ip_pool_usage=25.0,
        used_ips=64,
        total_ips=256,
        active_vlans=4,
        pon_capacity=128,
        pon_utilization=6.25,
        total_fiber_strands=48,
        available_fiber_strands=12,
        total_fdh=3,
        splitter_capacity=64,
        olts=(),
        pool_data=(),
        recent_ont_activity=(),
        fiber_status={},
        device_health_chart=ChartProjection.empty("No inventory."),
        ip_pool_chart=ChartProjection.empty("No pools."),
    )
    monkeypatch.setattr(
        web_reports, "get_network_report_data", lambda **_kwargs: report_data
    )
    monkeypatch.setattr(admin_web, "get_current_user", lambda _request: None)
    monkeypatch.setattr(admin_web, "get_sidebar_stats", lambda _db: {})
    monkeypatch.setattr(
        report_routes, "recent_activity_for_paths", lambda *_args, **_kwargs: []
    )
    monkeypatch.setattr(
        report_routes.templates,
        "TemplateResponse",
        lambda _template, context: context,
    )

    context = report_routes.reports_network(SimpleNamespace(), db_session)

    for key in (
        "pon_capacity",
        "pon_utilization",
        "total_fiber_strands",
        "available_fiber_strands",
        "total_fdh",
        "splitter_capacity",
        "device_health_chart",
        "ip_pool_chart",
    ):
        assert context[key] == getattr(report_data, key)


def test_online_activity_maps_the_internal_api_contract(monkeypatch, db_session):
    monkeypatch.setattr(
        crm_reporting.crm_api,
        "online_subscribers",
        lambda *_args, **_kwargs: (
            [
                {
                    "id": "subscriber-id",
                    "subscriber_number": "SUB-1001",
                    "status": "active",
                    "last_seen": "2026-08-12T09:30:00+00:00",
                }
            ],
            1,
        ),
    )

    report = crm_reporting.get_report(
        db_session,
        slug=crm_reporting.CrmReportSlug.ONLINE_ACTIVITY,
        query=crm_reporting.CrmReportQuery(),
    )

    assert report.columns == ("Subscriber number", "Status", "Last activity")
    assert report.rows == (("Sub-1001", "Active", "2026-08-12T09:30:00+00:00"),)


def test_subscriber_overview_projects_plan_region_and_ticket_counts(
    db_session, subscriber, subscription, catalog_offer
):
    from app.models.catalog import SubscriptionStatus
    from app.models.subscriber import UserType

    subscriber.status = SubscriberStatus.active
    subscriber.user_type = UserType.customer
    subscriber.region = "Abuja"
    subscription.status = SubscriptionStatus.active
    db_session.commit()

    report = web_reports.get_subscribers_report_data(db_session, page=1, per_page=10)

    assert report["plan_distribution"] == {catalog_offer.name: 1}
    assert report["regional_breakdown"][0]["region"] == "Abuja"
    assert report["regional_breakdown"][0]["subscribers"] == 1
    assert report["page"] == 1
    assert report["has_previous"] is False


def test_churn_reasons_come_from_native_subscription_cancellation(
    db_session, subscription
):
    from app.models.catalog import SubscriptionStatus

    subscription.status = SubscriptionStatus.canceled
    subscription.canceled_at = datetime.now(UTC)
    subscription.cancel_reason = "Moved away"
    db_session.commit()

    report = web_reports.get_churn_report_data(db_session)

    assert report.churn_reasons == {"Moved away": 1}


def test_churn_export_uses_complete_cohort_and_strict_active_retention(
    db_session, monkeypatch
):
    subscribers = [
        SimpleNamespace(
            id="active",
            status=SubscriberStatus.active,
            is_active=True,
            category=None,
            company_name=None,
            first_name="Active",
            last_name="Customer",
            display_name=None,
            updated_at=None,
        ),
        SimpleNamespace(
            id="suspended",
            status=SubscriberStatus.suspended,
            is_active=True,
            category=None,
            company_name=None,
            first_name="Suspended",
            last_name="Customer",
            display_name=None,
            updated_at=None,
        ),
        SimpleNamespace(
            id="cancelled",
            status=SubscriberStatus.canceled,
            is_active=False,
            category=None,
            company_name=None,
            first_name="Cancelled",
            last_name="Customer",
            display_name=None,
            updated_at=None,
        ),
    ]
    for subscriber in subscribers:
        subscriber.metadata_ = {}
    calls = 0

    def complete_cohort(_db):
        nonlocal calls
        calls += 1
        return subscribers

    monkeypatch.setattr(web_reports, "_load_report_subscribers", complete_cohort)

    export = web_reports.build_churn_export_csv(db_session)

    assert calls == 1
    assert "retention_rate_percent,33.33" in export


def test_technician_report_uses_completed_appointments_and_period_consistently(
    db_session, subscriber
):
    order = ServiceOrder(
        subscriber_id=subscriber.id,
        status=ServiceOrderStatus.active,
    )
    db_session.add(order)
    db_session.flush()
    now = datetime.now(UTC)
    db_session.add_all(
        [
            InstallAppointment(
                service_order_id=order.id,
                scheduled_start=now - timedelta(days=2),
                scheduled_end=now - timedelta(days=2) + timedelta(hours=1),
                technician="Ada",
                status=AppointmentStatus.completed,
            ),
            InstallAppointment(
                service_order_id=order.id,
                scheduled_start=now - timedelta(days=60),
                scheduled_end=now - timedelta(days=60) + timedelta(hours=1),
                technician="Ada",
                status=AppointmentStatus.completed,
            ),
            InstallAppointment(
                service_order_id=order.id,
                scheduled_start=now - timedelta(days=1),
                scheduled_end=now - timedelta(days=1) + timedelta(hours=1),
                technician="Ada",
                status=AppointmentStatus.proposed,
            ),
        ]
    )
    db_session.commit()

    report = provisioning_managers.technician_report_stats(
        db_session,
        start_at=now - timedelta(days=30),
        end_at=now + timedelta(days=1),
    )

    assert report["jobs_completed"] == 1
    assert report["appointment_completion_rate"] == 50.0
    assert report["technician_stats"][0]["completion_rate"] == 50.0


def test_technician_route_wires_completion_metric_and_date_filters(
    db_session, monkeypatch
):
    import app.web.admin as admin_web

    report_data = {
        "total_technicians": 1,
        "jobs_completed": 2,
        "avg_completion_hours": 3.5,
        "appointment_completion_rate": 50.0,
        "technician_stats": [],
        "job_type_breakdown": {},
        "recent_completions": [],
        "date_from": "2026-08-01",
        "date_to": "2026-08-12",
    }
    monkeypatch.setattr(
        web_reports, "get_technician_report_data", lambda *_args, **_kwargs: report_data
    )
    monkeypatch.setattr(admin_web, "get_current_user", lambda _request: None)
    monkeypatch.setattr(admin_web, "get_sidebar_stats", lambda _db: {})
    monkeypatch.setattr(
        report_routes, "recent_activity_for_paths", lambda *_args, **_kwargs: []
    )
    monkeypatch.setattr(
        report_routes.templates,
        "TemplateResponse",
        lambda _template, context: context,
    )

    context = report_routes.reports_technician(
        SimpleNamespace(),
        date_from="2026-08-01",
        date_to="2026-08-12",
        db=db_session,
    )

    assert context["appointment_completion_rate"] == 50.0
    assert context["date_from"] == "2026-08-01"
    assert context["date_to"] == "2026-08-12"
    assert "first_visit_rate" not in context


def test_operational_route_enforces_the_exact_report_permission():
    request = SimpleNamespace(
        state=SimpleNamespace(auth={"permission_keys": {"reports:support:read"}})
    )

    allowed = report_routes._operational_definition(request, "crm-performance")
    assert allowed.permission == "reports:support:read"

    with pytest.raises(Exception) as exc_info:
        report_routes._operational_definition(request, "subscriber-revenue")
    assert getattr(exc_info.value, "status_code", None) == 403


@pytest.mark.parametrize("report_slug", ["agent-performance", "my-performance"])
def test_agent_performance_page_returns_shell_before_running_analytics(
    db_session, monkeypatch, report_slug
):
    import app.web.admin as admin_web

    user_id = uuid4()
    request = SimpleNamespace(
        state=SimpleNamespace(
            auth={"permission_keys": {"reports:support:read"}},
            user=SimpleNamespace(id=user_id),
        )
    )
    monkeypatch.setattr(admin_web, "get_current_user", lambda _request: None)
    monkeypatch.setattr(admin_web, "get_sidebar_stats", lambda _db: {})
    monkeypatch.setattr(
        crm_reporting,
        "get_report",
        lambda *_args, **_kwargs: pytest.fail(
            "initial lazy shell must not run the analytics query"
        ),
    )
    monkeypatch.setattr(
        report_routes.templates,
        "TemplateResponse",
        lambda _template, context: context,
    )

    context = report_routes.reports_operational_page(
        request=request,
        report_slug=report_slug,
        range_value="custom",
        date_from="2026-08-01",
        date_to="2026-08-28",
        search="Ada",
        page=1,
        per_page=50,
        db=db_session,
    )

    assert context["report"] is None
    assert context["lazy_load"] is True
    assert f"/{report_slug}/data?" in context["lazy_data_url"]
    assert "date_from=2026-08-01" in context["lazy_data_url"]
    if report_slug == "my-performance":
        assert "search=" not in context["lazy_data_url"]


def test_agent_performance_data_endpoint_fetches_one_filtered_page(
    db_session, monkeypatch
):
    user_id = uuid4()
    request = SimpleNamespace(
        state=SimpleNamespace(
            auth={"permission_keys": {"reports:support:read"}},
            user=SimpleNamespace(id=user_id),
        )
    )
    captured = []

    def get_report(_db, *, slug, query):
        captured.append((slug, query))
        return crm_reporting.CrmReportPage(
            definition=crm_reporting.REPORT_DEFINITIONS[slug],
            metrics=(),
            columns=("Agent",),
            rows=(),
            total=0,
            page=query.page,
            per_page=query.per_page or 1,
        )

    monkeypatch.setattr(crm_reporting, "get_report", get_report)
    monkeypatch.setattr(
        report_routes.templates,
        "TemplateResponse",
        lambda _template, context, **kwargs: {**context, **kwargs},
    )

    context = report_routes.reports_operational_agent_data(
        request=request,
        report_slug="agent-performance",
        range_value="custom",
        date_from="2026-08-01",
        date_to="2026-08-28",
        search="Ada Agent",
        page=2,
        per_page=25,
        db=db_session,
    )

    slug, query = captured[0]
    assert slug is crm_reporting.CrmReportSlug.AGENT_PERFORMANCE
    assert query.date_from == date(2026, 8, 1)
    assert query.date_to == date(2026, 8, 28)
    assert query.page == 2
    assert query.per_page == 25
    assert query.search == "Ada Agent"
    assert "page=1" in context["previous_data_url"]
    assert context["headers"] == {"Cache-Control": "private, no-store"}


def test_agent_performance_data_endpoint_marks_database_failure_unavailable(
    db_session, monkeypatch
):
    request = SimpleNamespace(
        state=SimpleNamespace(
            auth={"permission_keys": {"reports:support:read"}},
            user=SimpleNamespace(id=uuid4()),
        )
    )
    released = []

    def unavailable(*_args, **_kwargs):
        raise report_routes.SQLAlchemyError("database unavailable")

    monkeypatch.setattr(
        crm_reporting,
        "get_report",
        unavailable,
    )
    monkeypatch.setattr(
        report_routes.db_session_adapter,
        "release_read_transaction",
        lambda db: released.append(db),
    )
    monkeypatch.setattr(
        report_routes.templates,
        "TemplateResponse",
        lambda _template, context, **kwargs: {**context, **kwargs},
    )

    context = report_routes.reports_operational_agent_data(
        request=request,
        report_slug="agent-performance",
        range_value="month",
        date_from=None,
        date_to=None,
        search=None,
        page=1,
        per_page=50,
        db=db_session,
    )

    assert context["report"] is None
    assert "temporarily unavailable" in context["report_error"]
    assert "estimated" in context["report_error"]
    assert released == [db_session]
    assert context["headers"] == {"Cache-Control": "private, no-store"}


def test_operational_report_template_compiles():
    template = report_routes.templates.env.get_template(
        "admin/reports/operational.html"
    )
    assert template is not None
    assert report_routes.templates.env.get_template(
        "admin/reports/_agent_performance_results.html"
    )


def test_inbox_performance_agent_load_template_uses_display_name_not_uuid():
    source = Path("templates/admin/reports/inbox_performance.html").read_text(
        encoding="utf-8"
    )

    assert "{{ row.agent_name }}" in source
    assert "{{ row.person_id }}" not in source


def test_agent_performance_partial_renders_display_name_and_lazy_pagination():
    agent_id = uuid4()
    report = crm_reporting.CrmReportPage(
        definition=crm_reporting.REPORT_DEFINITIONS[
            crm_reporting.CrmReportSlug.AGENT_PERFORMANCE
        ],
        metrics=(crm_reporting.CrmReportMetric("Chats assigned", "12"),),
        columns=("Agent", "Team", "Assigned"),
        rows=(("Ada Agent", "Customer Experience", "12"),),
        total=2,
        page=1,
        per_page=1,
        note="Current month in Africa/Lagos.",
    )

    rendered = report_routes.templates.env.get_template(
        "admin/reports/_agent_performance_results.html"
    ).render(
        report=report,
        report_error=None,
        retry_url="/admin/reports/operational/agent-performance/data?page=1",
        previous_data_url=None,
        next_data_url="/admin/reports/operational/agent-performance/data?page=2",
        previous_page_url=None,
        next_page_url="/admin/reports/operational/agent-performance?page=2",
    )

    assert "Ada Agent" in rendered
    assert str(agent_id) not in rendered
    assert "Chats assigned" in rendered
    assert (
        'hx-get="/admin/reports/operational/agent-performance/data?page=2"' in rendered
    )
    assert (
        'hx-push-url="/admin/reports/operational/agent-performance?page=2"' in rendered
    )


def test_sales_report_columns_bind_labels_to_exact_row_keys():
    context: report_routes.SalesReportContext = {
        "report_kind": "leads",
        "title": "Lead Performance",
        "description": "Lead KPIs",
        "columns": (
            {"label": "Agent", "key": "agent_name"},
            {"label": "Leads won", "key": "leads_won"},
        ),
        "rows": [{"agent_name": "Ada Agent", "leads_won": 3}],
        "metrics": (),
        "date_from": "",
        "date_to": "",
        "note": "",
    }
    html = report_routes.templates.env.get_template(
        "admin/reports/_sales_kpi_table.html"
    ).render(columns=context["columns"], rows=context["rows"])

    assert "Agent" in html
    assert "Ada Agent" in html
    assert "Leads won" in html
    assert ">3<" in html
    assert report_routes._sales_report_csv(context).splitlines() == [
        "Agent,Leads won",
        "Ada Agent,3",
    ]


def test_lead_performance_rows_paginate_twenty_at_a_time():
    rows: list[report_routes.SalesReportRow] = [
        {"agent_name": f"Agent {index}", "leads_won": index} for index in range(45)
    ]

    first = report_routes._paginate_sales_report_rows(rows, page=1)
    third = report_routes._paginate_sales_report_rows(rows, page=3)

    assert len(first.rows) == 20
    assert first.total_count == 45
    assert first.total_pages == 3
    assert first.has_next
    assert len(third.rows) == 5
    assert third.has_previous
    assert not third.has_next


def test_sales_performance_template_has_bottom_right_page_controls():
    source, _, _ = report_routes.templates.env.loader.get_source(
        report_routes.templates.env, "admin/reports/sales_kpi.html"
    )

    assert 'report_kind == "leads"' not in source
    assert 'aria-label="{{ title }} pages"' in source
    assert "page={{ page + 1 }}" in source
    assert "page={{ page - 1 }}" in source


def test_sales_order_performance_route_uses_twenty_row_pagination():
    source = Path("app/web/admin/reports.py").read_text(encoding="utf-8")
    route = source[source.index("def sales_order_performance_report(") :]
    route = route[: route.index("@router.get", 1)]

    assert "page: int = Query(default=1, ge=1)" in route
    assert '_paginate_sales_report_rows(context["rows"], page=page)' in route
    assert '"rows": report_page.rows' in route


def test_sales_order_report_columns_render_exact_prefixed_row_keys():
    columns: tuple[report_routes.SalesReportColumn, ...] = (
        {"label": "Agent", "key": "agent_name"},
        {"label": "Confirmed", "key": "orders_confirmed"},
        {"label": "Paid", "key": "orders_paid"},
        {"label": "Fulfilled", "key": "orders_fulfilled"},
        {"label": "Cancelled", "key": "orders_cancelled"},
    )
    rows: list[report_routes.SalesReportRow] = [
        {
            "agent_name": "Tunde Agent",
            "orders_confirmed": 4,
            "orders_paid": 3,
            "orders_fulfilled": 2,
            "orders_cancelled": 1,
        }
    ]

    html = report_routes.templates.env.get_template(
        "admin/reports/_sales_kpi_table.html"
    ).render(columns=columns, rows=rows)

    for expected in ("Tunde Agent", "Confirmed", "Paid", "Fulfilled", "Cancelled"):
        assert expected in html
    for expected in (">4<", ">3<", ">2<", ">1<"):
        assert expected in html


def test_crm_report_projection_is_registered_with_a_valid_contract():
    assert registry_validation_errors() == ()
