from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models.network import OLTDevice, OntUnit, OnuOnlineStatus
from app.models.provisioning import (
    AppointmentStatus,
    InstallAppointment,
    ServiceOrder,
    ServiceOrderStatus,
)
from app.models.subscriber import SubscriberStatus
from app.services import crm_reporting, provisioning_managers, web_reports
from app.services.sot_registry.registry import registry_validation_errors
from app.web.admin import reports as report_routes


def test_expected_operational_inventory_is_complete_and_exclusions_stay_excluded():
    assert set(crm_reporting.REPORT_DEFINITIONS) == set(crm_reporting.CrmReportSlug)
    assert len(crm_reporting.REPORT_DEFINITIONS) == 13

    hub_names = {
        link["name"]
        for section in report_routes.REPORT_HUB_SECTIONS
        for link in section["links"]
    }
    assert {
        definition.title for definition in crm_reporting.REPORT_DEFINITIONS.values()
    } <= hub_names
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


@pytest.mark.parametrize("slug", list(crm_reporting.CrmReportSlug))
def test_every_operational_report_has_a_typed_empty_state(db_session, slug):
    report = crm_reporting.get_report(
        db_session,
        slug=slug,
        query=crm_reporting.CrmReportQuery(),
    )

    assert report.definition.slug == slug
    assert report.total >= 0
    assert len(report.columns) > 0
    assert crm_reporting.build_csv(report).startswith(report.columns[0])


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

    assert report["total_olts"] == 101
    assert report["active_olts"] == 101
    assert report["total_onts"] == 2
    assert report["connected_onts"] == 1
    assert all(
        isinstance(item, crm_reporting.NetworkOltFacts) for item in report["olts"]
    )
    assert any(item.mgmt_ip == "10.0.0.1" for item in report["olts"])
    assert all(
        isinstance(item, crm_reporting.NetworkOntFacts)
        for item in report["recent_ont_activity"]
    )
    assert sum(item.is_online for item in report["recent_ont_activity"]) == 1


def test_network_route_wires_all_infrastructure_capacity_metrics(
    db_session, monkeypatch
):
    import app.web.admin as admin_web

    report_data = {
        "total_olts": 2,
        "active_olts": 1,
        "total_onts": 8,
        "connected_onts": 7,
        "ip_pool_usage": 25.0,
        "used_ips": 64,
        "total_ips": 256,
        "active_vlans": 4,
        "pon_capacity": 128,
        "pon_utilization": 6.25,
        "total_fiber_strands": 48,
        "available_fiber_strands": 12,
        "total_fdh": 3,
        "splitter_capacity": 64,
        "olts": [],
        "pool_data": [],
        "recent_ont_activity": [],
    }
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
    ):
        assert context[key] == report_data[key]


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

    assert report["churn_reasons"] == {"Moved away": 1}


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


def test_operational_report_template_compiles():
    template = report_routes.templates.env.get_template(
        "admin/reports/operational.html"
    )
    assert template is not None


def test_crm_report_projection_is_registered_with_a_valid_contract():
    assert registry_validation_errors() == ()
