from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.models.billing import Invoice, Payment, PaymentStatus
from app.models.catalog import NasDevice, SubscriptionStatus
from app.models.subscriber import Subscriber, SubscriberStatus, UserType
from app.models.support import Ticket, TicketStatus
from app.services import web_customer_lists


def _export_query(
    *,
    ids: str = "all",
    search: str | None = None,
    status: str | None = None,
    billing_mode: str | None = None,
) -> web_customer_lists.CustomerExportQuery:
    return web_customer_lists.build_customer_export_query(
        ids=ids,
        search=search,
        status=status,
        customer_type=None,
        nas_id=None,
        pop_site_id=None,
        infrastructure_type=None,
        infrastructure_id=None,
        billing_mode=billing_mode,
        sort_by="created_at",
        sort_dir="desc",
    )


def test_customer_export_preserves_billing_filter_in_canonical_scope():
    export_query = _export_query(billing_mode="non_billable")

    assert export_query.list_query.filter_value("billing_mode") == "non_billable"


def test_customer_csv_export_uses_current_filtered_scope(db_session):
    token = "export-filtered-scope"
    active_customer = Subscriber(
        first_name="Active",
        last_name="Filtered",
        email=f"active-{token}@example.com",
        user_type=UserType.customer,
        status=SubscriberStatus.active,
        is_active=True,
    )
    suspended_customer = Subscriber(
        first_name="Suspended",
        last_name="Filtered",
        email=f"suspended-{token}@example.com",
        user_type=UserType.customer,
        status=SubscriberStatus.suspended,
        is_active=False,
    )
    db_session.add_all([active_customer, suspended_customer])
    db_session.commit()

    exported = web_customer_lists.build_customer_csv_export(
        db_session,
        export_query=_export_query(search=token, status="active"),
    )
    rows = list(csv.DictReader(io.StringIO(exported.content)))

    assert [row["id"] for row in rows] == [str(active_customer.id)]


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
    assert row["open_ticket_ids"] == ""
    assert row["total_payment"] == "0.00"
    assert row["last_billing_date"] == ""
    assert exported.filename.startswith("customers_export_")
    assert exported.filename.endswith(".csv")


def test_customer_csv_projects_open_tickets_payments_and_last_billing_date(
    db_session,
    subscriber,
):
    subscriber.user_type = UserType.customer
    open_ticket = Ticket(
        number="TKT-EXPORT-200",
        title="Open export ticket",
        status=TicketStatus.open.value,
        subscriber_id=subscriber.id,
        customer_account_id=subscriber.id,
        is_active=True,
    )
    pending_ticket = Ticket(
        number="TKT-EXPORT-100",
        title="Pending export ticket",
        status=TicketStatus.pending.value,
        customer_person_id=subscriber.id,
        is_active=True,
    )
    closed_ticket = Ticket(
        number="TKT-EXPORT-CLOSED",
        title="Closed export ticket",
        status=TicketStatus.closed.value,
        subscriber_id=subscriber.id,
        is_active=True,
    )
    payments = [
        Payment(
            account_id=subscriber.id,
            amount=Decimal("1250.50"),
            status=PaymentStatus.succeeded,
            is_active=True,
        ),
        Payment(
            account_id=subscriber.id,
            amount=Decimal("249.50"),
            status=PaymentStatus.succeeded,
            is_active=True,
        ),
        Payment(
            account_id=subscriber.id,
            amount=Decimal("999.00"),
            status=PaymentStatus.failed,
            is_active=True,
        ),
        Payment(
            account_id=subscriber.id,
            amount=Decimal("500.00"),
            status=PaymentStatus.succeeded,
            is_active=False,
        ),
    ]
    invoices = [
        Invoice(
            account_id=subscriber.id,
            issued_at=datetime(2026, 1, 2, 8, 0, tzinfo=UTC),
            is_active=True,
        ),
        Invoice(
            account_id=subscriber.id,
            issued_at=datetime(2026, 2, 3, 8, 0, tzinfo=UTC),
            is_active=True,
        ),
        Invoice(
            account_id=subscriber.id,
            issued_at=datetime(2026, 3, 4, 8, 0, tzinfo=UTC),
            is_active=False,
        ),
    ]
    db_session.add_all(
        [open_ticket, pending_ticket, closed_ticket, *payments, *invoices]
    )
    db_session.commit()

    exported = web_customer_lists.build_customer_csv_export(
        db_session,
        export_query=_export_query(ids=f"person:{subscriber.id}"),
    )

    row = next(csv.DictReader(io.StringIO(exported.content)))
    assert row["open_ticket_ids"] == "TKT-EXPORT-100 | TKT-EXPORT-200"
    assert row["total_payment"] == "1500.00"
    assert row["last_billing_date"] == "2026-02-03"


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
