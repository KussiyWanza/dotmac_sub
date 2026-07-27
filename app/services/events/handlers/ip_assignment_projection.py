"""Project a committed exact-service IPv4 change to RADIUS and old sessions."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from app.models.catalog import Subscription
from app.services.events.types import Event, EventType
from app.services.ip_consistency_audit import _norm

HANDLED_EVENT_TYPES = frozenset({EventType.ip_assignment_served_projection_repaired})


class IPAssignmentProjectionConsequenceError(RuntimeError):
    """A committed served-IP repair has not reached every required projection."""


class IPAssignmentProjectionHandler:
    """Converge RADIUS first, then retire only sessions pinned to the old IP."""

    def handle(self, db: Session, event: Event) -> None:
        if event.event_type not in HANDLED_EVENT_TYPES:
            return
        if event.payload.get("schema_version") != 1:
            raise IPAssignmentProjectionConsequenceError(
                "Unsupported IP assignment projection event schema."
            )
        try:
            subscription_id = UUID(str(event.payload["subscription_id"]))
            previous_address = _norm(str(event.payload["previous_address"]))
            desired_address = _norm(str(event.payload["desired_address"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise IPAssignmentProjectionConsequenceError(
                "Invalid IP assignment projection event context."
            ) from exc
        if not previous_address or not desired_address:
            raise IPAssignmentProjectionConsequenceError(
                "IP assignment projection event is missing address evidence."
            )

        subscription = db.get(Subscription, subscription_id)
        if subscription is None:
            raise IPAssignmentProjectionConsequenceError(
                "Projected subscription no longer exists."
            )
        if _norm(subscription.ipv4_address) != desired_address:
            raise IPAssignmentProjectionConsequenceError(
                "Served IPv4 no longer matches the committed projection event."
            )

        from app.services.enforcement import disconnect_subscription_sessions
        from app.services.radius import reconcile_subscription_connectivity

        reconcile_subscription_connectivity(
            db,
            str(subscription_id),
        ).require_projected()
        disconnect_subscription_sessions(
            db,
            str(subscription_id),
            reason="ip_assignment_served_projection_repaired",
            framed_ip_address=previous_address,
            require_terminal=True,
        )


__all__ = [
    "HANDLED_EVENT_TYPES",
    "IPAssignmentProjectionConsequenceError",
    "IPAssignmentProjectionHandler",
]
