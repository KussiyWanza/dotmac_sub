"""Typed command origination for tracked ONT provisioning workflows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

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


def request_ont_authorization(
    db: Session,
    *,
    olt_id: str,
    fsp: str,
    serial_number: str,
    force_reauthorize: bool = False,
    preset_id: str | None = None,
    scoped_ont_id: str | None = None,
    initiated_by: str | None = None,
) -> ProvisioningCommandResult:
    """Atomically persist and stage one ONT authorization command."""
    if db.get(OLTDevice, olt_id) is None:
        return ProvisioningCommandResult(False, False, "OLT not found.")
    normalized_fsp = str(fsp or "").strip()
    normalized_serial = str(serial_number or "").strip()
    if not normalized_fsp or not normalized_serial:
        return ProvisioningCommandResult(
            False,
            False,
            "Port and serial number are required for ONT authorization.",
        )
    normalized_ont_id = str(scoped_ont_id or "").strip() or None
    if normalized_ont_id is None:
        return ProvisioningCommandResult(
            False,
            False,
            "Authorize & provision requires an assigned ONT. "
            "Assign the device first or use Commission ONT.",
        )
    ont = db.get(OntUnit, normalized_ont_id)
    if ont is None:
        return ProvisioningCommandResult(False, False, "ONT not found.")
    if normalize_serial(ont.serial_number) != normalize_serial(normalized_serial):
        return ProvisioningCommandResult(
            False,
            False,
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
        return ProvisioningCommandResult(
            False,
            False,
            "Authorize & provision requires an active assignment on an exact PON. "
            "Complete assignment first or use Commission ONT.",
        )
    pon = db.get(PonPort, assignment.pon_port_id)
    if (
        pon is None
        or not pon.is_active
        or str(pon.olt_id) != str(olt_id)
        or str(pon.name or "").strip() != normalized_fsp
    ):
        return ProvisioningCommandResult(
            False,
            False,
            "The active assignment does not match the submitted OLT and F/S/P.",
        )

    correlation_key = ont_authorization_correlation_key(
        olt_id=olt_id,
        fsp=normalized_fsp,
        serial_number=normalized_serial,
    )
    target_type = NetworkOperationTargetType.ont
    target_id = normalized_ont_id
    try:
        operation = network_operations.start(
            db,
            NetworkOperationType.ont_authorize,
            target_type,
            target_id,
            correlation_key=correlation_key,
            input_payload={
                "olt_id": olt_id,
                "fsp": normalized_fsp,
                "serial_number": normalized_serial,
                "force_reauthorize": bool(force_reauthorize),
                "preset_id": str(preset_id or "").strip() or None,
                "scoped_ont_id": normalized_ont_id,
            },
            initiated_by=initiated_by or "system",
        )
        dispatch = stage_dispatch(
            db,
            operation,
            NetworkOperationCommand.ont_authorize_v1,
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
            correlation_key=correlation_key,
            message="ONT authorization is already in progress.",
        )

    return ProvisioningCommandResult(
        True,
        True,
        "ONT authorization accepted; progress is tracked in network operations.",
        operation_id=str(operation.id),
        dispatch_id=str(dispatch.id),
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
