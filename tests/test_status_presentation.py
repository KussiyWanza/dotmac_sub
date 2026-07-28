from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from app.models.billing import CreditNoteStatus, InvoiceStatus, PaymentStatus
from app.models.catalog import SubscriptionStatus
from app.models.field_expense import FIELD_EXPENSE_STATUSES
from app.models.field_material import FIELD_MATERIAL_REQUEST_STATUSES
from app.models.network_monitoring import DeviceRole, DeviceStatus, NetworkDevice
from app.models.payment_proof import WithholdingTaxStatus
from app.models.subscriber import SubscriberStatus
from app.models.support import Ticket, TicketStatus
from app.models.vendor_routes import VendorPurchaseInvoiceStatus
from app.models.vendor_supply import VendorAdvanceStatus, VendorMaterialReleaseStatus
from app.schemas.billing import InvoiceRead, PaymentRead
from app.schemas.catalog import SubscriptionRead
from app.schemas.network_monitoring import NetworkDeviceRead
from app.schemas.status_presentation import StatusIcon, StatusTone
from app.schemas.support import TicketRead
from app.services.device_operational_status import (
    DeviceOperationalState,
    annotate_operational_status,
)
from app.services.field.work_order_status import WorkOrderStatus
from app.services.status_presentation import (
    access_session_status_presentation,
    account_status_presentation,
    connection_health_status_presentation,
    credit_note_status_presentation,
    device_operational_status_presentation,
    field_expense_status_presentation,
    field_material_request_status_presentation,
    invoice_status_presentation,
    outage_status_presentation,
    payment_status_presentation,
    service_access_status_presentation,
    subscription_status_presentation,
    supplier_invoice_status_presentation,
    system_job_status_presentation,
    ticket_status_presentation,
    vendor_advance_status_presentation,
    vendor_material_release_status_presentation,
    vendor_purchase_invoice_status_presentation,
    withholding_tax_status_presentation,
    work_order_status_presentation,
)
from app.services.topology.connection_status import ConnectionHealthState
from app.services.topology.outage import OutageStatus


@pytest.mark.parametrize("status", ["available", "restricted", "unavailable"])
def test_service_access_presentation_covers_projection_states(status: str) -> None:
    presentation = service_access_status_presentation(status)

    assert presentation.value == status
    assert presentation.label
    assert presentation.tone in StatusTone
    assert presentation.icon in StatusIcon


@pytest.mark.parametrize("status", list(WithholdingTaxStatus))
def test_wht_presentation_covers_authoritative_enum(
    status: WithholdingTaxStatus,
) -> None:
    presentation = withholding_tax_status_presentation(status)

    assert presentation.value == status.value
    assert presentation.tone in StatusTone
    assert presentation.icon in StatusIcon


@pytest.mark.parametrize("status", list(CreditNoteStatus))
def test_credit_note_presentation_covers_authoritative_enum(
    status: CreditNoteStatus,
) -> None:
    presentation = credit_note_status_presentation(status)

    assert presentation.value == status.value
    assert presentation.label
    assert presentation.tone in StatusTone
    assert presentation.icon in StatusIcon


@pytest.mark.parametrize("status", list(VendorPurchaseInvoiceStatus))
def test_vendor_purchase_invoice_presentation_covers_authoritative_enum(
    status: VendorPurchaseInvoiceStatus,
) -> None:
    presentation = vendor_purchase_invoice_status_presentation(status)

    assert presentation.value == status.value
    assert presentation.label
    assert presentation.tone in StatusTone
    assert presentation.icon in StatusIcon


@pytest.mark.parametrize("status", list(VendorMaterialReleaseStatus))
def test_vendor_material_release_presentation_covers_authoritative_enum(
    status: VendorMaterialReleaseStatus,
) -> None:
    presentation = vendor_material_release_status_presentation(status)

    assert presentation.value == status.value
    assert presentation.label
    assert presentation.tone in StatusTone
    assert presentation.icon in StatusIcon


@pytest.mark.parametrize("status", list(VendorAdvanceStatus))
def test_vendor_advance_presentation_covers_authoritative_enum(
    status: VendorAdvanceStatus,
) -> None:
    presentation = vendor_advance_status_presentation(status)

    assert presentation.value == status.value
    assert presentation.label
    assert presentation.tone in StatusTone
    assert presentation.icon in StatusIcon


