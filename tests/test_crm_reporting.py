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
    assert "Customer Retention" not in hub_names

    service_source = Path("app/services/crm_reporting.py").read_text(encoding="utf-8")
    assert "CustomerRetentionEngagement" not in service_source
    assert "retention notes" not in service_source.lower()


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
