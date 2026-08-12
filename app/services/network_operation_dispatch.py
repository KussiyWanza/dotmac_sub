"""Transactional outbox for typed network-operation command delivery.

Operation rows own business/device lifecycle. Dispatch rows own transport state.
Only commands registered here can be staged or executed; callers cannot persist
an arbitrary Celery task name or payload.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from functools import wraps
from typing import TYPE_CHECKING, Any, TypeVar, cast
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError

from app.models.network_operation import (
    NetworkOperation,
    NetworkOperationDispatch,
    NetworkOperationDispatchStatus,
    NetworkOperationStatus,
    NetworkOperationTargetType,
    NetworkOperationType,
)
from app.services.queue_adapter import enqueue_task

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)
_CallableT = TypeVar("_CallableT", bound=Callable[..., Any])

DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_REDELIVERY_AFTER = timedelta(minutes=5)
DEFAULT_EXECUTION_TIMEOUT = timedelta(minutes=20)
_ACTIVE_OPERATION_STATUSES = (
    NetworkOperationStatus.pending,
    NetworkOperationStatus.running,
    NetworkOperationStatus.waiting,
)
_READY_DISPATCH_STATUSES = (
    NetworkOperationDispatchStatus.pending,
    NetworkOperationDispatchStatus.dispatched,
)
_DISPATCH_STATUS_LABELS = {
    NetworkOperationDispatchStatus.pending: "Awaiting broker delivery",
    NetworkOperationDispatchStatus.dispatched: "Awaiting worker acknowledgement",
    NetworkOperationDispatchStatus.acknowledged: "Command executing",
    NetworkOperationDispatchStatus.completed: "Command delivered",
    NetworkOperationDispatchStatus.failed: "Command publication failed",
    NetworkOperationDispatchStatus.reconciliation_needed: "Review current device state",
    NetworkOperationDispatchStatus.canceled: "Command canceled",
}


class NetworkOperationCommand(StrEnum):
    """Versioned commands approved for durable operation dispatch."""

    ont_status_refresh_v1 = "ont_status_refresh.v1"
    ont_authorize_v1 = "ont_authorize.v1"
    ont_commission_v1 = "ont_commission.v1"
    ont_commission_verify_v1 = "ont_commission_verify.v1"
    ont_commission_cleanup_v1 = "ont_commission_cleanup.v1"
    ont_provision_v1 = "ont_provision.v1"
    ont_bootstrap_verify_v1 = "ont_bootstrap_verify.v1"
    ont_firmware_upgrade_v1 = "ont_firmware_upgrade.v1"
    olt_firmware_upgrade_v1 = "olt_firmware_upgrade.v1"
    ont_desired_reconcile_v1 = "ont_desired_reconcile.v1"
    ont_service_config_apply_v1 = "ont_service_config_apply.v1"
    cpe_tr069_command_v1 = "cpe_tr069_command.v1"


class NetworkOperationDispatchError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class DispatchInvocation:
    args: list[object]
    kwargs: dict[str, object]
    queue: str | None = None


@dataclass(frozen=True)
class _CommandSpec:
    task_name: str
    operation_type: NetworkOperationType
    target_types: frozenset[NetworkOperationTargetType]
    invocation: Callable[[NetworkOperation, str], DispatchInvocation]


@dataclass(frozen=True)
class DispatchExecution:
    dispatch_id: str
    operation_id: str
    command_name: str
    task_name: str
    args: list[object]
    kwargs: dict[str, object]


@dataclass
class DispatchSweepResult:
    examined: int = 0
    dispatched: int = 0
    retried: int = 0
    failed: int = 0
    reconciliation_needed: int = 0
    canceled: int = 0
    completed: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "examined": self.examined,
            "dispatched": self.dispatched,
            "retried": self.retried,
            "failed": self.failed,
            "reconciliation_needed": self.reconciliation_needed,
            "canceled": self.canceled,
            "completed": self.completed,
        }


def _required_payload_id(operation: NetworkOperation, field: str) -> str:
    value = str((operation.input_payload or {}).get(field) or "").strip()
    if not value:
        raise NetworkOperationDispatchError(
            "invalid_operation_payload",
            f"Operation payload is missing {field}.",
        )
    return value


def _ont_status_refresh_invocation(
    operation: NetworkOperation,
    _dispatch_key: str,
) -> DispatchInvocation:
    if (operation.input_payload or {}).get("action") != "status_refresh":
        raise NetworkOperationDispatchError(
            "invalid_operation_payload",
            "ONT status refresh operation has the wrong action.",
        )
    return DispatchInvocation(
        args=[str(operation.target_id), str(operation.id)],
        kwargs={},
        queue="ingestion",
    )


def _payload_matches_target(operation: NetworkOperation, field: str) -> str:
    value = _required_payload_id(operation, field)
    if value != str(operation.target_id):
        raise NetworkOperationDispatchError(
            "invalid_operation_payload",
            f"Operation payload {field} does not match its target.",
        )
    return value


def _ont_authorize_invocation(
    operation: NetworkOperation,
    _dispatch_key: str,
) -> DispatchInvocation:
    payload = operation.input_payload or {}
    olt_id = _required_payload_id(operation, "olt_id")
    fsp = str(payload.get("fsp") or "").strip()
    serial_number = str(payload.get("serial_number") or "").strip()
    if not fsp or not serial_number:
        raise NetworkOperationDispatchError(
            "invalid_operation_payload",
            "ONT authorization payload is missing its port or serial number.",
        )
    scoped_ont_id = str(payload.get("scoped_ont_id") or "").strip()
    if not scoped_ont_id or scoped_ont_id != str(operation.target_id):
        raise NetworkOperationDispatchError(
            "invalid_operation_payload",
            "Assigned ONT authorization payload does not match its ONT target.",
        )
    return DispatchInvocation(
        args=[],
        kwargs={
            "olt_id": olt_id,
            "fsp": fsp,
            "serial_number": serial_number,
            "force_reauthorize": bool(payload.get("force_reauthorize")),
            "preset_id": str(payload.get("preset_id") or "").strip() or None,
            "scoped_ont_id": scoped_ont_id,
            "initiated_by": operation.initiated_by,
            "operation_id": str(operation.id),
        },
    )


def _commissioning_attempt(dispatch_key: str) -> int:
    prefix, separator, raw_attempt = dispatch_key.partition(":")
    if prefix != "verify" or not separator:
        raise NetworkOperationDispatchError(
            "invalid_dispatch_key",
            "Commissioning verification dispatch keys must identify an attempt.",
        )
    try:
        attempt = int(raw_attempt)
    except ValueError as exc:
        raise NetworkOperationDispatchError(
            "invalid_dispatch_key",
            "Commissioning verification attempt must be an integer.",
        ) from exc
    if not 0 <= attempt <= 4:
        raise NetworkOperationDispatchError(
            "invalid_dispatch_key",
            "Commissioning verification attempt is outside the supported range.",
        )
    return attempt


def _ont_commission_invocation(
    operation: NetworkOperation,
    _dispatch_key: str,
) -> DispatchInvocation:
    intent_id = _required_payload_id(operation, "intent_id")
    olt_id = _required_payload_id(operation, "olt_id")
    if (
        operation.target_type != NetworkOperationTargetType.olt
        or str(operation.target_id) != olt_id
    ):
        raise NetworkOperationDispatchError(
            "invalid_operation_payload",
            "Commissioning operation does not match its exact OLT target.",
        )
    return DispatchInvocation(
        args=[intent_id, str(operation.id)],
        kwargs={},
        queue="tr069",
    )


def _ont_commission_verify_invocation(
    operation: NetworkOperation,
    dispatch_key: str,
) -> DispatchInvocation:
    intent_id = _required_payload_id(operation, "intent_id")
    return DispatchInvocation(
        args=[intent_id, str(operation.id), _commissioning_attempt(dispatch_key)],
        kwargs={},
        queue="tr069",
    )


def _ont_commission_cleanup_invocation(
    operation: NetworkOperation,
    _dispatch_key: str,
) -> DispatchInvocation:
    intent_id = _required_payload_id(operation, "intent_id")
    return DispatchInvocation(
        args=[intent_id, str(operation.id)],
        kwargs={},
        queue="tr069",
    )


def _ont_provision_invocation(
    operation: NetworkOperation,
    _dispatch_key: str,
) -> DispatchInvocation:
    payload = operation.input_payload or {}
    ont_id = _payload_matches_target(operation, "ont_id")
    if payload.get("dry_run") is not False:
        raise NetworkOperationDispatchError(
            "invalid_operation_payload",
            "Durable ONT provisioning commands cannot be dry runs.",
        )
    return DispatchInvocation(
        args=[ont_id],
        kwargs={
            "dry_run": False,
            "initiated_by": operation.initiated_by,
            "correlation_key": operation.correlation_key,
            "bulk_run_id": str(payload.get("bulk_run_id") or "").strip() or None,
            "bulk_item_id": str(payload.get("bulk_item_id") or "").strip() or None,
            "allow_low_optical_margin": bool(payload.get("allow_low_optical_margin")),
            "operation_id": str(operation.id),
        },
    )


def _bootstrap_attempt(dispatch_key: str) -> int:
    prefix, separator, raw_attempt = dispatch_key.partition(":")
    if prefix != "attempt" or not separator:
        raise NetworkOperationDispatchError(
            "invalid_dispatch_key",
            "Bootstrap verification dispatch keys must identify an attempt.",
        )
    try:
        attempt = int(raw_attempt)
    except ValueError as exc:
        raise NetworkOperationDispatchError(
            "invalid_dispatch_key",
            "Bootstrap verification attempt must be an integer.",
        ) from exc
    if not 0 <= attempt <= 4:
        raise NetworkOperationDispatchError(
            "invalid_dispatch_key",
            "Bootstrap verification attempt is outside the supported range.",
        )
    return attempt


def _ont_bootstrap_invocation(
    operation: NetworkOperation,
    dispatch_key: str,
) -> DispatchInvocation:
    ont_id = _payload_matches_target(operation, "ont_id")
    return DispatchInvocation(
        args=[ont_id, str(operation.id), _bootstrap_attempt(dispatch_key)],
        kwargs={},
    )


def _ont_firmware_invocation(
    operation: NetworkOperation,
    _dispatch_key: str,
) -> DispatchInvocation:
    image_id = _required_payload_id(operation, "firmware_image_id")
    return DispatchInvocation(
        args=[str(operation.target_id), image_id, str(operation.id)],
        kwargs={},
    )


def _olt_firmware_invocation(
    operation: NetworkOperation,
    _dispatch_key: str,
) -> DispatchInvocation:
    image_id = _required_payload_id(operation, "firmware_image_id")
    return DispatchInvocation(
        args=[str(operation.target_id), image_id],
        kwargs={"operation_id": str(operation.id)},
    )


def _ont_reconcile_invocation(
    operation: NetworkOperation,
    _dispatch_key: str,
) -> DispatchInvocation:
    reason = str((operation.input_payload or {}).get("reason") or "").strip()
    if reason != "olt_acs_assignment_changed":
        raise NetworkOperationDispatchError(
            "invalid_operation_payload",
            "ONT desired reconciliation has an unsupported reason.",
        )
    return DispatchInvocation(
        args=[str(operation.target_id), str(operation.id)],
        kwargs={},
    )


def _ont_service_config_invocation(
    operation: NetworkOperation,
    dispatch_key: str,
) -> DispatchInvocation:
    ont_id = _payload_matches_target(operation, "ont_id")
    head_id = _required_payload_id(operation, "configuration_head_id")
    revision = (operation.input_payload or {}).get("configuration_revision")
    try:
        parsed_revision = int(str(revision))
    except (TypeError, ValueError) as exc:
        raise NetworkOperationDispatchError(
            "invalid_operation_payload",
            "ONT service configuration payload has an invalid revision.",
        ) from exc
    if parsed_revision < 1:
        raise NetworkOperationDispatchError(
            "invalid_operation_payload",
            "ONT service configuration revision must be positive.",
        )
    verification_attempt = 0
    if dispatch_key.startswith("verify:"):
        try:
            verification_attempt = int(dispatch_key.partition(":")[2])
        except ValueError as exc:
            raise NetworkOperationDispatchError(
                "invalid_dispatch_key",
                "ONT service configuration verification key is invalid.",
            ) from exc
    return DispatchInvocation(
        args=[ont_id, str(operation.id), head_id, parsed_revision],
        kwargs={
            "verification_attempt": verification_attempt,
            "explicit_repair": bool(
                (operation.input_payload or {}).get("explicit_repair")
            ),
        },
        queue="network",
    )


def _cpe_tr069_invocation(
    operation: NetworkOperation,
    _dispatch_key: str,
) -> DispatchInvocation:
    payload = operation.input_payload or {}
    job_id = _required_payload_id(operation, "job_id")
    device_id = _required_payload_id(operation, "device_id")
    if device_id != str(operation.target_id):
        raise NetworkOperationDispatchError(
            "invalid_operation_payload",
            "TR-069 command device does not match its operation target.",
        )
    if str(payload.get("command") or "") not in {
        "refreshObject",
        "reboot",
        "factoryReset",
        "setParameterValues",
        "download",
    }:
        raise NetworkOperationDispatchError(
            "invalid_operation_payload",
            "TR-069 operation contains an unsupported command.",
        )
    return DispatchInvocation(
        args=[str(operation.id), job_id],
        kwargs={},
        queue="acs",
    )


_COMMAND_SPECS: dict[NetworkOperationCommand, _CommandSpec] = {
    NetworkOperationCommand.ont_status_refresh_v1: _CommandSpec(
        task_name="app.tasks.ont_runtime_status.refresh_single_ont_status",
        operation_type=NetworkOperationType.olt_ont_sync,
        target_types=frozenset({NetworkOperationTargetType.ont}),
        invocation=_ont_status_refresh_invocation,
    ),
    NetworkOperationCommand.ont_authorize_v1: _CommandSpec(
        task_name="app.tasks.ont_provisioning.authorize_ont",
        operation_type=NetworkOperationType.ont_authorize,
        target_types=frozenset({NetworkOperationTargetType.ont}),
        invocation=_ont_authorize_invocation,
    ),
    NetworkOperationCommand.ont_commission_v1: _CommandSpec(
        task_name="app.tasks.ont_commissioning.commission_ont",
        operation_type=NetworkOperationType.ont_commission,
        target_types=frozenset({NetworkOperationTargetType.olt}),
        invocation=_ont_commission_invocation,
    ),
    NetworkOperationCommand.ont_commission_verify_v1: _CommandSpec(
        task_name="app.tasks.ont_commissioning.verify_commissioned_ont",
        operation_type=NetworkOperationType.ont_commission,
        target_types=frozenset({NetworkOperationTargetType.olt}),
        invocation=_ont_commission_verify_invocation,
    ),
    NetworkOperationCommand.ont_commission_cleanup_v1: _CommandSpec(
        task_name="app.tasks.ont_commissioning.cleanup_commissioned_ont",
        operation_type=NetworkOperationType.ont_commission_cleanup,
        target_types=frozenset(
            {
                NetworkOperationTargetType.olt,
                NetworkOperationTargetType.ont,
            }
        ),
        invocation=_ont_commission_cleanup_invocation,
    ),
    NetworkOperationCommand.ont_provision_v1: _CommandSpec(
        task_name="app.tasks.ont_provisioning.provision_ont",
        operation_type=NetworkOperationType.ont_provision,
        target_types=frozenset({NetworkOperationTargetType.ont}),
        invocation=_ont_provision_invocation,
    ),
    NetworkOperationCommand.ont_bootstrap_verify_v1: _CommandSpec(
        task_name="app.tasks.tr069.wait_for_ont_bootstrap",
        operation_type=NetworkOperationType.tr069_bootstrap,
        target_types=frozenset({NetworkOperationTargetType.ont}),
        invocation=_ont_bootstrap_invocation,
    ),
    NetworkOperationCommand.ont_firmware_upgrade_v1: _CommandSpec(
        task_name="app.tasks.ont_firmware.apply_huawei_ont_firmware",
        operation_type=NetworkOperationType.ont_firmware_upgrade,
        target_types=frozenset({NetworkOperationTargetType.ont}),
        invocation=_ont_firmware_invocation,
    ),
    NetworkOperationCommand.olt_firmware_upgrade_v1: _CommandSpec(
        task_name="app.tasks.olt_firmware.upgrade_with_verification",
        operation_type=NetworkOperationType.olt_firmware_upgrade,
        target_types=frozenset({NetworkOperationTargetType.olt}),
        invocation=_olt_firmware_invocation,
    ),
    NetworkOperationCommand.ont_desired_reconcile_v1: _CommandSpec(
        task_name="app.tasks.ont_reconcile.reconcile_huawei_ont",
        operation_type=NetworkOperationType.olt_ont_sync,
        target_types=frozenset({NetworkOperationTargetType.ont}),
        invocation=_ont_reconcile_invocation,
    ),
    NetworkOperationCommand.ont_service_config_apply_v1: _CommandSpec(
        task_name="app.tasks.ont_service_configuration.apply",
        operation_type=NetworkOperationType.ont_service_config,
        target_types=frozenset({NetworkOperationTargetType.ont}),
        invocation=_ont_service_config_invocation,
    ),
    NetworkOperationCommand.cpe_tr069_command_v1: _CommandSpec(
        task_name="app.tasks.tr069.execute_network_operation_job",
        operation_type=NetworkOperationType.cpe_tr069_command,
        target_types=frozenset({NetworkOperationTargetType.cpe}),
        invocation=_cpe_tr069_invocation,
    ),
}


def _as_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _command_spec(
    command: NetworkOperationCommand | str,
) -> tuple[NetworkOperationCommand, _CommandSpec]:
    try:
        normalized = NetworkOperationCommand(str(command))
    except ValueError as exc:
        raise NetworkOperationDispatchError(
            "unsupported_command",
            "Network operation command is not registered.",
        ) from exc
    return normalized, _COMMAND_SPECS[normalized]


def _validated_invocation(
    operation: NetworkOperation,
    command: NetworkOperationCommand | str,
    dispatch_key: str,
) -> tuple[NetworkOperationCommand, _CommandSpec, DispatchInvocation]:
    normalized, spec = _command_spec(command)
    if (
        operation.operation_type != spec.operation_type
        or operation.target_type not in spec.target_types
    ):
        raise NetworkOperationDispatchError(
            "operation_command_mismatch",
            "Command does not match the tracked operation type and target.",
        )
    return normalized, spec, spec.invocation(operation, dispatch_key)


def stage_dispatch(
    db: Session,
    operation: NetworkOperation,
    command: NetworkOperationCommand | str,
    *,
    dispatch_key: str = "primary",
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    not_before: datetime | None = None,
) -> NetworkOperationDispatch:
    """Persist one typed command in the caller's operation transaction."""
    if operation.status not in _ACTIVE_OPERATION_STATUSES:
        raise NetworkOperationDispatchError(
            "operation_not_active",
            "Only active operations can stage command delivery.",
        )
    key = str(dispatch_key or "").strip()
    if not 1 <= len(key) <= 80:
        raise NetworkOperationDispatchError(
            "invalid_dispatch_key",
            "Dispatch key must be between 1 and 80 characters.",
        )
    if not 1 <= int(max_attempts) <= 20:
        raise NetworkOperationDispatchError(
            "invalid_attempt_budget",
            "Dispatch attempt budget must be between 1 and 20.",
        )

    normalized, spec, invocation = _validated_invocation(operation, command, key)
    existing = db.scalars(
        select(NetworkOperationDispatch).where(
            NetworkOperationDispatch.operation_id == operation.id,
            NetworkOperationDispatch.dispatch_key == key,
        )
    ).first()
    if existing is not None:
        if existing.command_name != normalized.value:
            raise NetworkOperationDispatchError(
                "dispatch_key_conflict",
                "Dispatch key is already owned by another command.",
            )
        return existing

    dispatch = NetworkOperationDispatch(
        operation_id=operation.id,
        dispatch_key=key,
        command_name=normalized.value,
        task_name=spec.task_name,
        args_payload=invocation.args,
        kwargs_payload=invocation.kwargs,
        queue=invocation.queue,
        status=NetworkOperationDispatchStatus.pending,
        attempts=0,
        max_attempts=int(max_attempts),
        next_attempt_at=(
            _as_aware_utc(not_before) if not_before is not None else datetime.now(UTC)
        ),
    )
    try:
        from app.services.owner_commands import (
            execute_owner_savepoint,
            owner_command_active,
        )

        if owner_command_active(db):

            def _persist_dispatch() -> None:
                db.add(dispatch)
                db.flush()

            execute_owner_savepoint(
                db,
                _persist_dispatch,
            )
        else:
            with db.begin_nested():
                db.add(dispatch)
                db.flush()
    except IntegrityError:
        winner = db.scalars(
            select(NetworkOperationDispatch).where(
                NetworkOperationDispatch.operation_id == operation.id,
                NetworkOperationDispatch.dispatch_key == key,
            )
        ).first()
        if winner is None:
            raise
        if winner.command_name != normalized.value:
            raise NetworkOperationDispatchError(
                "dispatch_key_conflict",
                "Dispatch key is already owned by another command.",
            )
        return winner
    logger.info(
        "Network operation dispatch staged",
        extra={
            "event": "network_operation_dispatch",
            "dispatch_id": str(dispatch.id),
            "operation_id": str(operation.id),
            "command_name": normalized.value,
            "dispatch_status": dispatch.status.value,
        },
    )
    return dispatch