@pytest.mark.parametrize(
    "status",
    [
        "draft",
        "submitted",
        "pending_approval",
        "approved",
        "posted",
        "partially_paid",
        "paid",
        "on_hold",
        "rejected",
        "void",
        "disputed",
    ],
)
def test_erp_supplier_invoice_presentation_covers_authoritative_statuses(
    status: str,
) -> None:
    presentation = supplier_invoice_status_presentation(status)

    assert presentation.value == status
    assert presentation.label
    assert presentation.tone in StatusTone
    assert presentation.icon in StatusIcon


@pytest.mark.parametrize("status", list(FIELD_EXPENSE_STATUSES))
def test_field_expense_presentation_covers_authoritative_statuses(
    status: str,
) -> None:
    presentation = field_expense_status_presentation(status)

    assert presentation.value == status
    assert presentation.label
    assert presentation.tone in StatusTone
    assert presentation.icon in StatusIcon


@pytest.mark.parametrize("status", list(FIELD_MATERIAL_REQUEST_STATUSES))
def test_field_material_request_presentation_covers_authoritative_statuses(
    status: str,
) -> None:
    presentation = field_material_request_status_presentation(status)

    assert presentation.value == status
    assert presentation.label
    assert presentation.tone in StatusTone
    assert presentation.icon in StatusIcon


@pytest.mark.parametrize(
    "status", ["queued", "running", "completed", "failed", "canceled"]
)
def test_system_job_presentation_covers_lifecycle(status: str) -> None:
    presentation = system_job_status_presentation(status)

    assert presentation.value == status
    assert presentation.label
    assert presentation.tone in StatusTone
    assert presentation.icon in StatusIcon


@pytest.mark.parametrize(
    ("status", "label", "tone", "icon"),
    [
        (
            DeviceOperationalState.working,
            "Working",
            StatusTone.positive,
            StatusIcon.check,
        ),
        (
            DeviceOperationalState.not_working,
            "Not working",
            StatusTone.negative,
            StatusIcon.x,
        ),
    ],
)
def test_device_operational_presentation_covers_authoritative_vocabulary(
    status: DeviceOperationalState,
    label: str,
    tone: StatusTone,
    icon: StatusIcon,
) -> None:
    presentation = device_operational_status_presentation(status)

    assert presentation.value == status.value
    assert presentation.label == label
    assert presentation.tone == tone
    assert presentation.icon == icon


@pytest.mark.parametrize(
    ("status", "label", "tone", "icon"),
    [
        (
            ConnectionHealthState.connected,
            "Connected",
            StatusTone.positive,
            StatusIcon.check,
        ),
        (
            ConnectionHealthState.trouble,
            "Connection issue",
            StatusTone.warning,
            StatusIcon.alert,
        ),
        (
            ConnectionHealthState.outage,
            "Area outage",
            StatusTone.negative,
            StatusIcon.alert,
        ),
    ],
)
def test_connection_health_presentation_covers_authoritative_vocabulary(
    status: ConnectionHealthState,
    label: str,
    tone: StatusTone,
    icon: StatusIcon,
) -> None:
    presentation = connection_health_status_presentation(status)

    assert presentation.value == status.value
    assert presentation.label == label
    assert presentation.tone == tone
    assert presentation.icon == icon


@pytest.mark.parametrize(
    ("status", "label", "tone", "icon"),
    [
        ("connected", "Connected", StatusTone.positive, StatusIcon.check),
        ("stale", "Last seen", StatusTone.warning, StatusIcon.clock),
        ("offline", "Not connected", StatusTone.neutral, StatusIcon.x),
        ("inactive", "Not connected", StatusTone.neutral, StatusIcon.minus),
    ],
)
def test_access_session_presentation_covers_admin_observation_vocabulary(
    status: str,
    label: str,
    tone: StatusTone,
    icon: StatusIcon,
) -> None:
    presentation = access_session_status_presentation(status)

    assert presentation.value == status
    assert presentation.label == label
    assert presentation.tone == tone
    assert presentation.icon == icon


