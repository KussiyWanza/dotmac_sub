"""Web service helpers for network operation history display."""

from __future__ import annotations

import logging
import secrets
from typing import TYPE_CHECKING, Any

from app.models.network_operation import (
    NetworkOperationTargetType,
)
from app.services.control_plane_intent import phase_for_network_operation
from app.services.network.ont_authorization import LOCAL_INVENTORY_FAILED_HEADLINE
from app.services.network_operation_dispatch import operation_dispatch_summary
from app.services.network_operation_recovery import review_redrive
from app.services.network_operations import network_operations

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

OPERATION_DISPLAY: dict[str, str] = {
    "olt_ont_sync": "OLT ONT Discovery",
    "olt_pon_repair": "PON Port Repair",
    "olt_firmware_upgrade": "OLT Firmware Upgrade",
    "ont_provision": "ONT Provision",
    "ont_authorize": "ONT Authorize",
    "ont_reboot": "ONT Reboot",
    "ont_factory_reset": "ONT Factory Reset",
    "ont_set_pppoe": "Set PPPoE Credentials",
    "ont_set_conn_request_creds": "Set Connection Request Credentials",
    "ont_send_conn_request": "Connection Request",
    "ont_enable_ipv6": "Enable IPv6",
    "ont_firmware_upgrade": "ONT Firmware Upgrade",
    "ont_return_to_inventory": "Return ONT to Inventory",
    "ont_decommission": "Decommission ONT",
    "cpe_set_conn_request_creds": "Set Connection Request Credentials",
    "cpe_send_conn_request": "Connection Request",
    "cpe_reboot": "CPE Reboot",
    "cpe_factory_reset": "CPE Factory Reset",
    "tr069_bootstrap": "TR-069 Bootstrap",
    "wifi_update": "Wi-Fi Configuration",
    "pppoe_push": "PPPoE Push",
}


def _operation_title(op: Any) -> str:
    op_type_val = op.operation_type.value if op.operation_type else ""
    input_payload = getattr(op, "input_payload", None) or {}
    if (
        op_type_val == "olt_ont_sync"
        and isinstance(input_payload, dict)
        and input_payload.get("action") == "status_refresh"
    ):
        return "ONT Status Refresh"
    payload = getattr(op, "output_payload", None) or {}
    if (
        op_type_val == "olt_ont_sync"
        and isinstance(payload, dict)
        and payload.get("mode") == "pon_port_repair"
    ):
        return "PON Port Repair"
    return OPERATION_DISPLAY.get(op_type_val, op_type_val)


STATUS_CLASSES: dict[str, str] = {
    "pending": "bg-blue-100 text-blue-800 ring-1 ring-blue-200 dark:bg-blue-400/15 dark:text-blue-50 dark:ring-blue-300/40",
    "running": "bg-blue-100 text-blue-800 ring-1 ring-blue-200 dark:bg-blue-400/15 dark:text-blue-50 dark:ring-blue-300/40",
    "waiting": "bg-amber-100 text-amber-800 ring-1 ring-amber-200 dark:bg-amber-400/15 dark:text-amber-50 dark:ring-amber-300/40",
    "succeeded": "bg-emerald-100 text-emerald-800 ring-1 ring-emerald-200 dark:bg-emerald-400/15 dark:text-emerald-50 dark:ring-emerald-300/40",
    "warning": "bg-amber-100 text-amber-800 ring-1 ring-amber-200 dark:bg-amber-400/15 dark:text-amber-50 dark:ring-amber-300/40",
    "failed": "bg-rose-100 text-rose-800 ring-1 ring-rose-200 dark:bg-rose-400/15 dark:text-rose-50 dark:ring-rose-300/40",
    "canceled": "bg-slate-100 text-slate-700 ring-1 ring-slate-200 dark:bg-slate-400/15 dark:text-slate-50 dark:ring-slate-300/30",
}

STATUS_DISPLAY: dict[str, str] = {
    "pending": "Pending",
    "running": "Running",
    "waiting": "Waiting",
    "succeeded": "Succeeded",
    "warning": "Warning",
    "failed": "Failed",
    "canceled": "Canceled",
}


