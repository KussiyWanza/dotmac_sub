"""Exact-service IPv4 assignment lifecycle and projection repair.

``IPAssignment`` is the desired-address authority. This owner contains both the
safe legacy ownership-link migration and the reviewed exact-service assignment
repair command. A separate fingerprinted command converges the served IPv4
projection after the ledger is exact; its durable event requests RADIUS and
old-IP session consequences after commit.

Preview is exhaustive and read-only.  Confirmation is limited to rows where:

* the assignment has no ``subscription_id``;
* the assignment has one subscriber;
* that subscriber has exactly one active subscription;
* that subscriber has exactly one active IPv4 assignment; and
* the subscription's served IPv4 equals the assignment address.

Every other row remains an explicitly classified blocker for operator review.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import secrets
from collections import Counter, defaultdict
from dataclasses import dataclass
from enum import StrEnum
from typing import NoReturn
from uuid import UUID

from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session, joinedload

from app.models.audit import AuditActorType, AuditEvent
from app.models.catalog import NasDevice, Subscription, SubscriptionStatus
from app.models.network import (
    IPAssignment,
    IpPool,
    IPv4Address,
    IPVersion,
    OLTDevice,
    SubscriberAdditionalRoute,
)
from app.models.network_monitoring import NetworkDevice
from app.models.radius import RadiusClient
from app.models.radius_active_session import RadiusActiveSession
from app.models.router_management import Router
from app.models.subscriber import Subscriber
from app.services.audit_adapter import stage_audit_event
from app.services.domain_errors import DomainError
from app.services.events import EventType, emit_event
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

_OWNER = "network.ip_assignment_lifecycle"
_CONCERN = "exact service ownership of active IPv4 assignments"
_OWNERSHIP_COMMAND = OwnerCommandDefinition(
    owner=_OWNER,
    concern=_CONCERN,
    name="reconcile_ip_assignment_service_ownership",
)
_LIFECYCLE_CONCERN = "reviewed exact-service IPv4 assignment lifecycle repair"
_LIFECYCLE_COMMAND = OwnerCommandDefinition(
    owner=_OWNER,
    concern=_LIFECYCLE_CONCERN,
    name="repair_service_ipv4_assignment",
)
_PROJECTION_CONCERN = "reviewed exact-service IPv4 served projection repair"
_PROJECTION_COMMAND = OwnerCommandDefinition(
    owner=_OWNER,
    concern=_PROJECTION_CONCERN,
    name="repair_service_ipv4_projection",
)
_ITEM_AUDIT_ACTION = "network.ip_assignment.service_ownership_linked"
_BATCH_AUDIT_ACTION = "network.ip_assignment.service_ownership_reconciled"
_LIFECYCLE_ITEM_AUDIT_ACTION = "network.ip_assignment.lifecycle_item_changed"
_LIFECYCLE_BATCH_AUDIT_ACTION = "network.ip_assignment.lifecycle_repaired"
_PROJECTION_BATCH_AUDIT_ACTION = "network.ip_assignment.served_projection_repaired"
_RETAINING_STATUSES = frozenset(
    {
        SubscriptionStatus.active,
        SubscriptionStatus.archived,
        SubscriptionStatus.blocked,
        SubscriptionStatus.disabled,
        SubscriptionStatus.hidden,
        SubscriptionStatus.pending,
        SubscriptionStatus.suspended,
        SubscriptionStatus.stopped,
    }
)
_TERMINAL_STATUSES = frozenset(
    {
        SubscriptionStatus.canceled,
        SubscriptionStatus.expired,
    }
)


class IPAssignmentOwnershipDecision(StrEnum):
    exact = "exact"
    repairable_missing_service_link = "repairable_missing_service_link"
    missing_subscriber = "missing_subscriber"
    missing_subscription = "missing_subscription"
    subscriber_mismatch = "subscriber_mismatch"
    served_address_mismatch = "served_address_mismatch"
    ambiguous_active_services = "ambiguous_active_services"
    ambiguous_active_assignments = "ambiguous_active_assignments"


class IPAssignmentOwnershipError(DomainError):
    """Stable fail-closed service-ownership reconciliation error."""


class IPv4AssignmentRepairDecision(StrEnum):
    ready_create = "ready_create"
    ready_link = "ready_link"
    ready_keep = "ready_keep"
    ready_release = "ready_release"
    noop = "noop"
    subscription_not_found = "subscription_not_found"
    subscriber_not_found = "subscriber_not_found"
    subscription_not_serviceable = "subscription_not_serviceable"
    subscription_not_terminal = "subscription_not_terminal"
    target_address_not_found = "target_address_not_found"
    target_address_not_serviceable = "target_address_not_serviceable"
    target_address_in_routed_block = "target_address_in_routed_block"
    target_address_is_device_host = "target_address_is_device_host"
    target_owned_by_other_service = "target_owned_by_other_service"
    deactivation_assignment_not_found = "deactivation_assignment_not_found"
    deactivation_not_exact_service = "deactivation_not_exact_service"
    desired_assignment_selected_for_deactivation = (
        "desired_assignment_selected_for_deactivation"
    )
    incomplete_deactivation_set = "incomplete_deactivation_set"


class IPv4AssignmentLifecycleError(DomainError):
    """Stable fail-closed exact-service IPv4 assignment lifecycle error."""


class IPv4ServedProjectionDecision(StrEnum):
    ready = "ready"
    noop = "noop"
    subscription_not_found = "subscription_not_found"
    subscription_not_active = "subscription_not_active"
    missing_exact_assignment = "missing_exact_assignment"
    multiple_exact_assignments = "multiple_exact_assignments"
    assignment_subscriber_mismatch = "assignment_subscriber_mismatch"
    missing_login = "missing_login"
    shared_login_not_selected = "shared_login_not_selected"
    radius_observation_unavailable = "radius_observation_unavailable"
    radius_projection_not_aligned = "radius_projection_not_aligned"
    session_observation_conflict = "session_observation_conflict"


@dataclass(frozen=True, slots=True)
class ActiveIPv4AssignmentEvidence:
    assignment_id: UUID
    subscriber_id: UUID | None
    subscription_id: UUID | None
    address_id: UUID
    address: str


@dataclass(frozen=True, slots=True)
class ServiceIPv4AssignmentRepairPreview:
    subscription_id: UUID
    subscriber_id: UUID | None
    desired_address_id: UUID | None
    desired_address: str | None
    deactivate_assignment_ids: tuple[UUID, ...]
    active_assignments: tuple[ActiveIPv4AssignmentEvidence, ...]
    decision: IPv4AssignmentRepairDecision
    fingerprint: str

    @property
    def applicable(self) -> bool:
        return self.decision in {
            IPv4AssignmentRepairDecision.ready_create,
            IPv4AssignmentRepairDecision.ready_link,
            IPv4AssignmentRepairDecision.ready_keep,
            IPv4AssignmentRepairDecision.ready_release,
        }


@dataclass(frozen=True, slots=True)
class RepairServiceIPv4AssignmentCommand:
    context: CommandContext
    subscription_id: UUID
    desired_address_id: UUID | None
    deactivate_assignment_ids: tuple[UUID, ...]
    preview_fingerprint: str


@dataclass(frozen=True, slots=True)
class ServiceIPv4AssignmentRepairOutcome:
    subscription_id: UUID
    desired_assignment_id: UUID | None
    linked_count: int
    created_count: int
    deactivated_count: int
    preview_fingerprint: str
    replayed: bool


@dataclass(frozen=True, slots=True)
class ServiceIPv4ProjectionPreview:
    subscription_id: UUID
    subscriber_id: UUID | None
    assignment_id: UUID | None
    served_address: str | None
    desired_address: str | None
    radius_mode: str | None
    observed_radius_address: str | None
    active_session_count: int
    old_address_session_count: int
    decision: IPv4ServedProjectionDecision
    fingerprint: str

    @property
    def applicable(self) -> bool:
        return self.decision is IPv4ServedProjectionDecision.ready


@dataclass(frozen=True, slots=True)
class RepairServiceIPv4ProjectionCommand:
    context: CommandContext
    subscription_id: UUID
    assignment_id: UUID
    preview_fingerprint: str


@dataclass(frozen=True, slots=True)
class ServiceIPv4ProjectionOutcome:
    subscription_id: UUID
    assignment_id: UUID
    previous_address: str
    desired_address: str
    observed_active_sessions: int
    preview_fingerprint: str
    replayed: bool


@dataclass(frozen=True, slots=True)
class IPAssignmentOwnershipItem:
    assignment_id: UUID
    subscriber_id: UUID | None
    current_subscription_id: UUID | None
    proposed_subscription_id: UUID | None
    address: str
    decision: IPAssignmentOwnershipDecision

    @property
    def repairable(self) -> bool:
        return (
            self.decision
            is IPAssignmentOwnershipDecision.repairable_missing_service_link
        )


@dataclass(frozen=True, slots=True)
class IPAssignmentOwnershipPreview:
    items: tuple[IPAssignmentOwnershipItem, ...]
    fingerprint: str

    @property
    def counts(self) -> dict[IPAssignmentOwnershipDecision, int]:
        counts = Counter(item.decision for item in self.items)
        return {
            decision: counts.get(decision, 0)
            for decision in IPAssignmentOwnershipDecision
        }

    @property
    def repairable_assignment_ids(self) -> tuple[UUID, ...]:
        return tuple(item.assignment_id for item in self.items if item.repairable)


@dataclass(frozen=True, slots=True)
class ReconcileIPAssignmentOwnershipCommand:
    context: CommandContext
    preview_fingerprint: str
    assignment_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class IPAssignmentOwnershipOutcome:
    preview_fingerprint: str
    linked_count: int
    replayed: bool


def _error(suffix: str, message: str, **details: object) -> NoReturn:
    raise IPAssignmentOwnershipError(
        code=f"{_OWNER}.{suffix}",
        message=message,
        details=details,
    )


def _normalized_ip(value: str | None) -> str:
    return str(value or "").strip()


def _preview_payload(
    items: tuple[IPAssignmentOwnershipItem, ...],
) -> list[dict[str, str | None]]:
    return [
        {
            "assignment_id": str(item.assignment_id),
            "subscriber_id": (
                str(item.subscriber_id) if item.subscriber_id is not None else None
            ),
            "current_subscription_id": (
                str(item.current_subscription_id)
                if item.current_subscription_id is not None
                else None
            ),
            "proposed_subscription_id": (
                str(item.proposed_subscription_id)
                if item.proposed_subscription_id is not None
                else None
            ),
            "address": item.address,
            "decision": item.decision.value,
        }
        for item in items
    ]


def _fingerprint(items: tuple[IPAssignmentOwnershipItem, ...]) -> str:
    encoded = json.dumps(
        _preview_payload(items),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def preview_ip_assignment_service_ownership(
    db: Session,
    *,
    assignment_ids: tuple[UUID, ...] | None = None,
) -> IPAssignmentOwnershipPreview:
    """Classify active IPv4 assignment ownership without changing state."""

    requested_ids = set(assignment_ids) if assignment_ids is not None else None
    assignment_rows = list(
        db.execute(
            select(IPAssignment, IPv4Address.address)
            .join(IPv4Address, IPAssignment.ipv4_address_id == IPv4Address.id)
            .where(
                IPAssignment.is_active.is_(True),
                IPAssignment.ip_version == IPVersion.ipv4,
            )
            .order_by(IPAssignment.id)
        ).all()
    )
    assignments = [assignment for assignment, _address in assignment_rows]
    addresses_by_assignment = {
        assignment.id: _normalized_ip(address)
        for assignment, address in assignment_rows
    }
    active_subscriptions = list(
        db.scalars(
            select(Subscription)
            .where(Subscription.status == SubscriptionStatus.active)
            .order_by(Subscription.id)
        ).all()
    )
    subscriptions_by_id = {row.id: row for row in active_subscriptions}
    subscriptions_by_subscriber: dict[UUID, list[Subscription]] = defaultdict(list)
    for subscription in active_subscriptions:
        subscriptions_by_subscriber[subscription.subscriber_id].append(subscription)
    assignments_by_subscriber: dict[UUID, list[IPAssignment]] = defaultdict(list)
    for assignment in assignments:
        if assignment.subscriber_id is not None:
            assignments_by_subscriber[assignment.subscriber_id].append(assignment)

    items: list[IPAssignmentOwnershipItem] = []
    for assignment in assignments:
        if requested_ids is not None and assignment.id not in requested_ids:
            continue
        address = addresses_by_assignment[assignment.id]
        subscriber_id = assignment.subscriber_id
        current_subscription_id = assignment.subscription_id
        proposed_subscription_id: UUID | None = None

        if subscriber_id is None:
            decision = IPAssignmentOwnershipDecision.missing_subscriber
        elif current_subscription_id is not None:
            linked_subscription = subscriptions_by_id.get(current_subscription_id)
            if linked_subscription is None:
                decision = IPAssignmentOwnershipDecision.missing_subscription
            elif linked_subscription.subscriber_id != subscriber_id:
                decision = IPAssignmentOwnershipDecision.subscriber_mismatch
            elif _normalized_ip(linked_subscription.ipv4_address) != address:
                decision = IPAssignmentOwnershipDecision.served_address_mismatch
            elif len(assignments_by_subscriber[subscriber_id]) != 1:
                decision = IPAssignmentOwnershipDecision.ambiguous_active_assignments
            else:
                decision = IPAssignmentOwnershipDecision.exact
        else:
            subscriptions = subscriptions_by_subscriber.get(subscriber_id, [])
            if len(subscriptions) != 1:
                decision = IPAssignmentOwnershipDecision.ambiguous_active_services
            elif len(assignments_by_subscriber[subscriber_id]) != 1:
                decision = IPAssignmentOwnershipDecision.ambiguous_active_assignments
            else:
                subscription = subscriptions[0]
                if _normalized_ip(subscription.ipv4_address) != address:
                    decision = IPAssignmentOwnershipDecision.served_address_mismatch
                else:
                    proposed_subscription_id = subscription.id
                    decision = (
                        IPAssignmentOwnershipDecision.repairable_missing_service_link
                    )
        items.append(
            IPAssignmentOwnershipItem(
                assignment_id=assignment.id,
                subscriber_id=subscriber_id,
                current_subscription_id=current_subscription_id,
                proposed_subscription_id=proposed_subscription_id,
                address=address,
                decision=decision,
            )
        )

    result = tuple(items)
    return IPAssignmentOwnershipPreview(items=result, fingerprint=_fingerprint(result))


def _lifecycle_error(suffix: str, message: str, **details: object) -> NoReturn:
    raise IPv4AssignmentLifecycleError(
        code=f"{_OWNER}.{suffix}",
        message=message,
        details=details,
    )


def _repair_preview_payload(
    *,
    subscription_id: UUID,
    subscriber_id: UUID | None,
    desired_address_id: UUID | None,
    desired_address: str | None,
    deactivate_assignment_ids: tuple[UUID, ...],
    active_assignments: tuple[ActiveIPv4AssignmentEvidence, ...],
    decision: IPv4AssignmentRepairDecision,
) -> dict[str, object]:
    return {
        "subscription_id": str(subscription_id),
        "subscriber_id": str(subscriber_id) if subscriber_id is not None else None,
        "desired_address_id": (
            str(desired_address_id) if desired_address_id is not None else None
        ),
        "desired_address": desired_address,
        "deactivate_assignment_ids": [
            str(value) for value in deactivate_assignment_ids
        ],
        "active_assignments": [
            {
                "assignment_id": str(item.assignment_id),
                "subscriber_id": (
                    str(item.subscriber_id) if item.subscriber_id is not None else None
                ),
                "subscription_id": (
                    str(item.subscription_id)
                    if item.subscription_id is not None
                    else None
                ),
                "address_id": str(item.address_id),
                "address": item.address,
            }
            for item in active_assignments
        ],
        "decision": decision.value,
    }


def _repair_fingerprint(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _active_assignment_evidence(
    db: Session,
    *,
    subscriber_id: UUID,
    desired_address_id: UUID | None,
) -> tuple[ActiveIPv4AssignmentEvidence, ...]:
    ownership_filter = IPAssignment.subscriber_id == subscriber_id
    if desired_address_id is not None:
        ownership_filter = or_(
            ownership_filter,
            IPAssignment.ipv4_address_id == desired_address_id,
        )
    rows = db.execute(
        select(IPAssignment, IPv4Address.address)
        .join(IPv4Address, IPAssignment.ipv4_address_id == IPv4Address.id)
        .where(
            IPAssignment.is_active.is_(True),
            IPAssignment.ip_version == IPVersion.ipv4,
            ownership_filter,
        )
        .order_by(IPAssignment.id)
    ).all()
    return tuple(
        ActiveIPv4AssignmentEvidence(
            assignment_id=assignment.id,
            subscriber_id=assignment.subscriber_id,
            subscription_id=assignment.subscription_id,
            address_id=assignment.ipv4_address_id,
            address=_normalized_ip(address),
        )
        for assignment, address in rows
        if assignment.ipv4_address_id is not None
    )


def _address_is_in_active_routed_block(db: Session, address: str) -> bool:
    try:
        parsed = ipaddress.IPv4Address(address)
    except ipaddress.AddressValueError:
        return True
    for cidr in db.scalars(
        select(SubscriberAdditionalRoute.cidr).where(
            SubscriberAdditionalRoute.is_active.is_(True)
        )
    ).all():
        try:
            network = ipaddress.ip_network(str(cidr), strict=False)
        except ValueError:
            continue
        if isinstance(network, ipaddress.IPv4Network) and parsed in network:
            return True
    return False


# Free-text markers for an infrastructure pool. This is a backstop, NOT the
# primary signal: it only catches pools whose operator happened to spell the
# purpose out. `mgmt` and `mgt` are here because the abbreviation is the common
# hand-created form and "management" does not match it.
_INFRASTRUCTURE_POOL_MARKERS = ("management", "mgmt", "mgt")


def _pool_is_infrastructure(pool: IpPool) -> bool:
    """True when the pool serves devices rather than customers.

    `IpPool` has no typed purpose column, so this reads two signals:

    * `olt_device_id` -- typed and reliable. Every auto-created OLT management
      pool carries it, and customer pools served by an OLT do not; verified
      against production 2026-07-26, where all 7 pools with this FK were
      management pools and none of the 163 customer pools had it.
    * name/notes markers -- a backstop for hand-created infrastructure pools,
      which have no typed signal at all.

    The typed check comes first deliberately. Relying on the text alone made a
    safety decision depend on a naming convention: a pool called `OLT-MGMT`
    does not contain "management", so an OLT management address would have
    been assignable to a customer.
    """
    if pool.olt_device_id is not None:
        return True
    description = f"{pool.name} {pool.notes or ''}".strip().lower()
    return any(marker in description for marker in _INFRASTRUCTURE_POOL_MARKERS)


def _target_address_is_serviceable(
    address: IPv4Address,
    pool: IpPool | None,
) -> bool:
    if address.is_reserved or address.ont_unit_id is not None:
        return False
    if str(address.allocation_type or "").strip().lower() == "management":
        return False
    if pool is None or not pool.is_active or pool.ip_version != IPVersion.ipv4:
        return False
    return not _pool_is_infrastructure(pool)


def _target_address_is_device_host(db: Session, address: str) -> bool:
    candidates = (address, f"{address}/32")
    lookups = (
        select(NetworkDevice.id)
        .where(
            NetworkDevice.is_active.is_(True),
            NetworkDevice.mgmt_ip.in_(candidates),
        )
        .limit(1),
        select(NasDevice.id)
        .where(
            NasDevice.is_active.is_(True),
            or_(
                NasDevice.management_ip.in_(candidates),
                NasDevice.ip_address.in_(candidates),
                NasDevice.nas_ip.in_(candidates),
            ),
        )
        .limit(1),
        select(OLTDevice.id)
        .where(
            OLTDevice.is_active.is_(True),
            OLTDevice.mgmt_ip.in_(candidates),
        )
        .limit(1),
        select(Router.id).where(Router.management_ip.in_(candidates)).limit(1),
        select(RadiusClient.id)
        .where(
            RadiusClient.is_active.is_(True),
            RadiusClient.client_ip.in_(candidates),
        )
        .limit(1),
    )
    return any(db.scalar(statement) is not None for statement in lookups)


def preview_service_ipv4_assignment_repair(
    db: Session,
    *,
    subscription_id: UUID,
    desired_address_id: UUID | None,
    deactivate_assignment_ids: tuple[UUID, ...] = (),
) -> ServiceIPv4AssignmentRepairPreview:
    """Read-only, exact-service plan for one reviewed IPv4 assignment repair."""

    normalized_deactivate_ids = tuple(sorted(set(deactivate_assignment_ids), key=str))
    subscription = db.get(Subscription, subscription_id)
    subscriber_id = subscription.subscriber_id if subscription is not None else None
    desired_address: str | None = None
    active_assignments: tuple[ActiveIPv4AssignmentEvidence, ...] = ()
    decision = IPv4AssignmentRepairDecision.subscription_not_found

    if subscription is not None and subscriber_id is None:
        decision = IPv4AssignmentRepairDecision.subscriber_not_found
    elif (
        subscription is not None
        and subscriber_id is not None
        and db.get(Subscriber, subscriber_id) is None
    ):
        decision = IPv4AssignmentRepairDecision.subscriber_not_found
    elif subscription is not None and subscriber_id is not None:
        active_assignments = _active_assignment_evidence(
            db,
            subscriber_id=subscriber_id,
            desired_address_id=desired_address_id,
        )
        assignments_by_id = {item.assignment_id: item for item in active_assignments}
        requested_rows = [
            assignments_by_id.get(value) for value in normalized_deactivate_ids
        ]
        if any(item is None for item in requested_rows):
            decision = IPv4AssignmentRepairDecision.deactivation_assignment_not_found
        elif any(
            item is not None and item.subscription_id != subscription.id
            for item in requested_rows
        ):
            decision = IPv4AssignmentRepairDecision.deactivation_not_exact_service
        elif desired_address_id is None:
            exact_rows = [
                item
                for item in active_assignments
                if item.subscription_id == subscription.id
            ]
            remaining_exact = [
                item
                for item in exact_rows
                if item.assignment_id not in normalized_deactivate_ids
            ]
            if subscription.status not in _TERMINAL_STATUSES:
                decision = IPv4AssignmentRepairDecision.subscription_not_terminal
            elif remaining_exact:
                decision = IPv4AssignmentRepairDecision.incomplete_deactivation_set
            elif not exact_rows:
                decision = IPv4AssignmentRepairDecision.noop
            else:
                decision = IPv4AssignmentRepairDecision.ready_release
        else:
            target = db.get(IPv4Address, desired_address_id)
            if subscription.status not in _RETAINING_STATUSES:
                decision = IPv4AssignmentRepairDecision.subscription_not_serviceable
            elif target is None:
                decision = IPv4AssignmentRepairDecision.target_address_not_found
            else:
                desired_address = _normalized_ip(target.address)
                pool = db.get(IpPool, target.pool_id) if target.pool_id else None
                target_assignment = next(
                    (
                        item
                        for item in active_assignments
                        if item.address_id == desired_address_id
                    ),
                    None,
                )
                remaining_exact = [
                    item
                    for item in active_assignments
                    if item.subscription_id == subscription.id
                    and item.address_id != desired_address_id
                    and item.assignment_id not in normalized_deactivate_ids
                ]
                if not _target_address_is_serviceable(target, pool):
                    decision = (
                        IPv4AssignmentRepairDecision.target_address_not_serviceable
                    )
                elif _address_is_in_active_routed_block(db, desired_address):
                    decision = (
                        IPv4AssignmentRepairDecision.target_address_in_routed_block
                    )
                elif _target_address_is_device_host(db, desired_address):
                    decision = (
                        IPv4AssignmentRepairDecision.target_address_is_device_host
                    )
                elif target_assignment is not None and (
                    target_assignment.subscriber_id != subscriber_id
                    or target_assignment.subscription_id not in {None, subscription.id}
                ):
                    decision = (
                        IPv4AssignmentRepairDecision.target_owned_by_other_service
                    )
                elif (
                    target_assignment is not None
                    and target_assignment.assignment_id in normalized_deactivate_ids
                ):
                    decision = IPv4AssignmentRepairDecision.desired_assignment_selected_for_deactivation
                elif remaining_exact:
                    decision = IPv4AssignmentRepairDecision.incomplete_deactivation_set
                elif target_assignment is None:
                    decision = IPv4AssignmentRepairDecision.ready_create
                elif target_assignment.subscription_id is None:
                    decision = IPv4AssignmentRepairDecision.ready_link
                elif normalized_deactivate_ids:
                    decision = IPv4AssignmentRepairDecision.ready_keep
                else:
                    decision = IPv4AssignmentRepairDecision.noop

    payload = _repair_preview_payload(
        subscription_id=subscription_id,
        subscriber_id=subscriber_id,
        desired_address_id=desired_address_id,
        desired_address=desired_address,
        deactivate_assignment_ids=normalized_deactivate_ids,
        active_assignments=active_assignments,
        decision=decision,
    )
    return ServiceIPv4AssignmentRepairPreview(
        subscription_id=subscription_id,
        subscriber_id=subscriber_id,
        desired_address_id=desired_address_id,
        desired_address=desired_address,
        deactivate_assignment_ids=normalized_deactivate_ids,
        active_assignments=active_assignments,
        decision=decision,
        fingerprint=_repair_fingerprint(payload),
    )


def _projection_preview_payload(
    *,
    subscription_id: UUID,
    subscriber_id: UUID | None,
    assignment_id: UUID | None,
    served_address: str | None,
    desired_address: str | None,
    radius_mode: str | None,
    observed_radius_address: str | None,
    active_session_count: int,
    old_address_session_count: int,
    decision: IPv4ServedProjectionDecision,
) -> dict[str, object]:
    return {
        "subscription_id": str(subscription_id),
        "subscriber_id": str(subscriber_id) if subscriber_id is not None else None,
        "assignment_id": str(assignment_id) if assignment_id is not None else None,
        "served_address": served_address,
        "desired_address": desired_address,
        "radius_mode": radius_mode,
        "observed_radius_address": observed_radius_address,
        "active_session_count": active_session_count,
        "old_address_session_count": old_address_session_count,
        "decision": decision.value,
    }


def preview_service_ipv4_projection_repair(
    db: Session,
    *,
    subscription_id: UUID,
    assignment_id: UUID,
) -> ServiceIPv4ProjectionPreview:
    """Preview one exact-service served-IP projection repair without writes."""

    from app.services.ip_consistency_audit import _external_ip_state, _norm
    from app.services.radius_access_state import ACTIVE_STATUSES, BLOCKED_STATUSES
    from app.services.radius_projection_planner import plan_login_radius_projections

    subscription = db.get(Subscription, subscription_id)
    subscriber_id = subscription.subscriber_id if subscription is not None else None
    served_address = (
        _norm(subscription.ipv4_address) or None if subscription is not None else None
    )
    desired_address: str | None = None
    radius_mode: str | None = None
    observed_radius_address: str | None = None
    active_session_count = 0
    old_address_session_count = 0
    decision = IPv4ServedProjectionDecision.subscription_not_found

    exact_rows = list(
        db.execute(
            select(IPAssignment, IPv4Address.address)
            .join(IPv4Address, IPAssignment.ipv4_address_id == IPv4Address.id)
            .where(
                IPAssignment.subscription_id == subscription_id,
                IPAssignment.is_active.is_(True),
                IPAssignment.ip_version == IPVersion.ipv4,
            )
            .order_by(IPAssignment.id)
        ).all()
    )
    selected = next(
        (
            (assignment, address)
            for assignment, address in exact_rows
            if assignment.id == assignment_id
        ),
        None,
    )
    if selected is not None:
        desired_address = _norm(selected[1]) or None

    session_addresses = [
        _norm(value) or None
        for value in db.scalars(
            select(RadiusActiveSession.framed_ip_address).where(
                RadiusActiveSession.subscription_id == subscription_id
            )
        ).all()
    ]
    active_session_count = len(session_addresses)
    if served_address is not None:
        old_address_session_count = sum(
            value == served_address for value in session_addresses
        )

    if subscription is None:
        pass
    elif subscription.status is not SubscriptionStatus.active:
        decision = IPv4ServedProjectionDecision.subscription_not_active
    elif len(exact_rows) == 0:
        decision = IPv4ServedProjectionDecision.missing_exact_assignment
    elif len(exact_rows) > 1:
        decision = IPv4ServedProjectionDecision.multiple_exact_assignments
    elif selected is None:
        decision = IPv4ServedProjectionDecision.missing_exact_assignment
    elif selected[0].subscriber_id != subscription.subscriber_id:
        decision = IPv4ServedProjectionDecision.assignment_subscriber_mismatch
    else:
        login = str(subscription.login or "").strip()
        if not login:
            decision = IPv4ServedProjectionDecision.missing_login
        else:
            login_candidates = (
                db.execute(
                    select(Subscription)
                    .options(
                        joinedload(Subscription.subscriber).joinedload(
                            Subscriber.reseller
                        )
                    )
                    .where(
                        Subscription.login == login,
                        Subscription.status.in_(ACTIVE_STATUSES | BLOCKED_STATUSES),
                    )
                )
                .unique()
                .scalars()
                .all()
            )
            projection = plan_login_radius_projections(db, login_candidates).get(login)
            if projection is None or projection.subscription_id != str(subscription.id):
                decision = IPv4ServedProjectionDecision.shared_login_not_selected
            else:
                radius_mode = projection.plan.mode
                framed, provisioned, errors = _external_ip_state(db, [login])
                observed_radius_address = _norm(framed.get(login)) or None
                if errors:
                    decision = (
                        IPv4ServedProjectionDecision.radius_observation_unavailable
                    )
                elif served_address == desired_address:
                    decision = IPv4ServedProjectionDecision.noop
                elif projection.plan.write_radreply and (
                    login not in provisioned
                    or observed_radius_address != served_address
                ):
                    decision = (
                        IPv4ServedProjectionDecision.radius_projection_not_aligned
                    )
                elif not projection.plan.write_radreply and observed_radius_address:
                    decision = (
                        IPv4ServedProjectionDecision.radius_projection_not_aligned
                    )
                elif any(
                    value not in {served_address, desired_address}
                    for value in session_addresses
                ):
                    decision = IPv4ServedProjectionDecision.session_observation_conflict
                else:
                    decision = IPv4ServedProjectionDecision.ready

    payload = _projection_preview_payload(
        subscription_id=subscription_id,
        subscriber_id=subscriber_id,
        assignment_id=selected[0].id if selected is not None else None,
        served_address=served_address,
        desired_address=desired_address,
        radius_mode=radius_mode,
        observed_radius_address=observed_radius_address,
        active_session_count=active_session_count,
        old_address_session_count=old_address_session_count,
        decision=decision,
    )
    return ServiceIPv4ProjectionPreview(
        subscription_id=subscription_id,
        subscriber_id=subscriber_id,
        assignment_id=selected[0].id if selected is not None else None,
        served_address=served_address,
        desired_address=desired_address,
        radius_mode=radius_mode,
        observed_radius_address=observed_radius_address,
        active_session_count=active_session_count,
        old_address_session_count=old_address_session_count,
        decision=decision,
        fingerprint=_repair_fingerprint(payload),
    )


def _prior_outcome(
    db: Session,
    *,
    idempotency_key: str,
    preview_fingerprint: str,
) -> IPAssignmentOwnershipOutcome | None:
    prior = db.scalar(
        select(AuditEvent).where(
            AuditEvent.action == _BATCH_AUDIT_ACTION,
            AuditEvent.entity_type == "ip_assignment_service_ownership",
            AuditEvent.entity_id == idempotency_key,
        )
    )
    if prior is None:
        return None
    metadata = prior.metadata_ if isinstance(prior.metadata_, dict) else {}
    prior_fingerprint = str(metadata.get("preview_fingerprint") or "")
    if not secrets.compare_digest(prior_fingerprint, preview_fingerprint):
        _error(
            "idempotency_conflict",
            "The idempotency key was already used for different IPAM evidence.",
        )
    return IPAssignmentOwnershipOutcome(
        preview_fingerprint=prior_fingerprint,
        linked_count=int(metadata.get("linked_count") or 0),
        replayed=True,
    )


def _reconcile(
    db: Session,
    command: ReconcileIPAssignmentOwnershipCommand,
) -> IPAssignmentOwnershipOutcome:
    idempotency_key = (command.context.idempotency_key or "").strip()
    if not idempotency_key:
        _error("missing_idempotency_key", "An idempotency key is required.")
    if not command.assignment_ids:
        _error("empty_cohort", "The reviewed repair cohort is empty.")
    if len(set(command.assignment_ids)) != len(command.assignment_ids):
        _error("duplicate_assignment", "The repair cohort repeats an assignment.")

    prior = _prior_outcome(
        db,
        idempotency_key=idempotency_key,
        preview_fingerprint=command.preview_fingerprint,
    )
    if prior is not None:
        return prior

    initial = preview_ip_assignment_service_ownership(
        db,
        assignment_ids=command.assignment_ids,
    )
    subscription_ids = sorted(
        {
            item.proposed_subscription_id
            for item in initial.items
            if item.proposed_subscription_id is not None
        },
        key=str,
    )
    if subscription_ids:
        list(
            db.scalars(
                select(Subscription)
                .where(Subscription.id.in_(subscription_ids))
                .order_by(Subscription.id)
                .with_for_update()
            ).all()
        )
    subscriber_ids = sorted(
        {
            item.subscriber_id
            for item in initial.items
            if item.subscriber_id is not None
        },
        key=str,
    )
    if subscriber_ids:
        list(
            db.scalars(
                select(Subscriber)
                .where(Subscriber.id.in_(subscriber_ids))
                .order_by(Subscriber.id)
                .with_for_update()
            ).all()
        )
    locked = list(
        db.scalars(
            select(IPAssignment)
            .where(IPAssignment.id.in_(command.assignment_ids))
            .order_by(IPAssignment.id)
            .with_for_update()
        ).all()
    )
    if len(locked) != len(command.assignment_ids):
        _error("assignment_not_found", "A reviewed IP assignment no longer exists.")
    prior = _prior_outcome(
        db,
        idempotency_key=idempotency_key,
        preview_fingerprint=command.preview_fingerprint,
    )
    if prior is not None:
        return prior
    current = preview_ip_assignment_service_ownership(
        db,
        assignment_ids=command.assignment_ids,
    )
    if not secrets.compare_digest(current.fingerprint, command.preview_fingerprint):
        _error(
            "stale_preview",
            "IPAM ownership evidence changed after preview; preview again.",
            current_fingerprint=current.fingerprint,
        )
    if any(not item.repairable for item in current.items):
        _error(
            "unsafe_cohort",
            "The reviewed cohort contains an ambiguous or already-linked assignment.",
        )

    by_id = {assignment.id: assignment for assignment in locked}
    for item in current.items:
        assert item.proposed_subscription_id is not None
        assignment = by_id[item.assignment_id]
        assignment.subscription_id = item.proposed_subscription_id
        stage_audit_event(
            db,
            action=_ITEM_AUDIT_ACTION,
            entity_type="ip_assignment",
            entity_id=str(assignment.id),
            actor_type=AuditActorType.service,
            actor_id=command.context.actor,
            metadata={
                "subscription_id": str(item.proposed_subscription_id),
                "preview_fingerprint": current.fingerprint,
                "reason": command.context.reason,
            },
        )

    stage_audit_event(
        db,
        action=_BATCH_AUDIT_ACTION,
        entity_type="ip_assignment_service_ownership",
        entity_id=idempotency_key,
        actor_type=AuditActorType.service,
        actor_id=command.context.actor,
        metadata={
            "preview_fingerprint": current.fingerprint,
            "linked_count": len(current.items),
            "assignment_ids": [str(item.assignment_id) for item in current.items],
            "reason": command.context.reason,
        },
    )
    emit_event(
        db,
        EventType.ip_assignment_service_ownership_reconciled,
        {
            "schema_version": 1,
            "preview_fingerprint": current.fingerprint,
            "linked_count": len(current.items),
            "assignment_ids": [str(item.assignment_id) for item in current.items],
        },
        actor=command.context.actor,
    )
    db.flush()
    return IPAssignmentOwnershipOutcome(
        preview_fingerprint=current.fingerprint,
        linked_count=len(current.items),
        replayed=False,
    )


def _prior_lifecycle_outcome(
    db: Session,
    *,
    idempotency_key: str,
    preview_fingerprint: str,
) -> ServiceIPv4AssignmentRepairOutcome | None:
    prior = db.scalar(
        select(AuditEvent).where(
            AuditEvent.action == _LIFECYCLE_BATCH_AUDIT_ACTION,
            AuditEvent.entity_type == "ip_assignment_lifecycle",
            AuditEvent.entity_id == idempotency_key,
        )
    )
    if prior is None:
        return None
    metadata = prior.metadata_ if isinstance(prior.metadata_, dict) else {}
    prior_fingerprint = str(metadata.get("preview_fingerprint") or "")
    if not secrets.compare_digest(prior_fingerprint, preview_fingerprint):
        _lifecycle_error(
            "idempotency_conflict",
            "The idempotency key was already used for different IPAM evidence.",
        )
    desired_assignment_id = metadata.get("desired_assignment_id")
    return ServiceIPv4AssignmentRepairOutcome(
        subscription_id=UUID(str(metadata["subscription_id"])),
        desired_assignment_id=(
            UUID(str(desired_assignment_id)) if desired_assignment_id else None
        ),
        linked_count=int(metadata.get("linked_count") or 0),
        created_count=int(metadata.get("created_count") or 0),
        deactivated_count=int(metadata.get("deactivated_count") or 0),
        preview_fingerprint=prior_fingerprint,
        replayed=True,
    )


def _prior_projection_outcome(
    db: Session,
    *,
    idempotency_key: str,
    preview_fingerprint: str,
) -> ServiceIPv4ProjectionOutcome | None:
    prior = db.scalar(
        select(AuditEvent).where(
            AuditEvent.action == _PROJECTION_BATCH_AUDIT_ACTION,
            AuditEvent.entity_type == "ip_assignment_served_projection",
            AuditEvent.entity_id == idempotency_key,
        )
    )
    if prior is None:
        return None
    metadata = prior.metadata_ if isinstance(prior.metadata_, dict) else {}
    prior_fingerprint = str(metadata.get("preview_fingerprint") or "")
    if not secrets.compare_digest(prior_fingerprint, preview_fingerprint):
        _lifecycle_error(
            "idempotency_conflict",
            "The idempotency key was already used for different projection evidence.",
        )
    return ServiceIPv4ProjectionOutcome(
        subscription_id=UUID(str(metadata["subscription_id"])),
        assignment_id=UUID(str(metadata["assignment_id"])),
        previous_address=str(metadata["previous_address"]),
        desired_address=str(metadata["desired_address"]),
        observed_active_sessions=int(metadata.get("observed_active_sessions") or 0),
        preview_fingerprint=prior_fingerprint,
        replayed=True,
    )


def _prefix_length(pool: IpPool | None) -> int | None:
    if pool is None:
        return None
    try:
        return ipaddress.ip_network(pool.cidr, strict=False).prefixlen
    except ValueError:
        return None


def _lock_eligibility_inventory(db: Session) -> None:
    bind = db.get_bind()
    if bind.dialect.name == "postgresql":
        db.execute(
            text(
                "LOCK TABLE subscriber_additional_routes, network_devices, "
                "nas_devices, olt_devices, routers, radius_clients IN SHARE MODE"
            )
        )


def _stage_lifecycle_item_audit(
    db: Session,
    *,
    command: RepairServiceIPv4AssignmentCommand,
    assignment: IPAssignment,
    action: str,
    fingerprint: str,
) -> None:
    stage_audit_event(
        db,
        action=_LIFECYCLE_ITEM_AUDIT_ACTION,
        entity_type="ip_assignment",
        entity_id=str(assignment.id),
        actor_type=AuditActorType.service,
        actor_id=command.context.actor,
        metadata={
            "action": action,
            "subscription_id": str(command.subscription_id),
            "ipv4_address_id": (
                str(assignment.ipv4_address_id)
                if assignment.ipv4_address_id is not None
                else None
            ),
            "preview_fingerprint": fingerprint,
            "reason": command.context.reason,
        },
    )


def _repair_service_ipv4_assignment(
    db: Session,
    command: RepairServiceIPv4AssignmentCommand,
) -> ServiceIPv4AssignmentRepairOutcome:
    idempotency_key = (command.context.idempotency_key or "").strip()
    if not idempotency_key:
        _lifecycle_error("missing_idempotency_key", "An idempotency key is required.")
    if len(set(command.deactivate_assignment_ids)) != len(
        command.deactivate_assignment_ids
    ):
        _lifecycle_error(
            "duplicate_assignment",
            "The deactivation cohort repeats an assignment.",
        )

    prior = _prior_lifecycle_outcome(
        db,
        idempotency_key=idempotency_key,
        preview_fingerprint=command.preview_fingerprint,
    )
    if prior is not None:
        return prior

    subscription = db.scalar(
        select(Subscription)
        .where(Subscription.id == command.subscription_id)
        .with_for_update()
    )
    if subscription is None:
        _lifecycle_error(
            "subscription_not_found",
            "The reviewed subscription no longer exists.",
        )
    subscriber = db.scalar(
        select(Subscriber)
        .where(Subscriber.id == subscription.subscriber_id)
        .with_for_update()
    )
    if subscriber is None:
        _lifecycle_error(
            "subscriber_not_found",
            "The reviewed subscription no longer has a subscriber.",
        )
    desired_address = None
    desired_pool = None
    if command.desired_address_id is not None:
        desired_address = db.scalar(
            select(IPv4Address)
            .where(IPv4Address.id == command.desired_address_id)
            .with_for_update()
        )
        if desired_address is not None and desired_address.pool_id is not None:
            desired_pool = db.scalar(
                select(IpPool)
                .where(IpPool.id == desired_address.pool_id)
                .with_for_update()
            )
    _lock_eligibility_inventory(db)
    assignment_lock_filter = IPAssignment.subscriber_id == subscriber.id
    if command.desired_address_id is not None:
        assignment_lock_filter = or_(
            assignment_lock_filter,
            IPAssignment.ipv4_address_id == command.desired_address_id,
        )
    locked_assignments = list(
        db.scalars(
            select(IPAssignment)
            .where(assignment_lock_filter)
            .order_by(IPAssignment.id)
            .with_for_update()
        ).all()
    )

    prior = _prior_lifecycle_outcome(
        db,
        idempotency_key=idempotency_key,
        preview_fingerprint=command.preview_fingerprint,
    )
    if prior is not None:
        return prior

    current = preview_service_ipv4_assignment_repair(
        db,
        subscription_id=command.subscription_id,
        desired_address_id=command.desired_address_id,
        deactivate_assignment_ids=command.deactivate_assignment_ids,
    )
    if not secrets.compare_digest(current.fingerprint, command.preview_fingerprint):
        _lifecycle_error(
            "stale_preview",
            "IPAM lifecycle evidence changed after preview; preview again.",
            current_fingerprint=current.fingerprint,
        )
    if not current.applicable:
        _lifecycle_error(
            "unsafe_repair",
            "The reviewed IPAM lifecycle repair is no longer safe to apply.",
            decision=current.decision.value,
        )

    active_by_id = {
        assignment.id: assignment
        for assignment in locked_assignments
        if assignment.is_active
    }
    deactivated_count = 0
    for assignment_id in current.deactivate_assignment_ids:
        assignment = active_by_id[assignment_id]
        assignment.is_active = False
        deactivated_count += 1
        _stage_lifecycle_item_audit(
            db,
            command=command,
            assignment=assignment,
            action="deactivated",
            fingerprint=current.fingerprint,
        )
    if deactivated_count:
        db.flush()

    desired_assignment: IPAssignment | None = None
    linked_count = 0
    created_count = 0
    if command.desired_address_id is not None:
        desired_assignment = next(
            (
                assignment
                for assignment in locked_assignments
                if assignment.is_active
                and assignment.ip_version == IPVersion.ipv4
                and assignment.ipv4_address_id == command.desired_address_id
            ),
            None,
        )
        if desired_assignment is None:
            assert desired_address is not None
            desired_assignment = IPAssignment(
                subscriber_id=subscriber.id,
                subscription_id=subscription.id,
                service_address_id=subscription.service_address_id,
                ip_version=IPVersion.ipv4,
                ipv4_address_id=desired_address.id,
                prefix_length=_prefix_length(desired_pool),
                gateway=desired_pool.gateway if desired_pool is not None else None,
                dns_primary=(
                    desired_pool.dns_primary if desired_pool is not None else None
                ),
                dns_secondary=(
                    desired_pool.dns_secondary if desired_pool is not None else None
                ),
                is_active=True,
            )
            db.add(desired_assignment)
            db.flush()
            created_count = 1
            _stage_lifecycle_item_audit(
                db,
                command=command,
                assignment=desired_assignment,
                action="created",
                fingerprint=current.fingerprint,
            )
        elif desired_assignment.subscription_id is None:
            desired_assignment.subscription_id = subscription.id
            desired_assignment.service_address_id = subscription.service_address_id
            linked_count = 1
            _stage_lifecycle_item_audit(
                db,
                command=command,
                assignment=desired_assignment,
                action="linked",
                fingerprint=current.fingerprint,
            )

    stage_audit_event(
        db,
        action=_LIFECYCLE_BATCH_AUDIT_ACTION,
        entity_type="ip_assignment_lifecycle",
        entity_id=idempotency_key,
        actor_type=AuditActorType.service,
        actor_id=command.context.actor,
        metadata={
            "subscription_id": str(subscription.id),
            "desired_assignment_id": (
                str(desired_assignment.id) if desired_assignment is not None else None
            ),
            "desired_address_id": (
                str(command.desired_address_id)
                if command.desired_address_id is not None
                else None
            ),
            "deactivate_assignment_ids": [
                str(value) for value in current.deactivate_assignment_ids
            ],
            "decision": current.decision.value,
            "preview_fingerprint": current.fingerprint,
            "linked_count": linked_count,
            "created_count": created_count,
            "deactivated_count": deactivated_count,
            "reason": command.context.reason,
        },
    )
    emit_event(
        db,
        EventType.ip_assignment_lifecycle_repaired,
        {
            "schema_version": 1,
            "subscription_id": str(subscription.id),
            "desired_assignment_id": (
                str(desired_assignment.id) if desired_assignment is not None else None
            ),
            "deactivated_assignment_ids": [
                str(value) for value in current.deactivate_assignment_ids
            ],
            "preview_fingerprint": current.fingerprint,
        },
        actor=command.context.actor,
    )
    db.flush()
    return ServiceIPv4AssignmentRepairOutcome(
        subscription_id=subscription.id,
        desired_assignment_id=(
            desired_assignment.id if desired_assignment is not None else None
        ),
        linked_count=linked_count,
        created_count=created_count,
        deactivated_count=deactivated_count,
        preview_fingerprint=current.fingerprint,
        replayed=False,
    )


def _repair_service_ipv4_projection(
    db: Session,
    command: RepairServiceIPv4ProjectionCommand,
) -> ServiceIPv4ProjectionOutcome:
    idempotency_key = (command.context.idempotency_key or "").strip()
    if not idempotency_key:
        _lifecycle_error("missing_idempotency_key", "An idempotency key is required.")
    prior = _prior_projection_outcome(
        db,
        idempotency_key=idempotency_key,
        preview_fingerprint=command.preview_fingerprint,
    )
    if prior is not None:
        return prior

    subscription = db.scalar(
        select(Subscription)
        .where(Subscription.id == command.subscription_id)
        .with_for_update()
    )
    if subscription is None:
        _lifecycle_error(
            "subscription_not_found",
            "The reviewed subscription no longer exists.",
        )
    assignment = db.scalar(
        select(IPAssignment)
        .where(IPAssignment.id == command.assignment_id)
        .with_for_update()
    )
    if assignment is None:
        _lifecycle_error(
            "assignment_not_found",
            "The reviewed IP assignment no longer exists.",
        )
    if assignment.ipv4_address_id is not None:
        db.scalar(
            select(IPv4Address)
            .where(IPv4Address.id == assignment.ipv4_address_id)
            .with_for_update()
        )

    prior = _prior_projection_outcome(
        db,
        idempotency_key=idempotency_key,
        preview_fingerprint=command.preview_fingerprint,
    )
    if prior is not None:
        return prior
    current = preview_service_ipv4_projection_repair(
        db,
        subscription_id=command.subscription_id,
        assignment_id=command.assignment_id,
    )
    if not secrets.compare_digest(current.fingerprint, command.preview_fingerprint):
        _lifecycle_error(
            "stale_preview",
            "IPv4 projection evidence changed after preview; preview again.",
            current_fingerprint=current.fingerprint,
        )
    if not current.applicable:
        _lifecycle_error(
            "unsafe_projection_repair",
            "The reviewed IPv4 projection repair is no longer safe to apply.",
            decision=current.decision.value,
        )
    if (
        current.assignment_id is None
        or current.served_address is None
        or current.desired_address is None
    ):
        _lifecycle_error(
            "incomplete_projection_evidence",
            "The reviewed IPv4 projection evidence is incomplete.",
        )

    from app.services.connectivity_reconciler import (
        note_connectivity_write,
        reconciler_write_scope,
    )

    with reconciler_write_scope():
        subscription.ipv4_address = current.desired_address
        note_connectivity_write(
            "subscription.ipv4_address",
            "ip_assignment_lifecycle",
        )

    stage_audit_event(
        db,
        action=_PROJECTION_BATCH_AUDIT_ACTION,
        entity_type="ip_assignment_served_projection",
        entity_id=idempotency_key,
        actor_type=AuditActorType.service,
        actor_id=command.context.actor,
        metadata={
            "subscription_id": str(subscription.id),
            "assignment_id": str(current.assignment_id),
            "previous_address": current.served_address,
            "desired_address": current.desired_address,
            "radius_mode": current.radius_mode,
            "observed_active_sessions": current.active_session_count,
            "old_address_sessions": current.old_address_session_count,
            "preview_fingerprint": current.fingerprint,
            "reason": command.context.reason,
        },
    )
    emit_event(
        db,
        EventType.ip_assignment_served_projection_repaired,
        {
            "schema_version": 1,
            "subscription_id": str(subscription.id),
            "assignment_id": str(current.assignment_id),
            "previous_address": current.served_address,
            "desired_address": current.desired_address,
            "preview_fingerprint": current.fingerprint,
        },
        actor=command.context.actor,
        subscriber_id=subscription.subscriber_id,
        subscription_id=subscription.id,
    )
    db.flush()
    return ServiceIPv4ProjectionOutcome(
        subscription_id=subscription.id,
        assignment_id=current.assignment_id,
        previous_address=current.served_address,
        desired_address=current.desired_address,
        observed_active_sessions=current.active_session_count,
        preview_fingerprint=current.fingerprint,
        replayed=False,
    )


def reconcile_ip_assignment_service_ownership(
    db: Session,
    command: ReconcileIPAssignmentOwnershipCommand,
) -> IPAssignmentOwnershipOutcome:
    """Apply one exact, fingerprint-bound service-ownership repair."""

    return execute_owner_command(
        db,
        definition=_OWNERSHIP_COMMAND,
        context=command.context,
        operation=lambda: _reconcile(db, command),
    )


def repair_service_ipv4_assignment(
    db: Session,
    command: RepairServiceIPv4AssignmentCommand,
) -> ServiceIPv4AssignmentRepairOutcome:
    """Apply one exact, fingerprint-bound service IPv4 assignment repair."""

    return execute_owner_command(
        db,
        definition=_LIFECYCLE_COMMAND,
        context=command.context,
        operation=lambda: _repair_service_ipv4_assignment(db, command),
    )


def repair_service_ipv4_projection(
    db: Session,
    command: RepairServiceIPv4ProjectionCommand,
) -> ServiceIPv4ProjectionOutcome:
    """Converge one exact-service served-IP projection after reviewed preview."""

    return execute_owner_command(
        db,
        definition=_PROJECTION_COMMAND,
        context=command.context,
        operation=lambda: _repair_service_ipv4_projection(db, command),
    )


__all__ = [
    "ActiveIPv4AssignmentEvidence",
    "IPAssignmentOwnershipDecision",
    "IPAssignmentOwnershipError",
    "IPAssignmentOwnershipItem",
    "IPAssignmentOwnershipOutcome",
    "IPAssignmentOwnershipPreview",
    "IPv4AssignmentLifecycleError",
    "IPv4AssignmentRepairDecision",
    "IPv4ServedProjectionDecision",
    "ReconcileIPAssignmentOwnershipCommand",
    "RepairServiceIPv4AssignmentCommand",
    "RepairServiceIPv4ProjectionCommand",
    "ServiceIPv4AssignmentRepairOutcome",
    "ServiceIPv4AssignmentRepairPreview",
    "ServiceIPv4ProjectionOutcome",
    "ServiceIPv4ProjectionPreview",
    "preview_ip_assignment_service_ownership",
    "preview_service_ipv4_assignment_repair",
    "preview_service_ipv4_projection_repair",
    "reconcile_ip_assignment_service_ownership",
    "repair_service_ipv4_assignment",
    "repair_service_ipv4_projection",
]
