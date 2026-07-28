"""Required owner-output identities fail delivery instead of logging success."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from app.services.events.handlers.billing_lifecycle_projection import (
    BillingLifecycleProjectionHandler,
)
from app.services.events.handlers.identity_lifecycle_projection import (
    IdentityLifecycleProjectionHandler,
)
from app.services.events.handlers.materials_lifecycle_projection import (
    MaterialsLifecycleProjectionHandler,
)
from app.services.events.handlers.outage_lifecycle_projection import (
    OutageLifecycleProjectionHandler,
)
from app.services.events.handlers.sales_lifecycle_projection import (
    SalesLifecycleProjectionHandler,
)
from app.services.events.handlers.support_lifecycle_projection import (
    SupportLifecycleProjectionHandler,
)
from app.services.events.owner_outputs import OwnerOutputError
from app.services.events.types import Event, EventType

HandlerCall = Callable[[object, Event], None]


@pytest.mark.parametrize(
    ("handler", "event"),
    (
        (
            SalesLifecycleProjectionHandler().handle,
            Event(EventType.sales_order_funding_satisfied, {}),
        ),
        (
            SalesLifecycleProjectionHandler().handle,
            Event(EventType.vendor_project_verified, {}),
        ),
        (
            SalesLifecycleProjectionHandler().handle,
            Event(EventType.service_order_released, {"sales_order_id": "sale"}),
        ),
        (
            SalesLifecycleProjectionHandler().handle,
            Event(EventType.service_order_completed, {"sales_order_id": "sale"}),
        ),
        (
            SalesLifecycleProjectionHandler().handle,
            Event(EventType.customer_experience_accepted, {"sales_order_id": "sale"}),
        ),
        (
            SalesLifecycleProjectionHandler().handle,
            Event(EventType.custom, {"trigger": "sales.cx_acceptance_due"}),
        ),
        (
            OutageLifecycleProjectionHandler().handle,
            Event(EventType.outage_created, {}),
        ),
        (
            MaterialsLifecycleProjectionHandler().handle,
            Event(EventType.field_material_request_approved, {}),
        ),
        (
            MaterialsLifecycleProjectionHandler().handle,
            Event(EventType.vendor_purchase_invoice_approved, {}),
        ),
        (
            SupportLifecycleProjectionHandler().handle,
            Event(
                EventType.work_order_field_outcome_recorded,
                {"origin_ticket_id": "ticket"},
            ),
        ),
        (
            SupportLifecycleProjectionHandler().handle,
            Event(
                EventType.custom,
                {"trigger": "support.resolution_confirmation_due"},
            ),
        ),
        (
            SupportLifecycleProjectionHandler().handle,
            Event(EventType.custom, {"trigger": "team_inbox.snooze_wake"}),
        ),
        (
            SupportLifecycleProjectionHandler().handle,
            Event(EventType.custom, {"trigger": "support.ticket_sla_breach_due"}),
        ),
        (
            IdentityLifecycleProjectionHandler().handle,
            Event(EventType.custom, {"trigger": "auth.access_invitation_expiry_due"}),
        ),
    ),
)
def test_missing_required_identity_fails_delivery(
    handler: HandlerCall,
    event: Event,
) -> None:
    with pytest.raises(OwnerOutputError) as raised:
        handler(None, event)  # type: ignore[arg-type]

    assert raised.value.code == "events.owner_outputs.missing_required_payload"
    assert raised.value.details["event_id"] == str(event.event_id)
    assert raised.value.details["field"]


def test_field_outcome_without_origin_ticket_has_no_support_consumer() -> None:
    SupportLifecycleProjectionHandler().handle(
        None,  # type: ignore[arg-type]
        Event(EventType.work_order_field_outcome_recorded, {}),
    )


def test_billing_output_requires_matching_versioned_envelope() -> None:
    event = Event(
        EventType.custom,
        {
            "output": "sales.fulfillment.funding_applied",
            "sales_order_id": "d1185157-ab84-42c4-a270-06613760fd64",
            "contracts": [],
        },
    )

    with pytest.raises(OwnerOutputError) as raised:
        BillingLifecycleProjectionHandler().handle(
            None,  # type: ignore[arg-type]
            event,
        )

    assert raised.value.code == "events.owner_outputs.invalid_required_payload"
    assert raised.value.details["field"] == "envelope"


@pytest.mark.parametrize(
    "handler",
    (
        SalesLifecycleProjectionHandler().handle,
        SupportLifecycleProjectionHandler().handle,
        IdentityLifecycleProjectionHandler().handle,
    ),
)
def test_unrecognized_custom_event_is_ignored(handler: HandlerCall) -> None:
    handler(None, Event(EventType.custom, {"trigger": "another.owner"}))  # type: ignore[arg-type]
