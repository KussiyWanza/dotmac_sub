"""Consequences of a committed exact-service served IPv4 repair."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.subscriber import Subscriber
from app.services.events.handlers.ip_assignment_projection import (
    IPAssignmentProjectionHandler,
)
from app.services.events.types import Event, EventType


def test_handler_projects_radius_before_old_ip_only_disconnect(
    db_session,
    catalog_offer,
) -> None:
    subscriber = Subscriber(
        first_name="Projection",
        last_name="Handler",
        email=f"projection-handler-{uuid4().hex[:8]}@example.com",
    )
    db_session.add(subscriber)
    db_session.flush()
    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=SubscriptionStatus.active,
        login=f"projection-handler-{uuid4().hex[:8]}",
        ipv4_address="10.40.0.2",
    )
    db_session.add(subscription)
    db_session.commit()
    order: list[str] = []
    projected = SimpleNamespace(
        require_projected=lambda: order.append("radius") or projected
    )

    def _disconnect(*_args, **kwargs):
        order.append("disconnect")
        assert kwargs["framed_ip_address"] == "10.40.0.1"
        assert kwargs["require_terminal"] is True
        return 1

    event = Event(
        event_type=EventType.ip_assignment_served_projection_repaired,
        payload={
            "schema_version": 1,
            "subscription_id": str(subscription.id),
            "assignment_id": str(uuid4()),
            "previous_address": "10.40.0.1",
            "desired_address": "10.40.0.2",
            "preview_fingerprint": "a" * 64,
        },
        subscription_id=subscription.id,
        subscriber_id=subscriber.id,
    )
    with (
        patch(
            "app.services.radius.reconcile_subscription_connectivity",
            return_value=projected,
        ),
        patch(
            "app.services.enforcement.disconnect_subscription_sessions",
            side_effect=_disconnect,
        ),
    ):
        IPAssignmentProjectionHandler().handle(db_session, event)

    assert order == ["radius", "disconnect"]
