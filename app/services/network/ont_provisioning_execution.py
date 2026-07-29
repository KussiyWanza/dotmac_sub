"""Execution owner for tracked ONT provisioning commands."""

from __future__ import annotations

import logging
import random
from typing import Any

from sqlalchemy import select

from app.models.network_operation import (
    NetworkOperation,
    NetworkOperationStatus,
    NetworkOperationType,
)

logger = logging.getLogger(__name__)
_retry_jitter_random = random.SystemRandom()


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


def execute_ont_authorization(
    db,
    *,
    olt_id: str,
    fsp: str,
    serial_number: str,
    force_reauthorize: bool = False,
    preset_id: str | None = None,
    initiated_by: str | None = None,
    operation_id: str,
) -> dict[str, Any]:
    """Execute one previously accepted authorization operation."""
    from app.services.network.ont_authorization import authorize_ont
    from app.services.network.ont_provisioning_commands import (
        request_bootstrap_verification,
    )
    from app.services.network_operations import network_operations

    network_operations.mark_running(db, operation_id)
    db.commit()

    result = authorize_ont(
        db,
        olt_id,
        fsp,
        serial_number,
        force_reauthorize=force_reauthorize,
        preset_id=preset_id,
        request=None,
        # Lets the authorization owner commit ``completed_authorization`` the
        # moment the OLT accepts the write, independently of this payload.
        operation_id=operation_id,
    )
    # Merge onto whatever the authorization owner already committed (notably the
    # landed ``device_authorization`` evidence) so terminalizing the operation
    # never erases the fact that the OLT write succeeded.
    operation = network_operations.get(db, operation_id)
    payload: dict[str, Any] = {**(operation.output_payload or {}), **result.to_dict()}
    payload["operation_id"] = operation_id
    follow_up = None

    if result.success and result.ont_unit_id:
        follow_up = request_bootstrap_verification(
            db,
            ont_id=result.ont_unit_id,
            parent_operation_id=operation_id,
            initiated_by=initiated_by,
        )
        payload["follow_up_operation_id"] = follow_up.operation_id
        payload["follow_up_dispatch_id"] = follow_up.dispatch_id
        payload["follow_up_queued"] = follow_up.accepted
        payload["follow_up_duplicate"] = follow_up.duplicate

    if result.success:
        if follow_up is not None and not follow_up.accepted:
            payload["status"] = "warning"
            payload["partial_success"] = True
            payload["message"] = (
                f"{result.message} TR-069 bootstrap follow-up failed: "
                f"{follow_up.message}"
            )
            network_operations.mark_warning(
                db,
                operation_id,
                str(payload["message"]),
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
    return payload


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
