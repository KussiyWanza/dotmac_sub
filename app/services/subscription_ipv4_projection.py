"""Typed read model for one subscription's current service IPv4."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import Subscription
from app.models.network import IPAssignment, IPv4Address, IPVersion


class ServiceIPv4Source(StrEnum):
    """Provenance for the IPv4 value exposed to subscription UI adapters."""

    exact_primary_assignment = "exact_primary_assignment"
    exact_sole_assignment = "exact_sole_assignment"
    served_projection = "served_projection"
    ambiguous_exact_assignments = "ambiguous_exact_assignments"
    unavailable = "unavailable"


@dataclass(frozen=True, slots=True)
class SubscriptionServiceIPv4:
    """Current IPv4 selection for one exact subscription."""

    subscription_id: UUID
    address: str | None
    assignment_id: UUID | None
    ipv4_address_id: UUID | None
    source: ServiceIPv4Source
    detail: str

    @property
    def is_exact_assignment(self) -> bool:
        return self.source in {
            ServiceIPv4Source.exact_primary_assignment,
            ServiceIPv4Source.exact_sole_assignment,
        }


def resolve_subscription_service_ipv4(
    db: Session,
    *,
    subscription_id: UUID,
) -> SubscriptionServiceIPv4:
    """Resolve display state from exact-service IPAM before its served copy.

    A primary assignment is the authoritative served-address choice. A sole
    exact assignment remains unambiguous migration evidence. Subscriber-wide,
    legacy-unbound, and sibling-subscription assignments are never candidates.
    """

    subscription = db.get(Subscription, subscription_id)
    if subscription is None:
        return SubscriptionServiceIPv4(
            subscription_id=subscription_id,
            address=None,
            assignment_id=None,
            ipv4_address_id=None,
            source=ServiceIPv4Source.unavailable,
            detail="Subscription not found.",
        )

    rows = tuple(
        db.execute(
            select(IPAssignment, IPv4Address.address)
            .join(IPv4Address, IPAssignment.ipv4_address_id == IPv4Address.id)
            .where(
                IPAssignment.subscription_id == subscription_id,
                IPAssignment.ip_version == IPVersion.ipv4,
                IPAssignment.is_active.is_(True),
            )
            .order_by(IPAssignment.id)
        ).all()
    )
    primary_rows = tuple(row for row in rows if row[0].is_primary)
    if len(primary_rows) == 1:
        assignment, address = primary_rows[0]
        return SubscriptionServiceIPv4(
            subscription_id=subscription_id,
            address=str(address),
            assignment_id=assignment.id,
            ipv4_address_id=assignment.ipv4_address_id,
            source=ServiceIPv4Source.exact_primary_assignment,
            detail="Primary active IPAM assignment for this subscription.",
        )
    if len(rows) == 1:
        assignment, address = rows[0]
        return SubscriptionServiceIPv4(
            subscription_id=subscription_id,
            address=str(address),
            assignment_id=assignment.id,
            ipv4_address_id=assignment.ipv4_address_id,
            source=ServiceIPv4Source.exact_sole_assignment,
            detail="Sole active IPAM assignment for this subscription.",
        )
    if rows:
        return SubscriptionServiceIPv4(
            subscription_id=subscription_id,
            address=None,
            assignment_id=None,
            ipv4_address_id=None,
            source=ServiceIPv4Source.ambiguous_exact_assignments,
            detail=(
                "Multiple active IPAM assignments exist without one primary "
                "assignment. Review IPAM before selecting a service IPv4."
            ),
        )

    served_address = str(subscription.ipv4_address or "").strip()
    if served_address:
        return SubscriptionServiceIPv4(
            subscription_id=subscription_id,
            address=served_address,
            assignment_id=None,
            ipv4_address_id=None,
            source=ServiceIPv4Source.served_projection,
            detail=(
                "Served IPv4 projection; no active exact-service IPAM "
                "assignment is linked yet."
            ),
        )
    return SubscriptionServiceIPv4(
        subscription_id=subscription_id,
        address=None,
        assignment_id=None,
        ipv4_address_id=None,
        source=ServiceIPv4Source.unavailable,
        detail="No current service IPv4 is recorded for this subscription.",
    )