def test_network_device_read_serializes_operational_presentation() -> None:
    now = datetime.now(UTC)
    device = NetworkDevice(
        id=uuid4(),
        name="Core Router",
        role=DeviceRole.edge,
        status=DeviceStatus.online,
        live_status="up",
        live_status_at=now,
        ping_enabled=True,
        snmp_enabled=False,
        send_notifications=True,
        notification_delay_minutes=0,
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    annotate_operational_status([device], now=now)

    payload = NetworkDeviceRead.model_validate(device).model_dump(mode="json")

    assert payload["operational_status"] == "working"
    assert payload["operational_reason"] == "observed_working"
    assert "operational_retry_pending" not in payload
    assert payload["status_presentation"] == {
        "value": "working",
        "label": "Working",
        "tone": "positive",
        "icon": "check",
    }


@pytest.mark.parametrize(
    ("status", "label", "tone", "icon"),
    [
        (SubscriberStatus.new, "New", StatusTone.info, StatusIcon.clock),
        (SubscriberStatus.active, "Active", StatusTone.positive, StatusIcon.check),
        (
            SubscriberStatus.delinquent,
            "Delinquent",
            StatusTone.warning,
            StatusIcon.alert,
        ),
        (
            SubscriberStatus.suspended,
            "Suspended",
            StatusTone.warning,
            StatusIcon.alert,
        ),
        (SubscriberStatus.blocked, "Blocked", StatusTone.negative, StatusIcon.x),
        (SubscriberStatus.disabled, "Disabled", StatusTone.negative, StatusIcon.x),
        (SubscriberStatus.canceled, "Canceled", StatusTone.negative, StatusIcon.x),
    ],
)
def test_account_status_presentation_covers_authoritative_enum(
    status: SubscriberStatus,
    label: str,
    tone: StatusTone,
    icon: StatusIcon,
) -> None:
    presentation = account_status_presentation(status)

    assert presentation.value == status.value
    assert presentation.label == label
    assert presentation.tone == tone
    assert presentation.icon == icon


@pytest.mark.parametrize("status", list(SubscriptionStatus))
def test_subscription_status_presentation_covers_authoritative_enum(
    status: SubscriptionStatus,
) -> None:
    presentation = subscription_status_presentation(status)

    assert presentation.value == status.value
    assert presentation.label
    assert presentation.tone in StatusTone
    assert presentation.icon in StatusIcon
    assert presentation.icon != StatusIcon.info


@pytest.mark.parametrize("status", list(WorkOrderStatus))
def test_work_order_status_presentation_covers_authoritative_enum(
    status: WorkOrderStatus,
) -> None:
    presentation = work_order_status_presentation(status)

    assert presentation.value == status.value
    assert presentation.label
    assert presentation.tone in StatusTone
    assert presentation.icon in StatusIcon
    assert not (
        presentation.tone == StatusTone.neutral and presentation.icon == StatusIcon.info
    )


def test_work_order_legacy_spelling_is_explicit_and_unknowns_are_neutral() -> None:
    cancelled = work_order_status_presentation("cancelled")
    unknown = work_order_status_presentation("awaiting_parts")

    assert cancelled.model_dump(mode="json") == {
        "value": "cancelled",
        "label": "Canceled",
        "tone": "negative",
        "icon": "x",
    }
    assert unknown.model_dump(mode="json") == {
        "value": "awaiting_parts",
        "label": "Awaiting Parts",
        "tone": "neutral",
        "icon": "info",
    }


@pytest.mark.parametrize("status", list(TicketStatus))
def test_ticket_status_presentation_covers_authoritative_enum(
    status: TicketStatus,
) -> None:
    presentation = ticket_status_presentation(status)

    assert presentation.value == status.value
    assert presentation.label
    assert presentation.tone in StatusTone
    assert presentation.icon in StatusIcon
    assert not (
        presentation.tone == StatusTone.neutral and presentation.icon == StatusIcon.info
    )


def test_ticket_read_serializes_status_presentation_for_api_clients() -> None:
    now = datetime.now(UTC)
    ticket = Ticket(
        id=uuid4(),
        title="Slow browsing",
        status=TicketStatus.waiting_on_customer.value,
        priority="normal",
        channel="web",
        metadata_={},
        is_active=True,
        created_at=now,
        updated_at=now,
    )

    payload = TicketRead.model_validate(ticket).model_dump(mode="json")

    assert payload["status_presentation"] == {
        "value": "waiting_on_customer",
        "label": "Waiting on customer",
        "tone": "warning",
        "icon": "clock",
    }


@pytest.mark.parametrize("status", list(InvoiceStatus))
def test_invoice_status_presentation_covers_authoritative_enum(
    status: InvoiceStatus,
) -> None:
    presentation = invoice_status_presentation(status)

    assert presentation.value == status.value
    assert presentation.label
    assert presentation.tone in StatusTone
    assert presentation.icon in StatusIcon
    assert not (
        presentation.tone == StatusTone.neutral and presentation.icon == StatusIcon.info
    )


def test_invoice_read_serializes_status_presentation_for_api_clients() -> None:
    now = datetime.now(UTC)
    invoice = InvoiceRead(
        id=uuid4(),
        account_id=uuid4(),
        status=InvoiceStatus.written_off,
        currency="NGN",
        subtotal=Decimal("100.00"),
        tax_total=Decimal("0.00"),
        total=Decimal("100.00"),
        balance_due=Decimal("0.00"),
        created_at=now,
        updated_at=now,
    )

    assert invoice.model_dump(mode="json")["status_presentation"] == {
        "value": "written_off",
        "label": "Written off",
        "tone": "negative",
        "icon": "archive",
    }


@pytest.mark.parametrize(
    ("status", "label", "tone", "icon"),
    [
        (PaymentStatus.pending, "Pending", StatusTone.warning, StatusIcon.clock),
        (
            PaymentStatus.succeeded,
            "Succeeded",
            StatusTone.positive,
            StatusIcon.check,
        ),
        (PaymentStatus.failed, "Failed", StatusTone.negative, StatusIcon.x),
        (
            PaymentStatus.refunded,
            "Refunded",
            StatusTone.neutral,
            StatusIcon.archive,
        ),
        (
            PaymentStatus.partially_refunded,
            "Partially refunded",
            StatusTone.warning,
            StatusIcon.clock,
        ),
        (PaymentStatus.reversed, "Reversed", StatusTone.negative, StatusIcon.alert),
        (PaymentStatus.canceled, "Canceled", StatusTone.neutral, StatusIcon.x),
    ],
)
def test_payment_status_presentation_covers_authoritative_enum(
    status: PaymentStatus,
    label: str,
    tone: StatusTone,
    icon: StatusIcon,
) -> None:
    presentation = payment_status_presentation(status)

    assert presentation.value == status.value
    assert presentation.label == label
    assert presentation.tone == tone
    assert presentation.icon == icon


def test_payment_read_serializes_status_presentation_for_api_clients() -> None:
    now = datetime.now(UTC)
    payment = PaymentRead(
        id=uuid4(),
        account_id=uuid4(),
        amount=Decimal("100.00"),
        currency="NGN",
        status=PaymentStatus.partially_refunded,
        created_at=now,
        updated_at=now,
    )

    assert payment.model_dump(mode="json")["status_presentation"] == {
        "value": "partially_refunded",
        "label": "Partially refunded",
        "tone": "warning",
        "icon": "clock",
    }


@pytest.mark.parametrize(
    ("status", "label", "tone", "icon"),
    [
        (OutageStatus.open, "Open", StatusTone.negative, StatusIcon.alert),
        (
            OutageStatus.suspected,
            "Suspected",
            StatusTone.warning,
            StatusIcon.clock,
        ),
        (
            OutageStatus.confirmed,
            "Confirmed",
            StatusTone.negative,
            StatusIcon.alert,
        ),
        (
            OutageStatus.clearing,
            "Clearing",
            StatusTone.info,
            StatusIcon.clock,
        ),
        (
            OutageStatus.resolved,
            "Resolved",
            StatusTone.positive,
            StatusIcon.check,
        ),
        (
            OutageStatus.discarded,
            "Discarded",
            StatusTone.neutral,
            StatusIcon.x,
        ),
    ],
)
def test_outage_status_presentation_covers_authoritative_vocabulary(
    status: OutageStatus,
    label: str,
    tone: StatusTone,
    icon: StatusIcon,
) -> None:
    presentation = outage_status_presentation(status)

    assert presentation.value == status.value
    assert presentation.label == label
    assert presentation.tone == tone
    assert presentation.icon == icon


def test_presentation_fallback_is_neutral_and_legacy_inactive_is_explicit() -> None:
    unknown = subscription_status_presentation("future_state")
    inactive = account_status_presentation(None, is_active=False)

    assert unknown.model_dump(mode="json") == {
        "value": "future_state",
        "label": "Future State",
        "tone": "neutral",
        "icon": "info",
    }
    assert inactive.model_dump(mode="json") == {
        "value": "inactive",
        "label": "Inactive",
        "tone": "neutral",
        "icon": "minus",
    }


def test_subscription_read_serializes_status_presentation_for_api_clients() -> None:
    subscription = SubscriptionRead(
        id=uuid4(),
        subscriber_id=uuid4(),
        offer_id=uuid4(),
        status=SubscriptionStatus.suspended,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    assert subscription.model_dump(mode="json", by_alias=True)[
        "status_presentation"
    ] == {
        "value": "suspended",
        "label": "Suspended",
        "tone": "warning",
        "icon": "alert",
    }


# --- Infrastructure service status (dashboard Phase 2) ---


def test_infrastructure_service_status_presentation_tones():
    from app.schemas.status_presentation import StatusTone
    from app.services.status_presentation import (
        infrastructure_service_status_presentation as pres,
    )

    assert pres("up").tone == StatusTone.positive
    assert pres("streaming").tone == StatusTone.positive
    assert pres("degraded").tone == StatusTone.warning
    assert pres("warning").tone == StatusTone.warning
    assert pres("down").tone == StatusTone.negative
    assert pres("failed").tone == StatusTone.negative
    # Unknown / unmapped falls back to neutral, never silently positive.
    assert pres("weird").tone == StatusTone.neutral
    assert pres(None).tone == StatusTone.neutral


# --- Customer-portal workflow presentations (service orders / appointments) ---


def test_service_order_status_presentation_covers_canonical_set():
    from app.models.provisioning import ServiceOrderStatus
    from app.schemas.status_presentation import StatusTone
    from app.services.status_presentation import service_order_status_presentation

    for status in ServiceOrderStatus:
        pres = service_order_status_presentation(status)
        assert pres.value == status.value
        assert pres.label
    assert (
        service_order_status_presentation(ServiceOrderStatus.active).tone
        == StatusTone.positive
    )
    assert (
        service_order_status_presentation(ServiceOrderStatus.failed).tone
        == StatusTone.negative
    )
    # Unknown values fail neutral, never silently positive.
    assert service_order_status_presentation("weird").tone == StatusTone.neutral


def test_provisioning_task_status_presentation_covers_canonical_set():
    from app.models.provisioning import TaskStatus
    from app.schemas.status_presentation import StatusTone
    from app.services.status_presentation import (
        provisioning_task_status_presentation,
    )

    for status in TaskStatus:
        pres = provisioning_task_status_presentation(status)
        assert pres.value == status.value
        assert pres.label
    assert (
        provisioning_task_status_presentation(TaskStatus.completed).tone
        == StatusTone.positive
    )
    assert (
        provisioning_task_status_presentation(TaskStatus.failed).tone
        == StatusTone.negative
    )
    # Unknown values fail neutral, never silently positive.
    assert provisioning_task_status_presentation("weird").tone == StatusTone.neutral
    assert provisioning_task_status_presentation(None).tone == StatusTone.neutral


def test_appointment_status_presentation_covers_canonical_set():
    from app.models.provisioning import AppointmentStatus
    from app.schemas.status_presentation import StatusTone
    from app.services.status_presentation import appointment_status_presentation

    for status in AppointmentStatus:
        pres = appointment_status_presentation(status)
        assert pres.value == status.value
        assert pres.label
    assert (
        appointment_status_presentation(AppointmentStatus.completed).tone
        == StatusTone.positive
    )
    assert (
        appointment_status_presentation(AppointmentStatus.no_show).tone
        == StatusTone.negative
    )
    assert appointment_status_presentation(None).tone == StatusTone.neutral


# --- Catalog / sales / project presentations (presentation completion) ---


def test_new_catalog_sales_project_families_cover_canonical_sets():
    from app.models.catalog import OfferStatus
    from app.models.project import ProjectStatus, ProjectTaskStatus
    from app.models.sales import QuoteStatus, SalesOrderStatus
    from app.schemas.status_presentation import StatusTone
    from app.services.status_presentation import (
        offer_status_presentation,
        project_status_presentation,
        project_task_status_presentation,
        quote_status_presentation,
        sales_order_status_presentation,
    )

    cases = [
        (OfferStatus, offer_status_presentation),
        (QuoteStatus, quote_status_presentation),
        (SalesOrderStatus, sales_order_status_presentation),
        (ProjectStatus, project_status_presentation),
        (ProjectTaskStatus, project_task_status_presentation),
    ]
    for enum_cls, pres in cases:
        for status in enum_cls:
            result = pres(status)
            assert result.value == status.value
            assert result.label
        # Unknown values fail neutral, never silently positive.
        assert pres("weird").tone == StatusTone.neutral
        assert pres(None).tone == StatusTone.neutral

    assert quote_status_presentation(QuoteStatus.rejected).tone == StatusTone.negative
    assert (
        sales_order_status_presentation(SalesOrderStatus.paid).tone
        == StatusTone.positive
    )
    assert (
        project_task_status_presentation(ProjectTaskStatus.blocked).tone
        == StatusTone.warning
    )
