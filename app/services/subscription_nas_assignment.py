"""Reviewed subscription service-access moves.

This coordinator owns the decision to move one exact subscription between NAS
access paths.  ``network.ip_assignment_lifecycle`` remains the canonical IPv4
writer; this owner invokes its required, flush-only participants inside one
root transaction and leaves RADIUS/session consequences to the durable event
emitted by the served-IPv4 projection owner.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import secrets
from dataclasses import dataclass
from enum import StrEnum
from typing import NoReturn
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType, AuditEvent
from app.models.catalog import (
    NasDevice,
    NasDeviceStatus,
    Subscription,
    SubscriptionStatus,
)
from app.models.network import IPAssignment, IpPool, IPv4Address, IPVersion
from app.services.audit_adapter import stage_audit_event
from app.services.domain_errors import DomainError
from app.services.ip_assignment_lifecycle import (
    IPv4ServedProjectionDecision,
    RepairServiceIPv4AssignmentCommand,
    RepairServiceIPv4ProjectionCommand,
    apply_service_ipv4_assignment_participant,
    apply_service_ipv4_projection_participant,
    preview_service_ipv4_assignment_repair,
    preview_service_ipv4_projection_repair,
)
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

_OWNER = "service_intent.subscription_nas_assignment"
_CONCERN = "reviewed subscription service-access move"
_COMMAND = OwnerCommandDefinition(
    owner=_OWNER,
    concern=_CONCERN,
    name="move_subscription_service_access",
)
_AUDIT_ACTION = "service_intent.subscription_service_access_moved"
_AUDIT_ENTITY_TYPE = "subscription_service_access_move"


class ServiceAccessMoveDecision(StrEnum):
    ready = "ready"
    subscription_not_found = "subscription_not_found"
    subscription_not_active = "subscription_not_active"
    target_nas_not_found = "target_nas_not_found"
    target_nas_inactive = "target_nas_inactive"
    target_nas_unchanged = "target_nas_unchanged"
    target_pool_not_found = "target_pool_not_found"
    target_pool_inactive = "target_pool_inactive"
    target_pool_not_linked = "target_pool_not_linked"
    target_ipv4_invalid = "target_ipv4_invalid"
    target_ipv4_not_in_pool = "target_ipv4_not_in_pool"
    target_ipv4_not_materialized = "target_ipv4_not_materialized"
    current_assignment_missing = "current_assignment_missing"
    current_assignment_ambiguous = "current_assignment_ambiguous"
    current_projection_unaligned = "current_projection_unaligned"
    target_assignment_unsafe = "target_assignment_unsafe"


class ServiceAccessMoveError(DomainError):
    """Stable fail-closed service-access move error."""


@dataclass(frozen=True, slots=True)
class ServiceAccessMovePreview:
    subscription_id: UUID
    current_nas_device_id: UUID | None
    current_nas_name: str | None
    current_ipv4: str | None
    target_nas_device_id: UUID
    target_nas_name: str | None
    target_pool_id: UUID
    target_pool_name: str | None
    target_ipv4_address_id: UUID | None
    target_ipv4: str
    current_assignment_id: UUID | None
    assignment_preview_fingerprint: str | None
    decision: ServiceAccessMoveDecision
    decision_detail: str
    fingerprint: str

    @property
    def applicable(self) -> bool:
        return self.decision is ServiceAccessMoveDecision.ready


@dataclass(frozen=True, slots=True)
class MoveSubscriptionServiceAccessCommand:
    context: CommandContext
    subscription_id: UUID
    target_nas_device_id: UUID
    target_pool_id: UUID
    target_ipv4: str
    preview_fingerprint: str


@dataclass(frozen=True, slots=True)
class ServiceAccessMoveOutcome:
    subscription_id: UUID
    previous_nas_device_id: UUID
    target_nas_device_id: UUID
    previous_ipv4: str
    target_ipv4: str
    desired_assignment_id: UUID
    preview_fingerprint: str
    replayed: bool


def _error(suffix: str, message: str, **details: object) -> NoReturn:
    raise ServiceAccessMoveError(
        code=f"{_OWNER}.{suffix}",
        message=message,
        details=details,
    )


def _fingerprint(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _preview_payload(
    *,
    subscription_id: UUID,
    current_nas_device_id: UUID | None,
    current_ipv4: str | None,
    target_nas_device_id: UUID,
    target_pool_id: UUID,
    target_ipv4_address_id: UUID | None,
    target_ipv4: str,
    current_assignment_id: UUID | None,
    assignment_preview_fingerprint: str | None,
    decision: ServiceAccessMoveDecision,
    decision_detail: str,
) -> dict[str, object]:
    return {
        "subscription_id": str(subscription_id),
        "current_nas_device_id": (
            str(current_nas_device_id) if current_nas_device_id else None
        ),
        "current_ipv4": current_ipv4,
        "target_nas_device_id": str(target_nas_device_id),
        "target_pool_id": str(target_pool_id),
        "target_ipv4_address_id": (
            str(target_ipv4_address_id) if target_ipv4_address_id else None
        ),
        "target_ipv4": target_ipv4,
        "current_assignment_id": (
            str(current_assignment_id) if current_assignment_id else None
        ),
        "assignment_preview_fingerprint": assignment_preview_fingerprint,
        "decision": decision.value,
        "decision_detail": decision_detail,
    }


def preview_subscription_service_access_move(
    db: Session,
    *,
    subscription_id: UUID,
    target_nas_device_id: UUID,
    target_pool_id: UUID,
    target_ipv4: str,
) -> ServiceAccessMovePreview:
    """Return a read-only, fingerprinted NAS-plus-IPv4 move decision."""

    normalized_target_ipv4 = str(target_ipv4 or "").strip()
    subscription = db.get(Subscription, subscription_id)
    current_nas = (
        db.get(NasDevice, subscription.provisioning_nas_device_id)
        if subscription is not None and subscription.provisioning_nas_device_id
        else None
    )
    target_nas = db.get(NasDevice, target_nas_device_id)
    target_pool = db.get(IpPool, target_pool_id)
    target_address = (
        db.scalar(
            select(IPv4Address).where(IPv4Address.address == normalized_target_ipv4)
        )
        if normalized_target_ipv4
        else None
    )
    current_rows: list[IPAssignment] = []
    if subscription is not None:
        current_rows = list(
            db.scalars(
                select(IPAssignment)
                .where(
                    IPAssignment.subscription_id == subscription.id,
                    IPAssignment.ip_version == IPVersion.ipv4,
                    IPAssignment.is_active.is_(True),
                )
                .order_by(IPAssignment.id)
            ).all()
        )

    current_assignment = current_rows[0] if len(current_rows) == 1 else None
    current_ipv4 = (
        str(current_assignment.ipv4_address.address)
        if current_assignment is not None and current_assignment.ipv4_address
        else None
    )
    decision = ServiceAccessMoveDecision.ready
    detail = "The service access move is ready for confirmation."
    assignment_fingerprint: str | None = None

    if subscription is None:
        decision = ServiceAccessMoveDecision.subscription_not_found
        detail = "The subscription no longer exists."
    elif subscription.status is not SubscriptionStatus.active:
        decision = ServiceAccessMoveDecision.subscription_not_active
        detail = "Only an active subscription can be moved between access routers."
    elif target_nas is None:
        decision = ServiceAccessMoveDecision.target_nas_not_found
        detail = "The selected target router no longer exists."
    elif not target_nas.is_active or target_nas.status is not NasDeviceStatus.active:
        decision = ServiceAccessMoveDecision.target_nas_inactive
        detail = "The selected target router is not active."
    elif subscription.provisioning_nas_device_id == target_nas.id:
        decision = ServiceAccessMoveDecision.target_nas_unchanged
        detail = "Select a different target router for a service-access move."
    elif target_pool is None:
        decision = ServiceAccessMoveDecision.target_pool_not_found
        detail = "The selected target IPv4 pool no longer exists."
    elif not target_pool.is_active or target_pool.ip_version is not IPVersion.ipv4:
        decision = ServiceAccessMoveDecision.target_pool_inactive
        detail = "The selected target IPv4 pool is not active."
    elif target_pool.nas_device_id != target_nas_device_id:
        decision = ServiceAccessMoveDecision.target_pool_not_linked
        detail = "The selected IPv4 pool is not linked to the target router."
    elif not normalized_target_ipv4:
        decision = ServiceAccessMoveDecision.target_ipv4_invalid
        detail = "Select a target IPv4 address."
    else:
        try:
            parsed_ip = ipaddress.ip_address(normalized_target_ipv4)
            parsed_pool = ipaddress.ip_network(str(target_pool.cidr), strict=False)
        except ValueError:
            decision = ServiceAccessMoveDecision.target_ipv4_invalid
            detail = "The selected target IPv4 address or pool is invalid."
        else:
            if parsed_ip.version != 4:
                decision = ServiceAccessMoveDecision.target_ipv4_invalid
                detail = "The selected target address is not IPv4."
            elif parsed_pool.version != 4 or parsed_ip not in parsed_pool:
                decision = ServiceAccessMoveDecision.target_ipv4_not_in_pool
                detail = "The selected IPv4 address is outside the target pool."
            elif target_address is None or target_address.pool_id != target_pool.id:
                decision = ServiceAccessMoveDecision.target_ipv4_not_materialized
                detail = (
                    "The selected IPv4 address is not present in the target "
                    "pool inventory."
                )
            elif len(current_rows) == 0:
                decision = ServiceAccessMoveDecision.current_assignment_missing
                detail = "The service has no exact active IPv4 assignment to move."
            elif len(current_rows) > 1:
                decision = ServiceAccessMoveDecision.current_assignment_ambiguous
                detail = "The service has multiple active IPv4 assignments."
            else:
                assert current_assignment is not None
                current_projection = preview_service_ipv4_projection_repair(
                    db,
                    subscription_id=subscription.id,
                    assignment_id=current_assignment.id,
                )
                if current_projection.decision is not IPv4ServedProjectionDecision.noop:
                    decision = ServiceAccessMoveDecision.current_projection_unaligned
                    detail = (
                        "Current IPAM, served-IP, RADIUS, or session evidence must "
                        "be reconciled before moving this service."
                    )
                else:
                    assignment_preview = preview_service_ipv4_assignment_repair(
                        db,
                        subscription_id=subscription.id,
                        desired_address_id=target_address.id,
                        deactivate_assignment_ids=(current_assignment.id,),
                    )
                    assignment_fingerprint = assignment_preview.fingerprint
                    if not assignment_preview.applicable:
                        decision = ServiceAccessMoveDecision.target_assignment_unsafe
                        detail = (
                            "The target IPv4 assignment is not safe to apply "
                            f"({assignment_preview.decision.value})."
                        )

    payload = _preview_payload(
        subscription_id=subscription_id,
        current_nas_device_id=(
            subscription.provisioning_nas_device_id if subscription else None
        ),
        current_ipv4=current_ipv4,
        target_nas_device_id=target_nas_device_id,
        target_pool_id=target_pool_id,
        target_ipv4_address_id=target_address.id if target_address else None,
        target_ipv4=normalized_target_ipv4,
        current_assignment_id=current_assignment.id if current_assignment else None,
        assignment_preview_fingerprint=assignment_fingerprint,
        decision=decision,
        decision_detail=detail,
    )
    return ServiceAccessMovePreview(
        subscription_id=subscription_id,
        current_nas_device_id=(
            subscription.provisioning_nas_device_id if subscription else None
        ),
        current_nas_name=current_nas.name if current_nas else None,
        current_ipv4=current_ipv4,
        target_nas_device_id=target_nas_device_id,
        target_nas_name=target_nas.name if target_nas else None,
        target_pool_id=target_pool_id,
        target_pool_name=target_pool.name if target_pool else None,
        target_ipv4_address_id=target_address.id if target_address else None,
        target_ipv4=normalized_target_ipv4,
        current_assignment_id=current_assignment.id if current_assignment else None,
        assignment_preview_fingerprint=assignment_fingerprint,
        decision=decision,
        decision_detail=detail,
        fingerprint=_fingerprint(payload),
    )


def _prior_outcome(
    db: Session,
    *,
    command: MoveSubscriptionServiceAccessCommand,
    idempotency_key: str,
) -> ServiceAccessMoveOutcome | None:
    prior = db.scalar(
        select(AuditEvent).where(
            AuditEvent.action == _AUDIT_ACTION,
            AuditEvent.entity_type == _AUDIT_ENTITY_TYPE,
            AuditEvent.entity_id == idempotency_key,
        )
    )
    if prior is None:
        return None
    evidence = prior.metadata_ if isinstance(prior.metadata_, dict) else {}
    prior_fingerprint = str(evidence.get("preview_fingerprint") or "")
    if not secrets.compare_digest(prior_fingerprint, command.preview_fingerprint):
        _error(
            "idempotency_conflict",
            "The idempotency key was already used for a different service move.",
        )
    return ServiceAccessMoveOutcome(
        subscription_id=command.subscription_id,
        previous_nas_device_id=UUID(str(evidence["previous_nas_device_id"])),
        target_nas_device_id=UUID(str(evidence["target_nas_device_id"])),
        previous_ipv4=str(evidence["previous_ipv4"]),
        target_ipv4=str(evidence["target_ipv4"]),
        desired_assignment_id=UUID(str(evidence["desired_assignment_id"])),
        preview_fingerprint=prior_fingerprint,
        replayed=True,
    )


def _move_subscription_service_access(
    db: Session,
    command: MoveSubscriptionServiceAccessCommand,
) -> ServiceAccessMoveOutcome:
    idempotency_key = str(command.context.idempotency_key or "").strip()
    if not idempotency_key:
        _error("missing_idempotency_key", "An idempotency key is required.")
    prior = _prior_outcome(
        db,
        command=command,
        idempotency_key=idempotency_key,
    )
    if prior is not None:
        return prior

    subscription = db.scalar(
        select(Subscription)
        .where(Subscription.id == command.subscription_id)
        .with_for_update()
    )
    if subscription is None:
        _error("subscription_not_found", "The reviewed subscription no longer exists.")
    nas_ids = sorted(
        {
            value
            for value in (
                subscription.provisioning_nas_device_id,
                command.target_nas_device_id,
            )
            if value is not None
        },
        key=str,
    )
    list(
        db.scalars(
            select(NasDevice)
            .where(NasDevice.id.in_(nas_ids))
            .order_by(NasDevice.id)
            .with_for_update()
        ).all()
    )
    db.scalar(
        select(IpPool).where(IpPool.id == command.target_pool_id).with_for_update()
    )
    target_address = db.scalar(
        select(IPv4Address)
        .where(IPv4Address.address == command.target_ipv4)
        .with_for_update()
    )
    if target_address is not None:
        list(
            db.scalars(
                select(IPAssignment)
                .where(
                    (IPAssignment.subscription_id == command.subscription_id)
                    | (IPAssignment.ipv4_address_id == target_address.id)
                )
                .order_by(IPAssignment.id)
                .with_for_update()
            ).all()
        )

    prior = _prior_outcome(
        db,
        command=command,
        idempotency_key=idempotency_key,
    )
    if prior is not None:
        return prior
    current = preview_subscription_service_access_move(
        db,
        subscription_id=command.subscription_id,
        target_nas_device_id=command.target_nas_device_id,
        target_pool_id=command.target_pool_id,
        target_ipv4=command.target_ipv4,
    )
    if not secrets.compare_digest(current.fingerprint, command.preview_fingerprint):
        _error(
            "stale_preview",
            "Service-access evidence changed after preview; preview the move again.",
            current_fingerprint=current.fingerprint,
        )
    if not current.applicable:
        _error(
            "unsafe_move",
            "The reviewed service-access move is no longer safe to apply.",
            decision=current.decision.value,
        )
    if (
        current.current_nas_device_id is None
        or current.current_ipv4 is None
        or current.current_assignment_id is None
        or current.target_ipv4_address_id is None
        or current.assignment_preview_fingerprint is None
    ):
        _error("incomplete_preview", "The reviewed move evidence is incomplete.")

    previous_nas_device_id = current.current_nas_device_id
    previous_ipv4 = current.current_ipv4
    subscription.provisioning_nas_device_id = command.target_nas_device_id
    db.flush()

    assignment_outcome = apply_service_ipv4_assignment_participant(
        db,
        RepairServiceIPv4AssignmentCommand(
            context=CommandContext(
                command_id=command.context.command_id,
                correlation_id=command.context.correlation_id,
                causation_id=command.context.causation_id,
                actor=command.context.actor,
                scope=command.context.scope,
                reason=command.context.reason,
                idempotency_key=f"{idempotency_key}:ipv4-assignment",
            ),
            subscription_id=command.subscription_id,
            desired_address_id=current.target_ipv4_address_id,
            deactivate_assignment_ids=(current.current_assignment_id,),
            preview_fingerprint=current.assignment_preview_fingerprint,
        ),
    )
    if assignment_outcome.desired_assignment_id is None:
        _error("incomplete_outcome", "The IPv4 owner returned no target assignment.")

    projection_preview = preview_service_ipv4_projection_repair(
        db,
        subscription_id=command.subscription_id,
        assignment_id=assignment_outcome.desired_assignment_id,
    )
    if not projection_preview.applicable:
        _error(
            "projection_not_ready",
            "The served IPv4 projection cannot be completed in this move.",
            decision=projection_preview.decision.value,
        )
    apply_service_ipv4_projection_participant(
        db,
        RepairServiceIPv4ProjectionCommand(
            context=CommandContext(
                command_id=command.context.command_id,
                correlation_id=command.context.correlation_id,
                causation_id=command.context.causation_id,
                actor=command.context.actor,
                scope=command.context.scope,
                reason=command.context.reason,
                idempotency_key=f"{idempotency_key}:ipv4-projection",
            ),
            subscription_id=command.subscription_id,
            assignment_id=assignment_outcome.desired_assignment_id,
            preview_fingerprint=projection_preview.fingerprint,
        ),
    )

    stage_audit_event(
        db,
        action=_AUDIT_ACTION,
        entity_type=_AUDIT_ENTITY_TYPE,
        entity_id=idempotency_key,
        actor_type=AuditActorType.service,
        actor_id=command.context.actor,
        metadata={
            "subscription_id": str(subscription.id),
            "previous_nas_device_id": str(previous_nas_device_id),
            "target_nas_device_id": str(command.target_nas_device_id),
            "target_pool_id": str(command.target_pool_id),
            "previous_ipv4": previous_ipv4,
            "target_ipv4": current.target_ipv4,
            "desired_assignment_id": str(assignment_outcome.desired_assignment_id),
            "preview_fingerprint": current.fingerprint,
            "reason": command.context.reason,
            "billing_changed": False,
        },
    )
    db.flush()
    return ServiceAccessMoveOutcome(
        subscription_id=subscription.id,
        previous_nas_device_id=previous_nas_device_id,
        target_nas_device_id=command.target_nas_device_id,
        previous_ipv4=previous_ipv4,
        target_ipv4=current.target_ipv4,
        desired_assignment_id=assignment_outcome.desired_assignment_id,
        preview_fingerprint=current.fingerprint,
        replayed=False,
    )


def move_subscription_service_access(
    db: Session,
    command: MoveSubscriptionServiceAccessCommand,
) -> ServiceAccessMoveOutcome:
    """Commit one reviewed NAS-plus-IPv4 move as a single root transaction."""

    return execute_owner_command(
        db,
        definition=_COMMAND,
        context=command.context,
        operation=lambda: _move_subscription_service_access(db, command),
    )


__all__ = [
    "MoveSubscriptionServiceAccessCommand",
    "ServiceAccessMoveDecision",
    "ServiceAccessMoveError",
    "ServiceAccessMoveOutcome",
    "ServiceAccessMovePreview",
    "move_subscription_service_access",
    "preview_subscription_service_access_move",
]