def _terminalize_operation(
    db: Session,
    dispatch: NetworkOperationDispatch,
    message: str,
) -> None:
    operation = dispatch.operation
    if operation.status not in _ACTIVE_OPERATION_STATUSES:
        return
    from app.services.network_operations import network_operations

    # Merge, never replace: the operation may already carry committed device
    # evidence (for example a landed ONT authorization) that must survive a
    # worker crash, otherwise the ledger claims the device was never touched.
    existing_payload = operation.output_payload or {}
    network_operations.mark_failed(
        db,
        str(operation.id),
        message,
        output_payload={
            **(existing_payload if isinstance(existing_payload, dict) else {}),
            "dispatch_id": str(dispatch.id),
            "dispatch_status": dispatch.status.value,
            "message": message,
        },
    )


def _mark_publish_exhausted(
    db: Session,
    dispatch: NetworkOperationDispatch,
    *,
    now: datetime,
    unknown_delivery: bool,
) -> None:
    if unknown_delivery:
        dispatch.status = NetworkOperationDispatchStatus.reconciliation_needed
        message = (
            "Command delivery could not be confirmed. Review current device state "
            "before retrying the operation."
        )
    else:
        dispatch.status = NetworkOperationDispatchStatus.failed
        message = "Command could not be published after its retry budget was exhausted."
    dispatch.completed_at = now
    dispatch.last_error = message
    _terminalize_operation(db, dispatch, message)


