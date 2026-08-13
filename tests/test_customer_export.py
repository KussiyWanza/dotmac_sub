from __future__ import annotations

import csv
import io

import pytest

from app.models.catalog import NasDevice, SubscriptionStatus
from app.models.subscriber import UserType
from app.services import web_customer_lists


def _export_query(*, ids: str = "all") -> web_customer_lists.CustomerExportQuery:
    return web_customer_lists.build_customer_export_query(
        ids=ids,
        search=None,
        status=None,
        customer_type=None,
        nas_id=None,
        pop_site_id=None,
        infrastructure_type=None,
        infrastructure_id=None,
        sort_by="created_at",
        sort_dir="desc",
    )


def test_complete_customer_csv_projects_advanced_analytical_fields(
    db_session,
    subscriber,
    subscription,
    pop_site,
):
    subscriber.user_type = UserType.customer
    subscriber.first_name = "=SUM(1,1)"
    subscriber.phone = "+2348000000000"
    subscriber.account_number = "ACC-1001"
    subscriber.subscriber_number = "SUB-1001"
    subscriber.pop_site_id = pop_site.id

    nas = NasDevice(name="Core NAS", code="CSV-CORE-NAS", pop_site_id=pop_site.id)
    db_session.add(nas)
    db_session.flush()
    subscription.status = SubscriptionStatus.active
    subscription.login = "customer.pppoe"
    subscription.ipv4_address = "198.51.100.10"
    subscription.provisioning_nas_device_id = nas.id
    db_session.commit()

    exported = web_customer_lists.build_customer_csv_export(
        db_session,
        export_query=_export_query(),
    )
    rows = list(csv.DictReader(io.StringIO(exported.content)))

    row = next(item for item in rows if item["id"] == str(subscriber.id))
    assert tuple(row) == web_customer_lists.CUSTOMER_EXPORT_HEADERS
    assert row["name"].startswith("'=")
    assert row["account_number"] == "ACC-1001"
    assert row["subscriber_number"] == "SUB-1001"
    assert row["subscription_plans"] == "Standard Internet"
    assert row["service_statuses"] == "active"
    assert row["pppoe_usernames"] == "customer.pppoe"
    assert row["service_ip_addresses"] == "198.51.100.10"
    assert row["nas_devices"] == "Core NAS"
    assert row["locations"] == "Test POP"
    assert row["contact_completeness"] == "Email and phone"
    assert exported.filename.startswith("customers_export_")
    assert exported.filename.endswith(".csv")


def test_selected_customer_export_preserves_requested_target_scope(
    db_session,
    subscriber,
):
    subscriber.user_type = UserType.customer
    db_session.commit()

    exported = web_customer_lists.build_customer_csv_export(
        db_session,
        export_query=_export_query(ids=f"person:{subscriber.id}"),
    )

    rows = list(csv.DictReader(io.StringIO(exported.content)))
    assert [row["id"] for row in rows] == [str(subscriber.id)]


@pytest.mark.parametrize(
    ("ids", "expected_code"),
    (
        ("", web_customer_lists.CustomerExportErrorCode.EMPTY_TARGET),
        (
            "customer:not-a-uuid",
            web_customer_lists.CustomerExportErrorCode.INVALID_TARGET,
        ),
        (
            "person:not-a-uuid",
            web_customer_lists.CustomerExportErrorCode.INVALID_TARGET,
        ),
    ),
)
def test_customer_export_rejects_invalid_selected_targets(ids, expected_code):
    with pytest.raises(web_customer_lists.CustomerExportQueryError) as exc_info:
        _export_query(ids=ids)

    assert exc_info.value.code is expected_code
