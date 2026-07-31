"""Execution owner for tracked ONT provisioning commands."""

from __future__ import annotations

import logging
import random
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import select

from app.models.network_operation import (
    NetworkOperation,
    NetworkOperationStatus,
    NetworkOperationType,
)
from app.services.network.ont_authorization_contracts import (
    AuthorizationWorkflowStatus,
    ExecuteAssignedOntAuthorization,
)

if TYPE_CHECKING:
    from app.services.network.ont_authorization import AuthorizationWorkflowResult

logger = logging.getLogger(__name__)
_retry_jitter_random = random.SystemRandom()


@dataclass(frozen=True, slots=True)
class OntAuthorizationExecutionOutcome:
    """Typed result of one durable assigned-authorization execution."""

    authorization: AuthorizationWorkflowResult
    operation_id: UUID
    message: str
    status: AuthorizationWorkflowStatus
    partial_success: bool
    follow_up_operation_id: UUID | None = None
    follow_up_dispatch_id: UUID | None = None
    follow_up_queued: bool | None = None
    follow_up_duplicate: bool | None = None

    def to_dict(
        self,
        *,
        base: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        payload = dict(base or {})
        payload.update(self.authorization.to_dict())
        payload.update(
            {
                "operation_id": str(self.operation_id),
                "message": self.message,
                "status": self.status.value,
                "partial_success": self.partial_success,
            }
        )
        if self.follow_up_operation_id is not None:
            payload["follow_up_operation_id"] = str(self.follow_up_operation_id)
            payload["follow_up_dispatch_id"] = (
                str(self.follow_up_dispatch_id)
                if self.follow_up_dispatch_id is not None
                else None
            )
            payload["follow_up_queued"] = self.follow_up_queued
            payload["follow_up_duplicate"] = self.follow_up_duplicate
        return payload


def preview_ont_provisioning(db, ont_id: str):
    """Return a DB-only baseline preview through the provisioning owner."""
    from app.services.network.ont_provision_steps import apply_authorization_baseline

    return apply_authorization_baseline(db, ont_id, dry_run=True)


def sync_bootstrap_parent(
    db,
    *,
    operation_id: str,
    ont_id: str,
    payload: dict[str, object],
) -> None:
    """Project a bootstrap verifier outcome onto its parent and bulk item."""
    from app.services.network_operations import network_operations

    operation = network_operations.get(db, operation_id)
    if not operation.parent_id:
        return

    parent = network_operations.update_parent_status(db, str(operation.parent_id))
    parent.output_payload = {
        **(parent.output_payload or {}),
        "device_confirmation": payload,
        "waiting": parent.status.value in {"pending", "running", "waiting"},
    }
    bulk_item_id = (parent.input_payload or {}).get("bulk_item_id")
    if not bulk_item_id or parent.status.value not in {
        "succeeded",
        "warning",
        "failed",
    }:
        return

    from app.services.network.bulk_provisioning import mark_bulk_item_completed

    confirmed = parent.status.value == "succeeded"
    mark_bulk_item_completed(
        db,
        str(bulk_item_id),
        {
            "success": confirmed,
            "waiting": False,
            "message": str(payload.get("message") or ""),
            "ont_id": ont_id,
            "operation_id": str(parent.id),
            "confirmation_operation_id": operation_id,
            "device_confirmation": payload,
        },
    )


def complete_waiting_bootstrap_after_inform(
    db,
    *,
    ont_id: str,
    result,
    reason: str,
) -> bool:
    """Close a waiting bootstrap operation after saved intent applies on Inform."""
    from app.services.network_operations import network_operations

    bootstrap_operation = db.scalars(
        select(NetworkOperation)
        .where(
            NetworkOperation.operation_type == NetworkOperationType.tr069_bootstrap,
            NetworkOperation.target_id == ont_id,
            NetworkOperation.status.in_(
                {
                    NetworkOperationStatus.pending,
                    NetworkOperationStatus.running,
                    NetworkOperationStatus.waiting,
                }
            ),
        )
        .order_by(NetworkOperation.created_at.desc())
        .limit(1)
    ).first()
    if bootstrap_operation is None:
        return False

    confirmation_payload = {
        "success": True,
        "waiting": False,
        "message": result.message,
        "service_config": {
            "step_name": result.step_name,
            "success": result.success,
            "message": result.message,
            "duration_ms": result.duration_ms,
            "waiting": result.waiting,
            "skipped": result.skipped,
            "data": result.data or {},
        },
        "confirmation_source": reason,
    }
    network_operations.mark_succeeded(
        db,
        str(bootstrap_operation.id),
        output_payload=confirmation_payload,
    )
    sync_bootstrap_parent(
        db,
        operation_id=str(bootstrap_operation.id),
        ont_id=ont_id,
        payload=confirmation_payload,
    )
    return True


def fail_unexecutable_authorization(
    db,
    *,
    operation_id: UUID,
    message: str,
) -> OntAuthorizationExecutionOutcome:
    """Terminalize an accepted operation the typed contract can never execute.

    An operation admitted before the assigned-authorization contract existed
    can lack the ONT that contract requires. Refusing it by raising would leave
    it non-terminal forever — invisible work that looks queued rather than
    dead — so it fails closed here on the same path a rejected assignment
    decision uses.
    """
    from app.services.network.ont_authorization import AuthorizationWorkflowResult
    from app.services.network_operations import network_operations

    tracked_id = str(operation_id)
    operation = network_operations.get(db, tracked_id)
    outcome = OntAuthorizationExecutionOutcome(
        authorization=AuthorizationWorkflowResult(
            success=False,
            message=message,
            status=AuthorizationWorkflowStatus.ERROR,
        ),
        operation_id=operation_id,
        message=message,
        status=AuthorizationWorkflowStatus.ERROR,
        partial_success=False,
    )
    network_operations.mark_failed(
        db,
        tracked_id,
        message,
        output_payload=outcome.to_dict(base=operation.output_payload or {}),
    )
    db.commit()
    return outcome


def execute_ont_authorization(
    db,
    command: ExecuteAssignedOntAuthorization,
) -> OntAuthorizationExecutionOutcome:
    """Execute one typed authorization command after fresh assignment admission."""
    from app.services.network.ont_authorization import (
        AuthorizationWorkflowResult,
        authorize_and_provision_ont,
    )
    from app.services.network.ont_provisioning_commands import (
        evaluate_assigned_authorization,
        request_bootstrap_verification,
    )
    from app.services.network_operations import network_operations

    operation_id = str(command.operation_id)
    operation = network_operations.get(db, operation_id)
    decision = evaluate_assigned_authorization(
        db,
        ont_id=command.ont_id,
        target=command.target,
    )
    if not decision.allowed:
        authorization = AuthorizationWorkflowResult(
            success=False,
            message=decision.message,
            status=AuthorizationWorkflowStatus.ERROR,
        )
        outcome = OntAuthorizationExecutionOutcome(
            authorization=authorization,
            operation_id=command.operation_id,
            message=decision.message,
            status=AuthorizationWorkflowStatus.ERROR,
            partial_success=False,
        )
        network_operations.mark_failed(
            db,
            operation_id,
            decision.message,
            output_payload=outcome.to_dict(base=operation.output_payload or {}),
        )
        db.commit()
        return outcome

    network_operations.mark_running(db, operation_id)
    db.commit()

    result = authorize_and_provision_ont(db, command)
    # Merge onto whatever the authorization owner already committed (notably the
    # landed ``device_authorization`` evidence) so terminalizing the operation
    # never erases the fact that the OLT write succeeded.
    operation = network_operations.get(db, operation_id)
    follow_up = None
    message = result.message
    status = result.status
    partial_success = result.partial_success

    if result.success and result.ont_unit_id:
        follow_up = request_bootstrap_verification(
            db,
            ont_id=str(result.ont_unit_id),
            parent_operation_id=operation_id,
            initiated_by=command.context.actor,
        )

    if result.success:
        if follow_up is not None and not follow_up.accepted:
            status = AuthorizationWorkflowStatus.WARNING
            partial_success = True
            message = (
                f"{result.message} TR-069 bootstrap follow-up failed: "
                f"{follow_up.message}"
            )
    outcome = OntAuthorizationExecutionOutcome(
        authorization=result,
        operation_id=command.operation_id,
        message=message,
        status=status,
        partial_success=partial_success,
        follow_up_operation_id=(
            UUID(follow_up.operation_id)
            if follow_up is not None and follow_up.operation_id is not None
            else None
        ),
        follow_up_dispatch_id=(
            UUID(follow_up.dispatch_id)
            if follow_up is not None and follow_up.dispatch_id is not None
            else None
        ),
        follow_up_queued=follow_up.accepted if follow_up is not None else None,
        follow_up_duplicate=follow_up.duplicate if follow_up is not None else None,
    )
    payload = outcome.to_dict(base=operation.output_payload or {})

    if result.success:
        if follow_up is not None and not follow_up.accepted:
            network_operations.mark_warning(
                db,
                operation_id,
                message,
                output_payload=payload,
            )
        else:
            parent = network_operations.update_parent_status(db, operation_id)
            parent.output_payload = payload
            db.flush()
    elif result.partial_success:
        network_operations.mark_warning(
            db,
            operation_id,
            result.message,
            output_payload=payload,
        )
    else:
        network_operations.mark_failed(
            db,
            operation_id,
            result.message,
            output_payload=payload,
        )
    db.commit()
    return outcome


def execute_ont_provisioning(
    db,
    *,
    ont_id: str,
    dry_run: bool,
    initiated_by: str | None,
    correlation_key: str,
    bulk_run_id: str | None,
    bulk_item_id: str | None,
    allow_low_optical_margin: bool,
    operation_id: str | None,
) -> dict[str, Any]:
    """Execute one baseline preview or previously accepted repair operation."""
    from app.services.network.ont_provision_steps import apply_authorization_baseline
    from app.services.network.provisioning_events import provisioning_correlation
    from app.services.network_operations import network_operations

    if bulk_item_id:
        from app.services.network.bulk_provisioning import mark_bulk_item_running

        mark_bulk_item_running(db, bulk_item_id)
        db.commit()
    if operation_id:
        network_operations.mark_running(db, operation_id)
        db.commit()

    with provisioning_correlation(correlation_key):
        result = apply_authorization_baseline(
            db,
            ont_id,
            dry_run=dry_run,
            allow_low_optical_margin=allow_low_optical_margin,
        )
    payload = {
        "success": result.success,
        "message": result.message,
        "ont_id": ont_id,
        "duration_ms": result.duration_ms,
        "step_name": result.step_name,
        "bulk_run_id": bulk_run_id,
        "bulk_item_id": bulk_item_id,
        "correlation_key": correlation_key,
        "operation_id": operation_id,
        "waiting": result.waiting,
        "data": result.data,
    }
    if operation_id:
        if result.waiting:
            waiting_reason = (result.data or {}).get("waiting_reason") or result.message
            operation = network_operations.mark_waiting(
                db,
                operation_id,
                str(waiting_reason),
            )
            operation.output_payload = payload
            db.flush()

            from app.services.network.ont_provisioning_commands import (
                request_bootstrap_verification,
            )

            follow_up = request_bootstrap_verification(
                db,
                ont_id=ont_id,
                parent_operation_id=operation_id,
                initiated_by=initiated_by,
            )
            payload["follow_up_operation_id"] = follow_up.operation_id
            payload["follow_up_dispatch_id"] = follow_up.dispatch_id
            payload["follow_up_queued"] = follow_up.accepted
            payload["follow_up_duplicate"] = follow_up.duplicate
            if follow_up.accepted:
                parent = network_operations.update_parent_status(db, operation_id)
                parent.output_payload = payload
                db.flush()
            else:
                payload["success"] = False
                payload["waiting"] = False
                payload["partial_success"] = True
                payload["message"] = (
                    f"{result.message} Bootstrap verification could not be scheduled: "
                    f"{follow_up.message}"
                )
                network_operations.mark_warning(
                    db,
                    operation_id,
                    str(payload["message"]),
                    output_payload=payload,
                )
        elif result.success:
            network_operations.mark_succeeded(
                db,
                operation_id,
                output_payload=payload,
            )
        else:
            network_operations.mark_failed(
                db,
                operation_id,
                result.message,
                output_payload=payload,
            )
        db.commit()
    if bulk_item_id:
        from app.services.network.bulk_provisioning import mark_bulk_item_completed

        mark_bulk_item_completed(db, bulk_item_id, payload)
        db.commit()
    return payload


def execute_ont_provisioning_command(
    db,
    *,
    ont_id: str,
    initiated_by: str | None,
    correlation_key: str,
    bulk_run_id: str | None,
    bulk_item_id: str | None,
    allow_low_optical_margin: bool,
    operation_id: str,
) -> dict[str, Any]:
    """Execute a claimed repair command and preserve bulk failure state."""
    try:
        return execute_ont_provisioning(
            db,
            ont_id=ont_id,
            dry_run=False,
            initiated_by=initiated_by,
            correlation_key=correlation_key,
            bulk_run_id=bulk_run_id,
            bulk_item_id=bulk_item_id,
            allow_low_optical_margin=allow_low_optical_margin,
            operation_id=operation_id,
        )
    except Exception as exc:
        db.rollback()
        if bulk_item_id:
            from app.services.network.bulk_provisioning import mark_bulk_item_failed

            mark_bulk_item_failed(db, bulk_item_id, str(exc))
            db.commit()
        raise


def execute_bootstrap_verification(
    db,
    *,
    ont_id: str,
    operation_id: str,
    service_retry_count: int,
) -> dict[str, object]:
    """Execute one bootstrap readback and stage any required delayed retry."""
    from app.services.network.ont_provision_steps import (
        apply_saved_service_config,
        wait_tr069_bootstrap,
    )
    from app.services.network.ont_provisioning_commands import stage_bootstrap_attempt
    from app.services.network_operation_dispatch import NetworkOperationDispatchError
    from app.services.network_operations import network_operations

    existing_operation = network_operations.get(db, operation_id)
    if existing_operation.status.value in {
        "succeeded",
        "warning",
        "failed",
        "canceled",
    }:
        return dict(
            existing_operation.output_payload
            or {
                "success": existing_operation.status.value == "succeeded",
                "waiting": False,
                "message": existing_operation.error
                or "Bootstrap operation already completed.",
            }
        )
    network_operations.mark_running(db, operation_id)
    db.commit()

    result = wait_tr069_bootstrap(db, ont_id, allow_blocking=True)
    apply_result = apply_saved_service_config(db, ont_id) if result.success else None
    service_waiting = bool(apply_result.waiting) if apply_result else False
    payload: dict[str, object] = {
        "step_name": result.step_name,
        "success": result.success
        and (apply_result.success if apply_result else True)
        and not service_waiting,
        "message": result.message,
        "duration_ms": result.duration_ms,
        "waiting": result.waiting or service_waiting,
        "data": result.data or {},
    }
    if apply_result is not None:
        payload["service_config"] = {
            "step_name": apply_result.step_name,
            "success": apply_result.success,
            "message": apply_result.message,
            "duration_ms": apply_result.duration_ms,
            "waiting": apply_result.waiting,
            "skipped": apply_result.skipped,
            "data": apply_result.data or {},
        }
        if apply_result.message:
            payload["message"] = f"{result.message} {apply_result.message}"

    if payload["success"]:
        network_operations.mark_succeeded(db, operation_id, output_payload=payload)
    elif payload["waiting"]:
        operation = network_operations.mark_waiting(
            db,
            operation_id,
            str(payload["message"]),
        )
        if service_retry_count < 4:
            retry_delays = [30, 60, 120, 240]
            base_countdown = retry_delays[
                min(service_retry_count, len(retry_delays) - 1)
            ]
            jitter = _retry_jitter_random.uniform(-0.1, 0.1) * base_countdown
            countdown = int(base_countdown + jitter)
            logger.info(
                "Scheduling TR-069 bootstrap retry %d for ONT %s in %ds",
                service_retry_count + 1,
                ont_id,
                countdown,
            )
            try:
                retry_dispatch = stage_bootstrap_attempt(
                    db,
                    operation,
                    attempt=service_retry_count + 1,
                    delay_seconds=countdown,
                )
                payload["retry_dispatch_id"] = str(retry_dispatch.id)
            except NetworkOperationDispatchError as exc:
                payload["waiting"] = False
                payload["success"] = False
                payload["message"] = (
                    f"TR-069 verification retry could not be scheduled: {exc.message}"
                )
                network_operations.mark_failed(
                    db,
                    operation_id,
                    str(payload["message"]),
                    output_payload=payload,
                )
        else:
            payload["message"] = (
                f"{payload['message']} Active retries exhausted; saved intent "
                "remains pending and will be retried on the next Inform."
            )
    else:
        network_operations.mark_failed(
            db,
            operation_id,
            str(payload["message"]),
            output_payload=payload,
        )
    sync_bootstrap_parent(
        db,
        operation_id=operation_id,
        ont_id=ont_id,
        payload=payload,
    )
    db.commit()
    return payload