def _publish_backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(300, 2 ** min(max(attempts, 1), 8)))


def _ready_dispatch_ids(
    db: Session,
    *,
    now: datetime,
    limit: int,
    redelivery_after: timedelta,
) -> list[UUID]:
    redelivery_cutoff = now - redelivery_after
    return list(
        db.scalars(
            select(NetworkOperationDispatch.id)
            .where(
                or_(
                    (
                        NetworkOperationDispatch.status
                        == NetworkOperationDispatchStatus.pending
                    )
                    & or_(
                        NetworkOperationDispatch.next_attempt_at.is_(None),
                        NetworkOperationDispatch.next_attempt_at <= now,
                    ),
                    (
                        NetworkOperationDispatch.status
                        == NetworkOperationDispatchStatus.dispatched
                    )
                    & (NetworkOperationDispatch.acknowledged_at.is_(None))
                    & (NetworkOperationDispatch.last_attempt_at <= redelivery_cutoff),
                )
            )
            .order_by(NetworkOperationDispatch.created_at.asc())
            .limit(limit)
        ).all()
    )


def publish_ready_dispatches(
    db: Session,
    *,
    now: datetime | None = None,
    limit: int = 100,
    redelivery_after: timedelta = DEFAULT_REDELIVERY_AFTER,
) -> DispatchSweepResult:
    """Publish due rows to registered tasks wrapped by the execution claim.

    Each row stays locked until broker acknowledgement is recorded. If the
    process dies after broker acceptance but before commit, a duplicate envelope
    may be sent later; the worker-side claim allows only one execution.
    """
    current = now or datetime.now(UTC)
    result = DispatchSweepResult()
    ids = _ready_dispatch_ids(
        db,
        now=current,
        limit=max(1, min(int(limit), 500)),
        redelivery_after=redelivery_after,
    )
    for dispatch_id in ids:
        dispatch = db.scalar(
            select(NetworkOperationDispatch)
            .where(NetworkOperationDispatch.id == dispatch_id)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        if dispatch is None or dispatch.status not in _READY_DISPATCH_STATUSES:
            db.rollback()
            continue
        result.examined += 1
        operation = dispatch.operation
        if operation.status not in _ACTIVE_OPERATION_STATUSES:
            dispatch.status = NetworkOperationDispatchStatus.canceled
            dispatch.completed_at = current
            dispatch.last_error = "Operation became terminal before command delivery."
            result.canceled += 1
            db.commit()
            continue

        was_dispatched = dispatch.status == NetworkOperationDispatchStatus.dispatched
        if dispatch.attempts >= dispatch.max_attempts:
            _mark_publish_exhausted(
                db,
                dispatch,
                now=current,
                unknown_delivery=was_dispatched,
            )
            if was_dispatched:
                result.reconciliation_needed += 1
            else:
                result.failed += 1
            db.commit()
            continue

        dispatch.attempts += 1
        dispatch.last_attempt_at = current
        queued = enqueue_task(
            dispatch.task_name,
            args=list(dispatch.args_payload or []),
            kwargs={
                **dict(dispatch.kwargs_payload or {}),
                "_network_dispatch_id": str(dispatch.id),
            },
            queue=dispatch.queue,
            correlation_id=operation.correlation_key,
            source="network_operation_dispatch",
            request_id=str(dispatch.id),
            actor_id=operation.initiated_by,
            headers={
                "network_operation_id": str(operation.id),
                "network_command": dispatch.command_name,
            },
        )
        if not queued.queued:
            dispatch.last_error = queued.error or "Broker rejected command envelope."
            if dispatch.attempts >= dispatch.max_attempts:
                _mark_publish_exhausted(
                    db,
                    dispatch,
                    now=current,
                    unknown_delivery=was_dispatched,
                )
                if was_dispatched:
                    result.reconciliation_needed += 1
                else:
                    result.failed += 1
            else:
                dispatch.status = (
                    NetworkOperationDispatchStatus.dispatched
                    if was_dispatched
                    else NetworkOperationDispatchStatus.pending
                )
                dispatch.next_attempt_at = current + _publish_backoff(dispatch.attempts)
                result.retried += 1
            db.commit()
            continue

        dispatch.status = NetworkOperationDispatchStatus.dispatched
        dispatch.dispatched_at = dispatch.dispatched_at or current
        dispatch.task_id = queued.task_id
        dispatch.last_error = None
        dispatch.next_attempt_at = None
        result.dispatched += 1
        db.commit()
    return result


def claim_dispatch_execution(
    db: Session,
    dispatch_id: str,
    *,
    now: datetime | None = None,
) -> DispatchExecution | None:
    """Atomically allow at most one envelope to enter the target task."""
    try:
        parsed_id = UUID(str(dispatch_id))
    except ValueError as exc:
        raise NetworkOperationDispatchError(
            "dispatch_not_found", "Network operation dispatch not found."
        ) from exc
    dispatch = db.scalar(
        select(NetworkOperationDispatch)
        .where(NetworkOperationDispatch.id == parsed_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if dispatch is None:
        raise NetworkOperationDispatchError(
            "dispatch_not_found", "Network operation dispatch not found."
        )
    if dispatch.status not in _READY_DISPATCH_STATUSES:
        return None
    operation = dispatch.operation
    if operation.status not in _ACTIVE_OPERATION_STATUSES:
        dispatch.status = NetworkOperationDispatchStatus.canceled
        dispatch.completed_at = now or datetime.now(UTC)
        dispatch.last_error = "Operation became terminal before worker acknowledgement."
        db.flush()
        return None

    normalized, spec, invocation = _validated_invocation(
        operation,
        dispatch.command_name,
        dispatch.dispatch_key,
    )
    stored_args = list(dispatch.args_payload or [])
    stored_kwargs = dict(dispatch.kwargs_payload or {})
    if (
        dispatch.task_name != spec.task_name
        or stored_args != invocation.args
        or stored_kwargs != invocation.kwargs
        or dispatch.queue != invocation.queue
    ):
        dispatch.status = NetworkOperationDispatchStatus.reconciliation_needed
        dispatch.completed_at = now or datetime.now(UTC)
        dispatch.last_error = "Stored command no longer matches its typed operation."
        _terminalize_operation(db, dispatch, dispatch.last_error)
        db.flush()
        return None

    current = now or datetime.now(UTC)
    dispatch.status = NetworkOperationDispatchStatus.acknowledged
    dispatch.acknowledged_at = current
    dispatch.last_error = None
    db.flush()
    return DispatchExecution(
        dispatch_id=str(dispatch.id),
        operation_id=str(operation.id),
        command_name=normalized.value,
        task_name=spec.task_name,
        args=stored_args,
        kwargs=stored_kwargs,
    )


def complete_dispatch_execution(
    db: Session,
    dispatch_id: str,
    *,
    now: datetime | None = None,
) -> None:
    dispatch = db.scalar(
        select(NetworkOperationDispatch)
        .where(NetworkOperationDispatch.id == UUID(str(dispatch_id)))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if dispatch is None:
        raise NetworkOperationDispatchError(
            "dispatch_not_found", "Network operation dispatch not found."
        )
    if dispatch.status == NetworkOperationDispatchStatus.acknowledged:
        dispatch.status = NetworkOperationDispatchStatus.completed
        dispatch.completed_at = now or datetime.now(UTC)
        dispatch.last_error = None
        db.flush()


def managed_network_operation_dispatch(
    task_name: str,
) -> Callable[[_CallableT], _CallableT]:
    """Wrap a registered device task with the durable execution claim."""

    def decorator(func: _CallableT) -> _CallableT:
        @wraps(func)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            dispatch_id = str(kwargs.get("_network_dispatch_id", "") or "").strip()
            if not dispatch_id:
                return func(*args, **kwargs)

            from app.services.db_session_adapter import db_session_adapter
            from app.services.observability import record_metric

            with db_session_adapter.session() as db:
                execution = claim_dispatch_execution(db, dispatch_id)
                if execution is not None and execution.task_name != task_name:
                    fail_dispatch_execution(
                        db,
                        dispatch_id,
                        f"Dispatch target mismatch: expected {task_name}.",
                    )
                    db.commit()
                    raise NetworkOperationDispatchError(
                        "dispatch_target_mismatch",
                        "Dispatch target does not match the registered task.",
                    )
                db.commit()
            if execution is None:
                return {
                    "dispatch_id": dispatch_id,
                    "executed": False,
                    "duplicate": True,
                }

            try:
                result = func(*args, **kwargs)
            except Exception as exc:
                with db_session_adapter.session() as db:
                    fail_dispatch_execution(db, dispatch_id, str(exc))
                    db.commit()
                record_metric(
                    domain="network_operations",
                    signal="dispatch_execution",
                    status="reconciliation_needed",
                )
                raise

            with db_session_adapter.session() as db:
                complete_dispatch_execution(db, dispatch_id)
                db.commit()
            record_metric(
                domain="network_operations",
                signal="dispatch_execution",
                status="completed",
            )
            return result

        return cast(_CallableT, wrapped)

    return decorator


def fail_dispatch_execution(
    db: Session,
    dispatch_id: str,
    error: str,
    *,
    now: datetime | None = None,
) -> None:
    dispatch = db.scalar(
        select(NetworkOperationDispatch)
        .where(NetworkOperationDispatch.id == UUID(str(dispatch_id)))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if dispatch is None:
        raise NetworkOperationDispatchError(
            "dispatch_not_found", "Network operation dispatch not found."
        )
    if dispatch.status == NetworkOperationDispatchStatus.acknowledged:
        dispatch.status = NetworkOperationDispatchStatus.reconciliation_needed
        dispatch.completed_at = now or datetime.now(UTC)
        dispatch.last_error = str(error or "Command worker failed.")[:4000]
        _terminalize_operation(
            db,
            dispatch,
            "Command worker exited unexpectedly. Review current device state before retrying.",
        )
        db.flush()


def reconcile_dispatches(
    db: Session,
    *,
    now: datetime | None = None,
    execution_timeout: timedelta = DEFAULT_EXECUTION_TIMEOUT,
    limit: int = 200,
) -> DispatchSweepResult:
    """Project terminal operations and expose interrupted worker execution."""
    current = now or datetime.now(UTC)
    cutoff = current - execution_timeout
    ids = list(
        db.scalars(
            select(NetworkOperationDispatch.id)
            .where(
                NetworkOperationDispatch.status
                == NetworkOperationDispatchStatus.acknowledged
            )
            .order_by(NetworkOperationDispatch.created_at.asc())
            .limit(max(1, min(int(limit), 1000)))
        ).all()
    )
    result = DispatchSweepResult()
    for dispatch_id in ids:
        dispatch = db.scalar(
            select(NetworkOperationDispatch)
            .where(NetworkOperationDispatch.id == dispatch_id)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        if dispatch is None:
            db.rollback()
            continue
        result.examined += 1
        operation = dispatch.operation
        if operation.status not in _ACTIVE_OPERATION_STATUSES:
            was_acknowledged = dispatch.acknowledged_at is not None
            dispatch.status = (
                NetworkOperationDispatchStatus.completed
                if was_acknowledged
                else NetworkOperationDispatchStatus.canceled
            )
            dispatch.completed_at = current
            if was_acknowledged:
                result.completed += 1
            else:
                result.canceled += 1
            db.commit()
            continue
        if (
            dispatch.status == NetworkOperationDispatchStatus.acknowledged
            and dispatch.acknowledged_at is not None
            and _as_aware_utc(dispatch.acknowledged_at) <= cutoff
        ):
            dispatch.status = NetworkOperationDispatchStatus.reconciliation_needed
            dispatch.completed_at = current
            dispatch.last_error = (
                "Worker acknowledgement became stale before command completion."
            )
            _terminalize_operation(
                db,
                dispatch,
                "Command execution state is unknown. Review current device state before retrying.",
            )
            result.reconciliation_needed += 1
            db.commit()
            continue
        db.rollback()
    return result


def health_snapshot(
    db: Session,
    *,
    now: datetime | None = None,
) -> dict[str, float]:
    current = now or datetime.now(UTC)
    snapshot = {
        f"dispatches_{status.value}": 0.0 for status in NetworkOperationDispatchStatus
    }
    rows = db.execute(
        select(
            NetworkOperationDispatch.status,
            func.count(NetworkOperationDispatch.id),
        ).group_by(NetworkOperationDispatch.status)
    ).all()
    for status, count in rows:
        value = (
            status.value
            if isinstance(status, NetworkOperationDispatchStatus)
            else str(status)
        )
        snapshot[f"dispatches_{value}"] = float(count)
    oldest_pending = db.scalar(
        select(func.min(NetworkOperationDispatch.created_at)).where(
            NetworkOperationDispatch.status == NetworkOperationDispatchStatus.pending
        )
    )
    snapshot["dispatch_oldest_pending_age_seconds"] = (
        max(0.0, (current - _as_aware_utc(oldest_pending)).total_seconds())
        if oldest_pending
        else 0.0
    )
    return snapshot


def operation_dispatch_summary(operation: NetworkOperation) -> dict[str, object] | None:
    """Project the latest transport state for operation-history surfaces."""
    if not operation.dispatches:
        return None
    dispatch = max(operation.dispatches, key=lambda item: item.created_at)
    return {
        "id": str(dispatch.id),
        "status": dispatch.status.value,
        "label": _DISPATCH_STATUS_LABELS[dispatch.status],
        "attempts": int(dispatch.attempts or 0),
        "max_attempts": int(dispatch.max_attempts or 0),
        "last_error": dispatch.last_error or "",
        "attention_required": dispatch.status
        in {
            NetworkOperationDispatchStatus.failed,
            NetworkOperationDispatchStatus.reconciliation_needed,
        },
    }
