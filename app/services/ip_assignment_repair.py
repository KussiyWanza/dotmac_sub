"""Exact-service ownership reconciliation for legacy IPv4 assignments.

``IPAssignment`` is the address-allocation authority.  This owner repairs only
the missing bridge from an existing active assignment to its exact
``Subscription``.  It never creates, releases, moves, reclaims, or deactivates
an address and never edits the served IPv4 or RADIUS projection.

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
import json
import secrets
from collections import Counter, defaultdict
from dataclasses import dataclass
from enum import StrEnum
from typing import NoReturn
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType, AuditEvent
from app.models.catalog import Subscription, SubscriptionStatus
from app.models.network import IPAssignment, IPv4Address, IPVersion
from app.models.subscriber import Subscriber
from app.services.audit_adapter import stage_audit_event
from app.services.domain_errors import DomainError
from app.services.events import EventType, emit_event
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

_OWNER = "network.ip_assignment_service_ownership"
_CONCERN = "exact service ownership of active IPv4 assignments"
_COMMAND = OwnerCommandDefinition(
    owner=_OWNER,
    concern=_CONCERN,
    name="reconcile_ip_assignment_service_ownership",
)
_ITEM_AUDIT_ACTION = "network.ip_assignment.service_ownership_linked"
_BATCH_AUDIT_ACTION = "network.ip_assignment.service_ownership_reconciled"


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
    subscriber_ids = sorted(
        {
            assignment.subscriber_id
            for assignment in locked
            if assignment.subscriber_id is not None
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
    prior = _prior_outcome(
        db,
        idempotency_key=idempotency_key,
        preview_fingerprint=command.preview_fingerprint,
    )
    if prior is not None:
        return prior
    subscription_ids = sorted(
        {
            item.proposed_subscription_id
            for item in preview_ip_assignment_service_ownership(
                db, assignment_ids=command.assignment_ids
            ).items
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


def reconcile_ip_assignment_service_ownership(
    db: Session,
    command: ReconcileIPAssignmentOwnershipCommand,
) -> IPAssignmentOwnershipOutcome:
    """Apply one exact, fingerprint-bound service-ownership repair."""

    return execute_owner_command(
        db,
        definition=_COMMAND,
        context=command.context,
        operation=lambda: _reconcile(db, command),
    )


__all__ = [
    "IPAssignmentOwnershipDecision",
    "IPAssignmentOwnershipError",
    "IPAssignmentOwnershipItem",
    "IPAssignmentOwnershipOutcome",
    "IPAssignmentOwnershipPreview",
    "ReconcileIPAssignmentOwnershipCommand",
    "preview_ip_assignment_service_ownership",
    "reconcile_ip_assignment_service_ownership",
]
