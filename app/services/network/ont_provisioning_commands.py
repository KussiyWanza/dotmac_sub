"""Typed command origination for tracked ONT provisioning workflows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select

from app.models.network import OLTDevice, OntAssignment, OntUnit, PonPort
from app.models.network_operation import (
    NetworkOperation,
    NetworkOperationDispatch,
    NetworkOperationStatus,
    NetworkOperationTargetType,
    NetworkOperationType,
)
from app.services.network.ont_authorization_contracts import (
    AssignedAuthorizationDecision,
    AssignedAuthorizationDecisionCode,
    OntAuthorizationAdmission,
    OntAuthorizationTarget,
    RequestAssignedOntAuthorization,
)
from app.services.network.serial_utils import normalize as normalize_serial
from app.services.network_operation_dispatch import (
    NetworkOperationCommand,
    NetworkOperationDispatchError,
    stage_dispatch,
)
from app.services.network_operations import network_operations

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

_ACTIVE_STATUSES = (
    NetworkOperationStatus.pending,
    NetworkOperationStatus.running,
    NetworkOperationStatus.waiting,
)


@dataclass(frozen=True)
class ProvisioningCommandResult:
    """Durable acceptance result returned to command adapters."""

    accepted: bool
    waiting: bool
    message: str
    operation_id: str | None = None
    dispatch_id: str | None = None
    duplicate: bool = False


def _active_operation(
    db: Session,
    correlation_key: str,
) -> NetworkOperation | None:
    return db.scalars(
        select(NetworkOperation)
        .where(
            NetworkOperation.correlation_key == correlation_key,
            NetworkOperation.status.in_(_ACTIVE_STATUSES),
        )
        .order_by(NetworkOperation.created_at.desc())
        .limit(1)
    ).first()


def _latest_dispatch_id(db: Session, operation: NetworkOperation) -> str | None:
    dispatch_id = db.scalars(
        select(NetworkOperationDispatch.id)
        .where(NetworkOperationDispatch.operation_id == operation.id)
        .order_by(NetworkOperationDispatch.created_at.desc())
        .limit(1)
    ).first()
    return str(dispatch_id) if dispatch_id else None


def _duplicate_result(
    db: Session,
    *,
    correlation_key: str,
    message: str,
) -> ProvisioningCommandResult:
    existing = _active_operation(db, correlation_key)
    if existing is None:
        return ProvisioningCommandResult(
            False,
            False,
            "A command conflict occurred; retry the request.",
        )
    return ProvisioningCommandResult(
        True,
        True,
        message,
        operation_id=str(existing.id),
        dispatch_id=_latest_dispatch_id(db, existing),
        duplicate=True,
    )


def _duplicate_authorization_result(
    db: Session,
    *,
    correlation_key: str,
    message: str,
) -> OntAuthorizationAdmission:
    existing = _active_operation(db, correlation_key)
    if existing is None:
        return OntAuthorizationAdmission(
            accepted=False,
            waiting=False,
            message="A command conflict occurred; retry the request.",
        )
    dispatch_id = _latest_dispatch_id(db, existing)
    return OntAuthorizationAdmission(
        accepted=True,
        waiting=True,
        message=message,
        operation_id=existing.id,
        dispatch_id=UUID(dispatch_id) if dispatch_id is not None else None,
        duplicate=True,
    )


def ont_authorization_correlation_key(
    *,
    olt_id: str,
    fsp: str,
    serial_number: str,
) -> str:
    """Return the canonical correlation key for one ONT authorization command.

    The command owner owns this format; readers that need to find a prior
    attempt for the same OLT/port/serial must use this helper rather than
    rebuilding the string.
    """
    return (
        f"ont_authorize:{olt_id}:{str(fsp or '').strip()}:"
        f"{str(serial_number or '').strip()}"
    )


def evaluate_assigned_authorization(
    db: Session,
    *,
    ont_id: UUID,
    target: OntAuthorizationTarget,
) -> AssignedAuthorizationDecision:
    """Decide whether an exact assigned target may enter OLT authorization."""
    if db.get(OLTDevice, target.olt_id) is None:
        return AssignedAuthorizationDecision(
            AssignedAuthorizationDecisionCode.OLT_NOT_FOUND,
            "OLT not found.",
        )
    ont = db.get(OntUnit, ont_id)
    if ont is None:
        return AssignedAuthorizationDecision(
            AssignedAuthorizationDecisionCode.ONT_NOT_FOUND,
            "ONT not found.",
        )
    if normalize_serial(ont.serial_number) != target.serial_number.value:
        return AssignedAuthorizationDecision(
            AssignedAuthorizationDecisionCode.SERIAL_MISMATCH,
            "The submitted serial does not match the assigned ONT.",
        )
    assignment = db.scalars(
        select(OntAssignment)
        .where(
            OntAssignment.ont_unit_id == ont.id,
            OntAssignment.active.is_(True),
        )
        .limit(1)
    ).first()
    if assignment is None or assignment.pon_port_id is None:
        return AssignedAuthorizationDecision(
            AssignedAuthorizationDecisionCode.ASSIGNMENT_REQUIRED,
            "Authorize & provision requires an active assignment on an exact PON. "
            "Complete assignment first or use Commission ONT.",
        )
    pon = db.get(PonPort, assignment.pon_port_id)
    if (
        pon is None
        or not pon.is_active
        or pon.olt_id != target.olt_id
        or str(pon.name or "").strip() != target.fsp.value
    ):
        return AssignedAuthorizationDecision(
            AssignedAuthorizationDecisionCode.TOPOLOGY_MISMATCH,
            "The active assignment does not match the submitted OLT and F/S/P.",
        )
    return AssignedAuthorizationDecision(
        AssignedAuthorizationDecisionCode.ALLOWED,
        "The exact active assignment permits authorization.",
    )


def request_ont_authorization(
    db: Session,
    command: RequestAssignedOntAuthorization,
) -> OntAuthorizationAdmission:
    """Atomically persist and stage one typed assigned-authorization command."""
    decision = evaluate_assigned_authorization(
        db,
        ont_id=command.ont_id,
        target=command.target,
    )
    if not decision.allowed:
        return OntAuthorizationAdmission(
            accepted=False,
            waiting=False,
            message=decision.message,
        )

    correlation_key = ont_authorization_correlation_key(
        olt_id=str(command.target.olt_id),
        fsp=command.target.fsp.value,
        serial_number=command.target.serial_number.value,
    )
    try:
        operation = network_operations.start(
            db,
            NetworkOperationType.ont_authorize,
            NetworkOperationTargetType.ont,
            str(command.ont_id),
            correlation_key=correlation_key,
            input_payload={
                "olt_id": str(command.target.olt_id),
                "fsp": command.target.fsp.value,
                "serial_number": command.target.serial_number.value,
                "force_reauthorize": command.force_reauthorize,
                "preset_id": (
                    str(command.preset_id) if command.preset_id is not None else None
                ),
                "scoped_ont_id": str(command.ont_id),
            },
            initiated_by=command.context.actor,
        )
        dispatch = stage_dispatch(
            db,
            operation,
            NetworkOperationCommand.ont_authorize_v1,
        )
        db.commit()
    except NetworkOperationDispatchError as exc:
        db.rollback()
        return OntAuthorizationAdmission(False, False, exc.message)
    except HTTPException as exc:
        if exc.status_code != 409:
            raise
        return _duplicate_authorization_result(
            db,
            correlation_key=correlation_key,
            message="ONT authorization is already in progress.",
        )

    return OntAuthorizationAdmission(
        accepted=True,
        waiting=True,
        message=(
            "ONT authorization accepted; progress is tracked in network operations."
        ),
        operation_id=operation.id,
        dispatch_id=dispatch.id,
    )


def request_ont_provisioning(
    db: Session,
    ont_id: str,
    *,
    initiated_by: str | None = None,
    correlation_key: str | None = None,
    bulk_run_id: str | None = None,
    bulk_item_id: str | None = None,
    allow_low_optical_margin: bool = False,
) -> ProvisioningCommandResult:
    """Atomically persist and stage one OLT baseline repair command."""
    if db.get(OntUnit, ont_id) is None:
        return ProvisioningCommandResult(False, False, "ONT not found.")
    effective_correlation = correlation_key or f"provision:{ont_id}"
    try:
        operation = network_operations.start(
            db,
            NetworkOperationType.ont_provision,
            NetworkOperationTargetType.ont,
            ont_id,
            correlation_key=effective_correlation,
            input_payload={
                "ont_id": ont_id,
                "dry_run": False,
                "bulk_run_id": bulk_run_id,
                "bulk_item_id": bulk_item_id,
                "allow_low_optical_margin": bool(allow_low_optical_margin),
            },
            initiated_by=initiated_by or "system",
        )
        dispatch = stage_dispatch(
            db,
            operation,
            NetworkOperationCommand.ont_provision_v1,
        )
        db.commit()
    except NetworkOperationDispatchError as exc:
        db.rollback()
        return ProvisioningCommandResult(False, False, exc.message)
    except HTTPException as exc:
        if exc.status_code != 409:
            raise
        return _duplicate_result(
            db,
            correlation_key=effective_correlation,
            message="ONT provisioning is already in progress.",
        )

    return ProvisioningCommandResult(
        True,
        True,
        "ONT provisioning accepted; waiting for device confirmation.",
        operation_id=str(operation.id),
        dispatch_id=str(dispatch.id),
    )


def stage_bootstrap_attempt(
    db: Session,
    operation: NetworkOperation,
    *,
    attempt: int,
    delay_seconds: int = 0,
) -> NetworkOperationDispatch:
    """Stage one immutable bootstrap attempt on an existing child operation."""
    if operation.operation_type != NetworkOperationType.tr069_bootstrap:
        raise NetworkOperationDispatchError(
            "operation_command_mismatch",
            "Only TR-069 bootstrap operations can stage verification attempts.",
        )
    not_before = datetime.now(UTC) + timedelta(seconds=max(0, delay_seconds))
    return stage_dispatch(
        db,
        operation,
        NetworkOperationCommand.ont_bootstrap_verify_v1,
        dispatch_key=f"attempt:{attempt}",
        not_before=not_before,
    )


def request_bootstrap_verification(
    db: Session,
    *,
    ont_id: str,
    parent_operation_id: str | None,
    initiated_by: str | None,
) -> ProvisioningCommandResult:
    """Create the bootstrap child and its first attempt in one transaction."""
    if db.get(OntUnit, ont_id) is None:
        return ProvisioningCommandResult(False, False, "ONT not found.")
    correlation_key = f"tr069_bootstrap:{ont_id}"
    try:
        operation = network_operations.start(
            db,
            NetworkOperationType.tr069_bootstrap,
            NetworkOperationTargetType.ont,
            ont_id,
            correlation_key=correlation_key,
            input_payload={
                "ont_id": ont_id,
                "parent_operation_id": parent_operation_id,
                "reason": "post_authorization_baseline",
            },
            parent_id=parent_operation_id,
            initiated_by=initiated_by or "system",
        )
        network_operations.mark_waiting(
            db,
            str(operation.id),
            "Waiting for the ONT to register and confirm service state through ACS.",
        )
        dispatch = stage_bootstrap_attempt(db, operation, attempt=0)
        db.commit()
    except NetworkOperationDispatchError as exc:
        db.rollback()
        return ProvisioningCommandResult(False, False, exc.message)
    except HTTPException as exc:
        if exc.status_code != 409:
            raise
        existing = _active_operation(db, correlation_key)
        if existing is None:
            return ProvisioningCommandResult(
                False,
                False,
                "A bootstrap verification conflict occurred; retry the request.",
            )
        if parent_operation_id and str(existing.parent_id or "") != str(
            parent_operation_id
        ):
            return ProvisioningCommandResult(
                False,
                True,
                "Bootstrap verification is already owned by another operation.",
                operation_id=str(existing.id),
                dispatch_id=_latest_dispatch_id(db, existing),
                duplicate=True,
            )
        return ProvisioningCommandResult(
            True,
            True,
            "Bootstrap verification is already in progress.",
            operation_id=str(existing.id),
            dispatch_id=_latest_dispatch_id(db, existing),
            duplicate=True,
        )

    return ProvisioningCommandResult(
        True,
        True,
        "Bootstrap verification accepted.",
        operation_id=str(operation.id),
        dispatch_id=str(dispatch.id),
    )