def _partial_outcome(op: Any) -> dict[str, Any]:
    """Project the stored partial-success classification for the operator.

    The authorization owner is the single writer of these flags; this only
    reads them so "the OLT authorized the ONT but the local projection failed"
    can never render as a generic failure.
    """
    payload = getattr(op, "output_payload", None)
    if not isinstance(payload, dict):
        return {
            "device_authorization_completed": False,
            "local_inventory_failed": False,
            "partial_success_headline": None,
            "device_message": None,
            "device_authorization_reused_from": None,
        }
    local_inventory_failed = bool(payload.get("local_inventory_failed"))
    return {
        "device_authorization_completed": bool(payload.get("completed_authorization")),
        "local_inventory_failed": local_inventory_failed,
        "partial_success_headline": (
            LOCAL_INVENTORY_FAILED_HEADLINE if local_inventory_failed else None
        ),
        "device_message": str(payload.get("device_message") or "") or None,
        "device_authorization_reused_from": (
            str(payload.get("device_authorization_reused_from") or "") or None
        ),
    }


def _format_duration(op: Any) -> str | None:
    """Format the duration between started_at and completed_at."""
    if not op.started_at or not op.completed_at:
        return None
    delta = op.completed_at - op.started_at
    total_seconds = int(delta.total_seconds())
    if total_seconds < 1:
        return "<1s"
    if total_seconds < 60:
        return f"{total_seconds}s"
    minutes, seconds = divmod(total_seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def build_operation_history(
    db: Session,
    target_type: str,
    target_id: str,
    *,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Build formatted operation history for a device.

    Args:
        db: Database session.
        target_type: Device type string ("olt", "ont", "cpe").
        target_id: Device UUID string.
        limit: Maximum number of records.

    Returns:
        List of dicts ready for template rendering.
    """
    try:
        target_enum = NetworkOperationTargetType(target_type)
    except ValueError:
        logger.warning("Unknown target_type %r for operation history", target_type)
        return []
    try:
        if network_operations.mark_stale_for_device(db, target_enum, target_id):
            db.commit()
    except Exception:
        db.rollback()
        logger.exception(
            "Failed to mark stale network operations for %s %s",
            target_type,
            target_id,
        )
    ops = network_operations.list_for_device(db, target_enum, target_id, limit=limit)

    result: list[dict[str, Any]] = []
    for op in ops:
        status_val = op.status.value if op.status else "pending"
        op_type_val = op.operation_type.value if op.operation_type else ""

        redrive_review = review_redrive(db, op)
        dispatch_summary = operation_dispatch_summary(op)
        partial_outcome = _partial_outcome(op)
        entry: dict[str, Any] = {
            **partial_outcome,
            "id": str(op.id),
            "title": _operation_title(op),
            "status": STATUS_DISPLAY.get(status_val, status_val),
            "status_value": status_val,
            "control_plane_phase": phase_for_network_operation(op.status).value,
            "status_class": STATUS_CLASSES.get(status_val, STATUS_CLASSES["pending"]),
            "is_running": status_val in ("running", "pending"),
            "is_waiting": status_val == "waiting",
            "is_failed": status_val == "failed",
            "is_warning": status_val == "warning",
            "message": op.error or op.waiting_reason or "",
            "initiated_by": op.initiated_by or "",
            "occurred_at": op.created_at,
            "duration": _format_duration(op),
            "can_retry": redrive_review.eligible,
            "retry_reason": redrive_review.message,
            "redrive_expected_head": redrive_review.expected_head,
            "redrive_idempotency_key": (
                secrets.token_urlsafe(24) if redrive_review.eligible else None
            ),
            "redrive_action_label": redrive_review.action_label,
            "redrive_of_id": str(op.redrive_of_id) if op.redrive_of_id else None,
            "redrive_reason": op.redrive_reason,
            "retry_count": int(op.retry_count or 0),
            "max_retries": int(op.max_retries or 0),
            "dispatch": dispatch_summary,
            "operation_type": op_type_val,
            "target_type": target_type,
            "target_id": target_id,
        }
        result.append(entry)

    return result
