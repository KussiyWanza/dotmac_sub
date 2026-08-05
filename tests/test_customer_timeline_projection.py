"""Customer timeline attribution and evidence projection behavior."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models.audit import AuditActorType, AuditEvent
from app.models.auth import ApiKey
from app.models.billing import Invoice, InvoiceStatus
from app.models.system_user import SystemUser, SystemUserType
from app.services.customer_timeline import (
    CustomerTimelineActorKind,
    CustomerTimelineResult,
    build_customer_timeline,
)


def test_customer_timeline_identifies_staff_system_and_unknown_actors(
    db_session,
    subscriber,
):
    staff = SystemUser(
        first_name="Ada",
        last_name="Operator",
        email="ada.timeline@example.com",
        user_type=SystemUserType.system_user,
        is_active=True,
    )
    api_key = ApiKey(
        label="CRM Integration",
        key_hash="customer-timeline-api-key",
        scopes=[],
        is_active=True,
    )
    db_session.add_all([staff, api_key])
    db_session.flush()
    occurred_at = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    db_session.add_all(
        [
            AuditEvent(
                occurred_at=occurred_at,
                actor_type=AuditActorType.user,
                actor_id=str(staff.id),
                action="status_change",
                entity_type="subscriber_account",
                entity_id=str(subscriber.id),
                is_success=True,
                request_id="request-staff-1",
                metadata_={
                    "changes": {
                        "status": {"from": "new", "to": "active"},
                    }
                },
            ),
            AuditEvent(
                occurred_at=occurred_at - timedelta(minutes=1),
                actor_type=AuditActorType.system,
                actor_id="system:billing-scheduler",
                action="customer.pppoe_password_reveal",
                entity_type="subscriber",
                entity_id=str(subscriber.id),
                is_success=False,
            ),
            AuditEvent(
                occurred_at=occurred_at - timedelta(minutes=2),
                actor_type=AuditActorType.user,
                actor_id=str(subscriber.id),
                action="update",
                entity_type="subscriber",
                entity_id=str(subscriber.id),
                is_success=True,
            ),
            AuditEvent(
                occurred_at=occurred_at - timedelta(minutes=3),
                actor_type=AuditActorType.service,
                actor_id="service:radius-sync",
                action="sync",
                entity_type="subscriber",
                entity_id=str(subscriber.id),
                is_success=True,
            ),
            AuditEvent(
                occurred_at=occurred_at - timedelta(minutes=4),
                actor_type=AuditActorType.api_key,
                actor_id=str(api_key.id),
                action="update",
                entity_type="subscriber",
                entity_id=str(subscriber.id),
                is_success=True,
            ),
            AuditEvent(
                occurred_at=occurred_at - timedelta(minutes=5),
                actor_type=AuditActorType.user,
                actor_id="deleted-user",
                actor_label="Former Operator",
                action="update",
                entity_type="subscriber",
                entity_id=str(subscriber.id),
                is_success=True,
            ),
        ]
    )
    db_session.commit()

    timeline = build_customer_timeline(
        db_session,
        customer_id=str(subscriber.id),
        account_ids=[],
        subscriptions=[],
    )

    items_by_kind = {item["actor_kind"]: item for item in timeline}
    staff_item = items_by_kind[CustomerTimelineActorKind.STAFF]
    assert staff_item["actor_kind"] == CustomerTimelineActorKind.STAFF
    assert staff_item["actor_label"] == "Ada Operator"
    assert staff_item["action_label"] == "Changed status"
    assert staff_item["description"] == "status: new -> active"
    assert {detail["label"] for detail in staff_item["details"]} == {
        "Changes",
        "Request ID",
        "Result",
        "Source",
    }

    system_item = items_by_kind[CustomerTimelineActorKind.SYSTEM]
    assert system_item["actor_label"] == "Billing Scheduler"
    assert system_item["result"] == CustomerTimelineResult.FAILED
    assert system_item["security_sensitive"] is True

    customer_item = items_by_kind[CustomerTimelineActorKind.CUSTOMER]
    assert customer_item["actor_label"] == "Test User"

    service_item = items_by_kind[CustomerTimelineActorKind.SERVICE]
    assert service_item["actor_label"] == "Radius Sync"

    api_key_item = items_by_kind[CustomerTimelineActorKind.API_KEY]
    assert api_key_item["actor_label"] == "CRM Integration"

    unknown_item = items_by_kind[CustomerTimelineActorKind.UNKNOWN]
    assert unknown_item["actor_label"] == "Former Operator"
    assert [item["actor_kind"] for item in timeline] == [
        CustomerTimelineActorKind.STAFF,
        CustomerTimelineActorKind.SYSTEM,
        CustomerTimelineActorKind.CUSTOMER,
        CustomerTimelineActorKind.SERVICE,
        CustomerTimelineActorKind.API_KEY,
        CustomerTimelineActorKind.UNKNOWN,
    ]
    assert len({item["key"] for item in timeline}) == len(timeline)


def test_customer_timeline_does_not_invent_actor_for_record_only_activity(
    db_session,
    subscriber,
):
    invoice = Invoice(
        account_id=subscriber.id,
        invoice_number="INV-TIMELINE-001",
        status=InvoiceStatus.issued,
        currency="NGN",
        subtotal=Decimal("5000.00"),
        total=Decimal("5000.00"),
        balance_due=Decimal("5000.00"),
        issued_at=datetime(2026, 8, 5, 10, 0, tzinfo=UTC),
        is_active=True,
    )
    db_session.add(invoice)
    db_session.commit()

    timeline = build_customer_timeline(
        db_session,
        customer_id=str(subscriber.id),
        account_ids=[subscriber.id],
        subscriptions=[],
    )

    invoice_item = next(item for item in timeline if item["type"] == "invoice")
    assert invoice_item["actor_kind"] == CustomerTimelineActorKind.UNKNOWN
    assert invoice_item["actor_label"] == "Actor not recorded"
    assert invoice_item["result"] == CustomerTimelineResult.RECORDED
    assert invoice_item["details"] == (
        {"label": "Source", "value": "Invoice INV-TIMELINE-001 record"},
        {
            "label": "Attribution",
            "value": "No audit actor is attached to this record activity.",
        },
    )
