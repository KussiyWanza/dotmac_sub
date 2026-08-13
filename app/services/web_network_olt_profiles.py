"""Web service for OLT profile display (line, service, TR-069, WAN profiles)."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.models.audit import AuditEvent
from app.models.catalog import CatalogOffer
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.network import (
    OLTDevice,
    OltLineProfile,
    OltLineProfileGemMapping,
    OltOnuTypeProfileMapping,
    OltProfileBundle,
    OltProfileSyncTask,
    OltServicePort,
    OltServiceProfile,
    OntProvisioningProfile,
)
from app.services.audit_helpers import (
    humanize_action,
    log_audit_event,
    resolve_actor_display_names,
)
from app.services.network import olt as olt_service
from app.services.network.imported_service_ports import imported_service_port_summary
from app.services.network.olt_command_gen import (
    HuaweiCommandGenerator,
    OltCommandSet,
    OntProvisioningContext,
    build_spec_from_profile,
)
from app.services.network.olt_config_snapshot_reader import (
    BackupRunner as SnapshotBackupRunner,
)
from app.services.network.olt_config_snapshot_reader import OltConfigSnapshotReader
from app.services.network.olt_web_audit import log_olt_audit_event
from app.services.network.ont_provisioning.credentials import mask_credentials
from app.services.network.profile_apply_workflow import (
    BackupRunner,
    CommandExecutor,
    ProfileCommandGroup,
    apply_profile_bundle,
    build_profile_apply_plan,
)
from app.services.network.profile_inventory_preflight import (
    validate_dotmac_profile_apply_plan,
    validate_offer_profile_sync_plan_inventory,
)
from app.services.network.profile_sync import (
    OfferProfileBundle,
    OfferProfileSyncError,
    OfferProfileSyncPlan,
    OfferProfileSyncTaskError,
    approve_profile_sync_task,
    build_offer_profile_sync_plan,
    cancel_profile_sync_task,
    list_due_profile_sync_tasks,
    list_profile_sync_tasks,
    list_syncable_catalog_offers,
    resolve_profile_bundle_for_offer,
    retry_profile_sync_task,
    upsert_profile_bundle,
)
from app.services.olt_profile_adapter import olt_profile_adapter
from app.services.web_network_service_ports import _resolve_ont_olt_context

logger = logging.getLogger(__name__)

PROFILE_SYNC_TASK_STATUS_FILTERS = {
    "open": ("pending", "approved", "scheduled"),
    "pending": ("pending",),
    "approved": ("approved",),
    "scheduled": ("scheduled",),
    "done": ("completed", "failed", "cancelled"),
}

_PROFILE_NAME_RE = re.compile(r'profile-name\s+"(?P<name>[^"]+)"', re.IGNORECASE)
_TRAFFIC_NAME_RE = re.compile(r'name\s+"(?P<name>[^"]+)"', re.IGNORECASE)


def line_profiles_context(db: Session, olt_id: str) -> dict[str, Any]:
    """Fetch OLT line and service profiles through the profile adapter."""
    return olt_profile_adapter.line_profiles_context(db, olt_id)


def tr069_profiles_context(db: Session, olt_id: str) -> dict[str, Any]:
    """Fetch OLT TR-069 server profiles through the profile adapter."""
    return olt_profile_adapter.tr069_profiles_context(db, olt_id)


def imported_profile_state_context(db: Session, olt_id: str) -> dict[str, Any]:
    """Return imported OLT profile state from DB source-of-truth tables."""
    olt = db.get(OLTDevice, olt_id)
    if olt is None:
        return {
            "olt": None,
            "line_profiles": [],
            "service_profiles": [],
            "profile_mappings": [],
            "error": "OLT not found",
        }

    line_profiles = list(
        db.scalars(
            select(OltLineProfile)
            .where(OltLineProfile.olt_id == olt.id)
            .order_by(OltLineProfile.profile_id)
        )
    )
    service_profiles = list(
        db.scalars(
            select(OltServiceProfile)
            .where(OltServiceProfile.olt_id == olt.id)
            .order_by(OltServiceProfile.profile_id)
        )
    )
    profile_mappings = list(
        db.scalars(
            select(OltOnuTypeProfileMapping)
            .where(OltOnuTypeProfileMapping.olt_id == olt.id)
            .order_by(OltOnuTypeProfileMapping.equipment_id)
        )
    )
    gem_mappings = list(
        db.scalars(
            select(OltLineProfileGemMapping)
            .where(OltLineProfileGemMapping.olt_id == olt.id)
            .order_by(
                OltLineProfileGemMapping.line_profile_id,
                OltLineProfileGemMapping.source,
                OltLineProfileGemMapping.vlan_id,
                OltLineProfileGemMapping.gem_index,
            )
        )
    )
    service_ports = list(
        db.scalars(
            select(OltServicePort)
            .where(OltServicePort.olt_device_id == olt.id)
            .order_by(OltServicePort.port_index)
            .limit(100)
        )
    )
    profile_bundles = list(
        db.scalars(
            select(OltProfileBundle)
            .where(OltProfileBundle.olt_id == olt.id)
            .order_by(OltProfileBundle.name)
        )
    )
    service_port_summary = imported_service_port_summary(db, olt_id=olt.id)
    return {
        "olt": olt,
        "line_profiles": line_profiles,
        "service_profiles": service_profiles,
        "profile_mappings": profile_mappings,
        "gem_mappings": gem_mappings,
        "service_ports": service_ports,
        "profile_bundles": profile_bundles,
        "service_port_summary": service_port_summary,
        "syncable_catalog_offers": list_syncable_catalog_offers(db),
        "error": None,
    }


def profile_sync_tasks_context(
    db: Session,
    *,
    status: str = "open",
    limit: int = 100,
) -> dict[str, Any]:
    """Build the admin review queue for tariff-driven OLT profile sync tasks."""
    selected_status = str(status or "open").strip().lower()
    statuses = PROFILE_SYNC_TASK_STATUS_FILTERS.get(selected_status)
    if selected_status == "all":
        statuses = None
    elif statuses is None:
        selected_status = "open"
        statuses = PROFILE_SYNC_TASK_STATUS_FILTERS[selected_status]

    tasks = list_profile_sync_tasks(db, statuses=statuses, limit=limit)
    due_tasks = list_due_profile_sync_tasks(db, limit=100)
    status_rows = db.execute(
        select(OltProfileSyncTask.status, func.count(OltProfileSyncTask.id)).group_by(
            OltProfileSyncTask.status
        )
    ).all()
    status_counts = {str(status): int(count) for status, count in status_rows}
    drift_rows = db.execute(
        select(OltProfileBundle.drift_status, func.count(OltProfileBundle.id))
        .where(OltProfileBundle.is_active.is_(True))
        .group_by(OltProfileBundle.drift_status)
    ).all()
    drift_counts = {
        str(status or "unknown"): int(count) for status, count in drift_rows
    }
    audit_events = list(
        db.scalars(
            select(AuditEvent)
            .where(
                AuditEvent.entity_type.in_(
                    ("olt_profile_sync_task", "olt_profile_bundle")
                )
            )
            .where(AuditEvent.is_active.is_(True))
            .order_by(AuditEvent.occurred_at.desc())
            .limit(25)
        )
    )
    actor_labels = resolve_actor_display_names(
        db,
        {
            actor
            for actor in (
                *(event.actor_id for event in audit_events),
                *(task.requested_by for task in tasks),
                *(task.approved_by for task in tasks),
            )
            if actor
        },
    )
    return {
        "tasks": tasks,
        "selected_status": selected_status,
        "status_counts": status_counts,
        "open_count": sum(
            status_counts.get(status, 0)
            for status in PROFILE_SYNC_TASK_STATUS_FILTERS["open"]
        ),
        "pending_count": status_counts.get("pending", 0),
        "approved_count": status_counts.get("approved", 0),
        "scheduled_count": status_counts.get("scheduled", 0),
        "due_count": len(due_tasks),
        "task_count": len(tasks),
        "worker_enabled": _profile_sync_worker_enabled(db),
        "worker_interval_seconds": _profile_sync_worker_interval_seconds(db),
        "profile_bundle_count": sum(drift_counts.values()),
        "drift_counts": drift_counts,
        "audit_events": audit_events,
        "actor_labels": actor_labels,
        "audit_action_labels": {
            str(event.id): humanize_action(event.action) for event in audit_events
        },
    }


def approve_profile_sync_task_from_form(
    db: Session,
    *,
    task_id: str,
    approved_by: str | None,
    scheduled_for_raw: str | None = None,
    request: Request | None = None,
) -> tuple[bool, str]:
    """Approve or schedule a pending sync task without executing OLT commands."""
    actor = str(approved_by or "").strip()
    if not actor:
        return False, "Cannot approve profile sync task without an actor"
    try:
        scheduled_for = _parse_scheduled_for(scheduled_for_raw)
    except ValueError:
        return False, "Scheduled time is invalid"
    try:
        task = approve_profile_sync_task(
            db,
            task_id=task_id,
            approved_by=actor,
            scheduled_for=scheduled_for,
        )
    except OfferProfileSyncTaskError as exc:
        return False, str(exc)
    _log_profile_sync_task_audit(
        db,
        request=request,
        action="olt_profile_sync_task_scheduled"
        if task.status == "scheduled"
        else "olt_profile_sync_task_approved",
        task=task,
        actor_id=actor,
        metadata={
            "scheduled_for": task.scheduled_for.isoformat()
            if task.scheduled_for
            else None,
        },
    )
    db.commit()
    if task.status == "scheduled" and task.scheduled_for is not None:
        return True, f"Scheduled profile sync task for {task.scheduled_for.isoformat()}"
    return True, "Approved profile sync task"


def cancel_profile_sync_task_from_form(
    db: Session,
    *,
    task_id: str,
    cancelled_by: str | None,
    reason: str | None = None,
    request: Request | None = None,
) -> tuple[bool, str]:
    """Cancel an open sync task without executing OLT commands."""
    actor = str(cancelled_by or "").strip()
    if not actor:
        return False, "Cannot cancel profile sync task without an actor"
    try:
        task = cancel_profile_sync_task(
            db,
            task_id=task_id,
            cancelled_by=actor,
            reason=reason,
        )
    except OfferProfileSyncTaskError as exc:
        return False, str(exc)
    _log_profile_sync_task_audit(
        db,
        request=request,
        action="olt_profile_sync_task_cancelled",
        task=task,
        actor_id=actor,
        metadata={"reason": str(reason or "").strip() or None},
    )
    db.commit()
    return True, "Cancelled profile sync task"


def retry_profile_sync_task_from_form(
    db: Session,
    *,
    task_id: str,
    retried_by: str | None,
    reason: str | None = None,
    request: Request | None = None,
) -> tuple[bool, str]:
    """Return a failed sync task to pending review."""
    actor = str(retried_by or "").strip()
    if not actor:
        return False, "Cannot retry profile sync task without an actor"
    try:
        task = retry_profile_sync_task(
            db,
            task_id=task_id,
            retried_by=actor,
            reason=reason,
        )
    except OfferProfileSyncTaskError as exc:
        return False, str(exc)
    _log_profile_sync_task_audit(
        db,
        request=request,
        action="olt_profile_sync_task_retried",
        task=task,
        actor_id=actor,
        metadata={"reason": str(reason or "").strip() or None},
    )
    db.commit()
    return True, "Profile sync task returned to pending review"


def execute_profile_sync_task(
    db: Session,
    *,
    task_id: str,
    executed_by: str | None,
    actor_is_admin: bool,
    request: Request | None = None,
    now: datetime | None = None,
    backup_runner: BackupRunner | None = None,
    command_executor: CommandExecutor | None = None,
) -> tuple[bool, str]:
    """Execute an approved or due scheduled task through the bundle apply workflow."""
    actor = str(executed_by or "").strip()
    if not actor:
        return False, "Cannot execute profile sync task without an actor"
    if not actor_is_admin:
        return False, "Only admin users can execute OLT profile sync tasks"

    task = db.get(OltProfileSyncTask, task_id)
    if task is None:
        return False, "Profile sync task not found"
    current_status = str(task.status or "")
    if current_status not in {"approved", "scheduled"}:
        return (
            False,
            f"Only approved or scheduled profile sync tasks can execute, got {current_status}",
        )
    now_value = now or datetime.now(UTC)
    scheduled_for = task.scheduled_for
    if scheduled_for is not None and scheduled_for.tzinfo is None:
        scheduled_for = scheduled_for.replace(tzinfo=UTC)
    if current_status == "scheduled" and scheduled_for and scheduled_for > now_value:
        return False, f"Profile sync task is scheduled for {scheduled_for.isoformat()}"

    bundle = resolve_profile_bundle_for_offer(
        db,
        olt_id=task.olt_id,
        offer_id=task.offer_id,
    )
    if bundle is None:
        task.status = "failed"
        task.error = "No active profile bundle found for task"
        task.result_payload = _task_result_payload(task, executed_by=actor)
        _log_profile_sync_task_audit(
            db,
            request=request,
            action="olt_profile_sync_task_failed",
            task=task,
            actor_id=actor,
            metadata={"error": task.error},
            is_success=False,
        )
        db.commit()
        return False, task.error

    task.status = "running"
    task.error = None
    task.result_payload = _task_result_payload(task, executed_by=actor)
    db.flush()
    result = apply_saved_profile_bundle(
        db,
        str(task.olt_id),
        str(bundle.id),
        actor_is_admin=True,
        backup_runner=backup_runner,
        command_executor=command_executor,
    )
    if result.get("ok"):
        task.status = "completed"
        task.error = None
        task.result_payload = _task_result_payload(
            task,
            executed_by=actor,
            bundle_id=str(bundle.id),
            apply_result=result.get("apply_result"),
        )
        _log_profile_sync_task_audit(
            db,
            request=request,
            action="olt_profile_sync_task_completed",
            task=task,
            actor_id=actor,
            metadata={"bundle_id": str(bundle.id)},
        )
        db.commit()
        return True, str(result.get("message") or "Profile sync task completed")

    task.status = "failed"
    task.error = str(result.get("message") or "Profile sync task failed")
    task.result_payload = _task_result_payload(
        task,
        executed_by=actor,
        bundle_id=str(bundle.id),
        apply_result=result.get("apply_result"),
    )
    _log_profile_sync_task_audit(
        db,
        request=request,
        action="olt_profile_sync_task_failed",
        task=task,
        actor_id=actor,
        metadata={"bundle_id": str(bundle.id), "error": task.error},
        is_success=False,
    )
    db.commit()
    return False, task.error


def execute_due_profile_sync_tasks(
    db: Session,
    *,
    executed_by: str | None,
    actor_is_admin: bool,
    request: Request | None = None,
    now: datetime | None = None,
    limit: int = 25,
    backup_runner: BackupRunner | None = None,
    command_executor: CommandExecutor | None = None,
) -> dict[str, object]:
    """Execute approved and due scheduled tasks up to a bounded limit."""
    tasks = list_due_profile_sync_tasks(db, now=now, limit=limit)
    results: list[dict[str, object]] = []
    for task in tasks:
        ok, message = execute_profile_sync_task(
            db,
            task_id=str(task.id),
            executed_by=executed_by,
            actor_is_admin=actor_is_admin,
            request=request,
            now=now,
            backup_runner=backup_runner,
            command_executor=command_executor,
        )
        results.append({"task_id": str(task.id), "ok": ok, "message": message})
    return {
        "total": len(results),
        "completed": sum(1 for result in results if result["ok"]),
        "failed": sum(1 for result in results if not result["ok"]),
        "results": results,
    }


def execute_due_profile_sync_tasks_from_form(
    db: Session,
    *,
    executed_by: str | None,
    actor_is_admin: bool,
    request: Request | None = None,
    limit: int = 25,
) -> tuple[bool, str]:
    """Execute approved/due profile sync tasks from the admin queue."""
    if not str(executed_by or "").strip():
        return False, "Cannot execute due profile sync tasks without an actor"
    if not actor_is_admin:
        return False, "Only admin users can execute due OLT profile sync tasks"
    summary = execute_due_profile_sync_tasks(
        db,
        executed_by=executed_by,
        actor_is_admin=actor_is_admin,
        request=request,
        limit=limit,
    )
    total = int(summary.get("total") or 0)  # type: ignore[call-overload]
    completed = int(summary.get("completed") or 0)  # type: ignore[call-overload]
    failed = int(summary.get("failed") or 0)  # type: ignore[call-overload]
    if total == 0:
        return True, "No approved or due scheduled profile sync tasks are ready"
    return failed == 0, (
        f"Executed {total} due profile sync task(s): "
        f"{completed} completed, {failed} failed"
    )


def check_profile_bundle_drift(
    db: Session,
    *,
    checked_by: str | None,
    request: Request | None = None,
    limit: int = 50,
    backup_runner: SnapshotBackupRunner | None = None,
) -> tuple[bool, str]:
    """Capture OLT snapshots and update active bundle validation status."""
    actor = str(checked_by or "").strip()
    if not actor:
        return False, "Cannot check profile bundle drift without an actor"

    bundles = list(
        db.scalars(
            select(OltProfileBundle)
            .where(OltProfileBundle.is_active.is_(True))
            .order_by(OltProfileBundle.updated_at.desc())
            .limit(max(1, min(int(limit or 50), 200)))
        )
    )
    if not bundles:
        return True, "No active profile bundles to check"

    checked = 0
    in_sync = 0
    drifted = 0
    unknown = 0
    readers: dict[str, OltConfigSnapshotReader] = {}
    backup_failures: dict[str, str] = {}
    for bundle in bundles:
        status, details = _check_one_profile_bundle_drift(
            bundle,
            db=db,
            readers=readers,
            backup_failures=backup_failures,
            backup_runner=backup_runner,
        )
        bundle.drift_status = status
        bundle.drift_details = details
        bundle.last_verified_at = datetime.now(UTC)
        checked += 1
        if status == "in_sync":
            in_sync += 1
        elif status == "drifted":
            drifted += 1
        else:
            unknown += 1
        _log_profile_bundle_audit(
            db,
            request=request,
            action="olt_profile_bundle_drift_checked",
            bundle=bundle,
            actor_id=actor,
            metadata={"drift_status": status, **details},
            is_success=status != "drift_unknown",
        )

    db.commit()
    return unknown == 0 and drifted == 0, (
        f"Validated {checked} bundle(s) from running-config snapshot(s): "
        f"{in_sync} in sync, {drifted} need action, {unknown} snapshot failures"
    )


def _profile_sync_worker_enabled(db: Session) -> bool:
    from app.services import control_registry

    return control_registry.is_enabled(db, "network.olt_profile_sync")


def _profile_sync_worker_interval_seconds(db: Session) -> int:
    raw = os.getenv("OLT_PROFILE_SYNC_INTERVAL_SECONDS", "")
    if not raw:
        raw = _domain_setting_text(db, "olt_profile_sync_interval_seconds") or ""
    try:
        return max(int(raw), 60)
    except (TypeError, ValueError):
        return 300


def _domain_setting_text(db: Session, key: str) -> str | None:
    setting = (
        db.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.network)
        .filter(DomainSetting.key == key)
        .filter(DomainSetting.is_active.is_(True))
        .first()
    )
    if setting is None:
        return None
    if setting.value_text:
        return str(setting.value_text)
    if setting.value_json is not None:
        return str(setting.value_json)
    return None


def _log_profile_sync_task_audit(
    db: Session,
    *,
    request: Request | None,
    action: str,
    task: OltProfileSyncTask,
    actor_id: str | None,
    metadata: dict[str, object] | None = None,
    is_success: bool = True,
) -> None:
    payload = {
        "status": task.status,
        "olt_id": str(task.olt_id),
        "offer_id": str(task.offer_id),
        "trigger": task.trigger,
    }
    payload.update({key: value for key, value in (metadata or {}).items() if value})  # type: ignore[misc]
    log_audit_event(
        db=db,
        request=request,
        action=action,
        entity_type="olt_profile_sync_task",
        entity_id=str(task.id),
        actor_id=actor_id,
        metadata=payload,
        status_code=200,
        is_success=is_success,
    )


def _log_profile_bundle_audit(
    db: Session,
    *,
    request: Request | None,
    action: str,
    bundle: OltProfileBundle,
    actor_id: str | None,
    metadata: dict[str, object] | None = None,
    is_success: bool = True,
) -> None:
    payload = {
        "olt_id": str(bundle.olt_id),
        "offer_id": str(bundle.offer_id),
        "bundle_name": bundle.name,
    }
    payload.update({key: value for key, value in (metadata or {}).items() if value})  # type: ignore[misc]
    log_audit_event(
        db=db,
        request=request,
        action=action,
        entity_type="olt_profile_bundle",
        entity_id=str(bundle.id),
        actor_id=actor_id,
        metadata=payload,
        status_code=200,
        is_success=is_success,
    )


def _task_result_payload(
    task: OltProfileSyncTask,
    *,
    executed_by: str,
    bundle_id: str | None = None,
    apply_result: Any | None = None,
) -> dict[str, object]:
    payload = dict(task.result_payload or {})
    payload.update(
        {
            "executed_by": executed_by,
            "executed_at": datetime.now(UTC).isoformat(),
        }
    )
    if bundle_id:
        payload["bundle_id"] = bundle_id
    if apply_result is not None:
        payload["backup_id"] = getattr(apply_result, "backup_id", None)
        payload["commands"] = len(getattr(apply_result, "commands", []) or [])
        errors = list(getattr(apply_result, "errors", []) or [])
        if errors:
            payload["errors"] = errors
    return {key: value for key, value in payload.items() if value is not None}


def _parse_scheduled_for(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _check_one_profile_bundle_drift(
    bundle: OltProfileBundle,
    db: Session | None = None,
    readers: dict[str, OltConfigSnapshotReader] | None = None,
    backup_failures: dict[str, str] | None = None,
    backup_runner: SnapshotBackupRunner | None = None,
) -> tuple[str, dict[str, object]]:
    """Validate bundle profiles from one captured running-config snapshot."""
    olt = bundle.olt
    if olt is None:
        return "drift_unknown", {"message": "Bundle has no linked OLT"}
    if db is None:
        return "drift_unknown", {"message": "Database session is required"}

    expected = _expected_bundle_inventory(bundle)
    olt_key = str(olt.id)
    readers = readers if readers is not None else {}
    backup_failures = backup_failures if backup_failures is not None else {}
    if olt_key in backup_failures:
        return "drift_unknown", {
            "message": backup_failures[olt_key],
            "errors": [backup_failures[olt_key]],
            "source": "running_config_backup",
        }
    reader = readers.get(olt_key)
    if reader is None:
        kwargs: dict[str, object] = {}
        if backup_runner is not None:
            kwargs["backup_runner"] = backup_runner
        reader, message = OltConfigSnapshotReader.capture(db, olt, **kwargs)  # type: ignore[arg-type]
        if reader is None:
            error = f"Snapshot capture failed before validation: {message}"
            backup_failures[olt_key] = error
            return "drift_unknown", {
                "message": error,
                "errors": [error],
                "source": "running_config_backup",
            }
        readers[olt_key] = reader
    return reader.validate_profile_bundle(expected)


def _expected_bundle_inventory(
    bundle: OltProfileBundle,
) -> dict[str, dict[str, dict[str, object]]]:
    names = _expected_bundle_names(bundle)
    return {
        "dba": {
            "DBA profile": {
                "id": bundle.dba_profile_id,
                "name": names.get("dba", ""),
            }
        },
        "traffic": {
            "download traffic table": {
                "id": bundle.download_traffic_table_id,
                "name": names.get("traffic_down", ""),
            },
            "upload traffic table": {
                "id": bundle.upload_traffic_table_id,
                "name": names.get("traffic_up", ""),
            },
        },
        "line": {
            "line profile": {
                "id": bundle.line_profile_id,
                "name": names.get("line", ""),
            }
        },
        "service": {
            "service profile": {
                "id": bundle.service_profile_id,
                "name": names.get("service", ""),
            }
        },
    }


def _expected_bundle_names(bundle: OltProfileBundle) -> dict[str, str]:
    names: dict[str, str] = {}
    command_plan = bundle.command_plan or {}
    groups = command_plan.get("groups")
    if not isinstance(groups, list):
        return names
    for raw_group in groups:
        if not isinstance(raw_group, dict):
            continue
        step = str(raw_group.get("step") or "").casefold()
        commands = raw_group.get("commands")
        if not isinstance(commands, list) or not commands:
            continue
        first_command = str(commands[0] or "")
        if "traffic" in step:
            match = _TRAFFIC_NAME_RE.search(first_command)
            if "download" in step and match:
                names["traffic_down"] = match.group("name")
            elif "upload" in step and match:
                names["traffic_up"] = match.group("name")
            continue
        match = _PROFILE_NAME_RE.search(first_command)
        if not match:
            continue
        if "dba" in step:
            names["dba"] = match.group("name")
        elif "line" in step:
            names["line"] = match.group("name")
        elif "service" in step:
            names["service"] = match.group("name")
    return names


def _profile_entry_map(entries: object, *, id_attr: str) -> dict[int, str]:
    mapped: dict[int, str] = {}
    for entry in list(entries or []):  # type: ignore[call-overload]
        raw_id = getattr(entry, id_attr, None)
        if raw_id is None:
            continue
        try:
            profile_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        mapped[profile_id] = str(getattr(entry, "name", "") or "")
    return mapped


def _profile_inventory_entries(
    inventory: dict[int, str],
    *,
    id_attr: str,
) -> list[SimpleNamespace]:
    return [
        SimpleNamespace(**{id_attr: profile_id, "name": name})
        for profile_id, name in inventory.items()
    ]


def offer_profile_sync_preview_context(
    db: Session,
    olt_id: str,
    *,
    offer_id: str,
    vlan_id: int,
) -> dict[str, Any]:
    """Build a dry-run command preview for syncing one offer to OLT profiles."""
    ok, message, olt, offer, plan = _build_offer_profile_sync_plan_from_live(
        db,
        olt_id,
        offer_id=offer_id,
        vlan_id=vlan_id,
    )
    if not ok or olt is None or offer is None or plan is None:
        return {"ok": False, "message": message}

    return {
        "ok": True,
        "message": f"Dry-run profile sync plan built for {offer.name}",
        "olt": olt,
        "offer": offer,
        "bundle": plan.bundle,
        "apply_plan": plan.apply_plan,
        "allocations": plan.allocations,
        "saved_bundle": None,
    }


def save_offer_profile_bundle(
    db: Session,
    olt_id: str,
    *,
    offer_id: str,
    vlan_id: int,
) -> dict[str, Any]:
    """Regenerate, validate, and persist a profile bundle without OLT writes."""
    ok, message, olt, offer, plan = _build_offer_profile_sync_plan_from_live(
        db,
        olt_id,
        offer_id=offer_id,
        vlan_id=vlan_id,
    )
    if not ok or olt is None or offer is None or plan is None:
        return {"ok": False, "message": message}

    record = upsert_profile_bundle(db, olt=olt, sync_plan=plan)
    db.commit()
    return {
        "ok": True,
        "message": f"Saved profile bundle for {offer.name}",
        "olt": olt,
        "offer": offer,
        "bundle": plan.bundle,
        "apply_plan": plan.apply_plan,
        "allocations": plan.allocations,
        "saved_bundle": record,
    }


def apply_saved_profile_bundle(
    db: Session,
    olt_id: str,
    bundle_id: str,
    *,
    actor_is_admin: bool,
    backup_runner: BackupRunner | None = None,
    command_executor: CommandExecutor | None = None,
) -> dict[str, Any]:
    """Apply a saved profile bundle to the OLT with backup and admin guardrails."""
    olt = db.get(OLTDevice, olt_id)
    if olt is None:
        return {"ok": False, "message": "OLT not found", "bundle": None}

    bundle = db.get(OltProfileBundle, bundle_id)
    if bundle is None or bundle.olt_id != olt.id:
        return {"ok": False, "message": "Profile bundle not found", "bundle": None}
    if not bundle.is_active:
        return {"ok": False, "message": "Profile bundle is inactive", "bundle": bundle}
    if not actor_is_admin:
        return {
            "ok": False,
            "message": "Only admin users can apply OLT profile bundles",
            "bundle": bundle,
        }

    try:
        plan = _build_apply_plan_from_saved_bundle(bundle)
    except ValueError as exc:
        return {"ok": False, "message": str(exc), "bundle": bundle}

    ownership_preflight = validate_dotmac_profile_apply_plan(plan)
    if not ownership_preflight.success:
        bundle.drift_status = "preflight_failed"
        bundle.drift_details = {
            "message": ownership_preflight.message,
            "errors": list(ownership_preflight.errors),
        }
        db.flush()
        return {"ok": False, "message": ownership_preflight.message, "bundle": bundle}

    kwargs: dict[str, Any] = {}
    if backup_runner is not None:
        kwargs["backup_runner"] = backup_runner
    reader, snapshot_message = OltConfigSnapshotReader.capture(db, olt, **kwargs)
    if reader is None:
        bundle.drift_status = "preflight_failed"
        bundle.drift_details = {
            "message": f"Snapshot capture failed before profile apply: {snapshot_message}",
            "source": "running_config_backup",
        }
        db.flush()
        return {
            "ok": False,
            "message": f"Snapshot capture failed before profile apply: {snapshot_message}",
            "bundle": bundle,
        }

    snapshot_preflight_ok, snapshot_preflight_message = (
        _validate_saved_bundle_against_snapshot(
            bundle,
            plan,
            reader,
        )
    )
    if not snapshot_preflight_ok:
        bundle.drift_status = "preflight_failed"
        bundle.drift_details = {
            "message": snapshot_preflight_message,
            "backup_id": reader.snapshot.backup_id,
            "captured_at": reader.snapshot.provenance().get("captured_at"),
            "source": "running_config_backup",
        }
        db.flush()
        return {"ok": False, "message": snapshot_preflight_message, "bundle": bundle}

    apply_kwargs: dict[str, Any] = {}
    if command_executor is not None:
        apply_kwargs["command_executor"] = command_executor
    result = apply_profile_bundle(
        db,
        olt,
        plan,
        actor_is_admin=actor_is_admin,
        dry_run=False,
        require_admin=True,
        require_backup=False,
        **apply_kwargs,
    )
    result = replace(result, backup_id=reader.snapshot.backup_id)
    if not result.success:
        bundle.drift_status = "apply_failed"
        bundle.drift_details = {
            "message": result.message,
            "errors": list(result.errors),
            "backup_id": reader.snapshot.backup_id,
            "captured_at": reader.snapshot.provenance().get("captured_at"),
        }
        db.flush()
        return {
            "ok": False,
            "message": result.message,
            "bundle": bundle,
            "apply_result": result,
        }

    now = datetime.now(UTC)
    bundle.last_applied_at = now
    bundle.drift_status = "applied"
    bundle.drift_details = {
        "message": result.message,
        "backup_id": reader.snapshot.backup_id,
        "captured_at": reader.snapshot.provenance().get("captured_at"),
        "commands": len(result.commands),
    }
    db.commit()
    return {
        "ok": True,
        "message": result.message,
        "bundle": bundle,
        "apply_result": result,
    }


def _build_apply_plan_from_saved_bundle(bundle: OltProfileBundle):
    command_plan = bundle.command_plan or {}
    groups = command_plan.get("groups")
    if not isinstance(groups, list) or not groups:
        raise ValueError("Saved profile bundle has no command plan")

    command_groups: list[ProfileCommandGroup] = []
    for raw_group in groups:
        if not isinstance(raw_group, dict):
            raise ValueError("Saved profile bundle command plan is malformed")
        commands = raw_group.get("commands")
        if not isinstance(commands, list):
            raise ValueError("Saved profile bundle command group has no commands")
        command_groups.append(
            ProfileCommandGroup(
                step=str(raw_group.get("step") or "Apply profile commands"),
                commands=tuple(str(command) for command in commands),
                requires_config_mode=bool(raw_group.get("requires_config_mode", True)),
            )
        )

    return build_profile_apply_plan(bundle.name, command_groups)


def _validate_saved_bundle_against_snapshot(
    bundle: OltProfileBundle,
    plan,
    reader: OltConfigSnapshotReader,
) -> tuple[bool, str]:
    sync_plan = OfferProfileSyncPlan(
        bundle=OfferProfileBundle(
            offer_id=str(bundle.offer_id),
            offer_name=bundle.name,
            vlan_id=bundle.vlan_id,
            download_kbps=bundle.download_kbps,
            upload_kbps=bundle.upload_kbps,
            dba_profile_id=bundle.dba_profile_id,
            download_traffic_table_id=bundle.download_traffic_table_id,
            upload_traffic_table_id=bundle.upload_traffic_table_id,
            line_profile_id=bundle.line_profile_id,
            service_profile_id=bundle.service_profile_id,
            gem_id=bundle.gem_id,
            tcont_id=bundle.tcont_id,
            checksum=bundle.checksum,
        ),
        apply_plan=plan,
        allocations=(),
    )
    preflight = validate_offer_profile_sync_plan_inventory(
        sync_plan,
        reader.profile_preflight_inventory(),
    )
    return preflight.success, preflight.message


def _build_offer_profile_sync_plan_from_live(
    db: Session,
    olt_id: str,
    *,
    offer_id: str,
    vlan_id: int,
):
    olt = db.get(OLTDevice, olt_id)
    if olt is None:
        return False, "OLT not found", None, None, None

    offer = db.get(CatalogOffer, offer_id)
    if offer is None:
        return False, "Catalog offer not found", olt, None, None

    reader, snapshot_message = OltConfigSnapshotReader.capture(db, olt)
    if reader is None:
        message = (
            "Cannot build profile sync preview because running-config snapshot "
            f"capture failed: {snapshot_message}"
        )
        return False, message, olt, offer, None
    snapshot_profiles = reader.profile_inventory()

    try:
        plan = build_offer_profile_sync_plan(
            offer,
            vlan_id=vlan_id,
            live_dba_profiles=_profile_inventory_entries(
                snapshot_profiles["dba"],
                id_attr="profile_id",
            ),
            live_traffic_tables=_profile_inventory_entries(
                snapshot_profiles["traffic"],
                id_attr="index",
            ),
            live_line_profiles=_profile_inventory_entries(
                snapshot_profiles["line"],
                id_attr="profile_id",
            ),
            live_service_profiles=_profile_inventory_entries(
                snapshot_profiles["service"],
                id_attr="profile_id",
            ),
        )
    except (OfferProfileSyncError, ValueError) as exc:
        return False, str(exc), olt, offer, None

    try:
        inventory = reader.profile_preflight_inventory()
        preflight = validate_offer_profile_sync_plan_inventory(plan, inventory)
    except ValueError as exc:
        return False, str(exc), olt, offer, None
    if not preflight.success:
        return False, preflight.message, olt, offer, None

    return (
        True,
        f"Profile sync plan built from snapshot {reader.snapshot.backup_id}",
        olt,
        offer,
        plan,
    )


def save_imported_profile_mapping(
    db: Session,
    olt_id: str,
    *,
    equipment_id: str,
    line_profile_id: int,
    service_profile_id: int,
    wan_provisioning_mode: str | None = None,
    internet_config_ip_index: int | None = None,
    wan_config_profile_id: int | None = None,
    pppoe_wcd_index: int | None = None,
    mgmt_wcd_index: int | None = None,
    voip_wcd_index: int | None = None,
    primary_wan_service: str | None = None,
) -> tuple[bool, str]:
    """Create or update an OLT equipment mapping using imported profiles only."""
    olt = db.get(OLTDevice, olt_id)
    if olt is None:
        return False, "OLT not found"

    clean_equipment_id = equipment_id.strip()
    if not clean_equipment_id:
        return False, "Equipment ID is required"

    line_profile = db.scalars(
        select(OltLineProfile)
        .where(OltLineProfile.olt_id == olt.id)
        .where(OltLineProfile.profile_id == line_profile_id)
    ).first()
    if line_profile is None:
        return (
            False,
            f"Line profile {line_profile_id} has not been imported for {olt.name}",
        )

    service_profile = db.scalars(
        select(OltServiceProfile)
        .where(OltServiceProfile.olt_id == olt.id)
        .where(OltServiceProfile.profile_id == service_profile_id)
    ).first()
    if service_profile is None:
        return (
            False,
            f"Service profile {service_profile_id} has not been imported for {olt.name}",
        )

    mapping = db.scalars(
        select(OltOnuTypeProfileMapping)
        .where(OltOnuTypeProfileMapping.olt_id == olt.id)
        .where(OltOnuTypeProfileMapping.equipment_id == clean_equipment_id)
    ).first()
    created = mapping is None
    if mapping is None:
        mapping = OltOnuTypeProfileMapping(
            olt_id=olt.id,
            equipment_id=clean_equipment_id,
            line_profile_id=line_profile_id,
            service_profile_id=service_profile_id,
            wan_provisioning_mode=wan_provisioning_mode,
            internet_config_ip_index=internet_config_ip_index,
            wan_config_profile_id=wan_config_profile_id,
            pppoe_wcd_index=pppoe_wcd_index,
            mgmt_wcd_index=mgmt_wcd_index,
            voip_wcd_index=voip_wcd_index,
            primary_wan_service=primary_wan_service,
            source_registration_count=0,
        )
        db.add(mapping)
    else:
        mapping.line_profile_id = line_profile_id
        mapping.service_profile_id = service_profile_id
        mapping.wan_provisioning_mode = wan_provisioning_mode
        mapping.internet_config_ip_index = internet_config_ip_index
        mapping.wan_config_profile_id = wan_config_profile_id
        mapping.pppoe_wcd_index = pppoe_wcd_index
        mapping.mgmt_wcd_index = mgmt_wcd_index
        mapping.voip_wcd_index = voip_wcd_index
        mapping.primary_wan_service = primary_wan_service

    db.flush()
    action = "Created" if created else "Updated"
    return (
        True,
        (
            f"{action} mapping for {clean_equipment_id}: "
            f"line {line_profile_id}, service {service_profile_id}"
        ),
    )


def delete_imported_profile_mapping(
    db: Session,
    olt_id: str,
    mapping_id: str,
) -> tuple[bool, str]:
    """Delete an explicit imported equipment mapping."""
    olt = db.get(OLTDevice, olt_id)
    if olt is None:
        return False, "OLT not found"

    mapping = db.get(OltOnuTypeProfileMapping, mapping_id)
    if mapping is None or str(mapping.olt_id) != str(olt.id):
        return False, "Mapping not found"

    equipment_id = mapping.equipment_id
    db.delete(mapping)
    db.flush()
    return True, f"Deleted mapping for {equipment_id}"


def propagate_acs_to_onts(
    db: Session, olt_id: str, *, request: Request | None = None
) -> tuple[int, dict[str, Any]]:
    try:
        stats = olt_service.OLTDevices.propagate_acs_to_onts(db, olt_id)
    except HTTPException as exc:
        return exc.status_code, {"ok": False, "message": exc.detail}

    log_olt_audit_event(
        db,
        request=request,
        action="propagate_acs",
        entity_id=olt_id,
        metadata=dict(stats),
    )
    updated = stats["updated"]
    total = stats["total"]
    already = stats["already_bound"]
    if updated:
        message = (
            f"ACS binding propagated to {updated} ONTs "
            f"({already} already bound, {total} total)."
        )
    else:
        message = f"All {total} ONTs already bound to this ACS server."
    return 200, {"ok": True, "message": message, **stats}


def enforce_provisioning(
    db: Session, olt_id: str, *, request: Request | None = None
) -> tuple[int, dict[str, Any]]:
    from app.services.network.provisioning_enforcement import ProvisioningEnforcement

    stats = ProvisioningEnforcement.run_full_enforcement(db, olt_id=olt_id)
    log_olt_audit_event(
        db,
        request=request,
        action="enforce_provisioning",
        entity_id=olt_id,
        metadata=dict(stats),
    )

    gaps = stats.get("gaps_detected", {})
    total_gaps = sum(gaps.values()) if isinstance(gaps, dict) else 0
    if total_gaps == 0:
        message = "No provisioning gaps detected on this OLT."
    else:
        message = f"Provisioning gap scan complete: {total_gaps} gap(s) detected."
    return 200, {"ok": True, "message": message, **stats}


def backfill_pon_ports(
    db: Session, olt_id: str, *, request: Request | None = None
) -> tuple[int, dict[str, Any]]:
    try:
        stats = olt_service.OLTDevices.backfill_pon_ports(db, olt_id)
    except HTTPException as exc:
        return exc.status_code, {"ok": False, "message": exc.detail}

    log_olt_audit_event(
        db,
        request=request,
        action="backfill_pon_ports",
        entity_id=olt_id,
        metadata=dict(stats),
    )

    created = stats["ports_created"]
    linked = stats["assignments_linked"]
    total = stats["total_onts"]
    parts = []
    if created:
        parts.append(f"{created} PON ports created")
    if linked:
        parts.append(f"{linked} assignments linked")
    if not parts:
        message = f"All PON ports already exist for {total} ONTs."
    else:
        message = f"{', '.join(parts)} ({total} ONTs on this OLT)."
    return 200, {"ok": True, "message": message, **stats}


def command_preview_context(
    db: Session,
    ont_id: str,
    profile_id: str,
    *,
    tr069_olt_profile_id: int | None = None,
) -> dict[str, Any]:
    """Generate provisioning command preview for an ONT + profile combo.

    Args:
        db: Database session.
        ont_id: OntUnit ID.
        profile_id: OntProvisioningProfile ID.
        tr069_olt_profile_id: OLT-level TR-069 server profile ID.

    Returns:
        Context dict with command_sets, spec, error.
    """
    context: dict[str, Any] = {
        "command_sets": [],
        "error": None,
        "ont": None,
        "profile": None,
    }

    ont, olt, fsp, olt_ont_id = _resolve_ont_olt_context(db, ont_id)
    if not ont:
        context["error"] = "ONT not found"
        return context
    context["ont"] = ont

    if not olt or not fsp or olt_ont_id is None:
        context["error"] = (
            "Cannot resolve OLT context — check assignment and external ID"
        )
        return context

    profile = db.get(OntProvisioningProfile, profile_id)
    if not profile:
        context["error"] = "Provisioning profile not found"
        return context
    context["profile"] = profile

    # Build provisioning context
    parts = fsp.split("/")
    prov_context = OntProvisioningContext(
        frame=int(parts[0]) if len(parts) > 0 else 0,
        slot=int(parts[1]) if len(parts) > 1 else 0,
        port=int(parts[2]) if len(parts) > 2 else 0,
        ont_id=olt_ont_id,
        olt_name=olt.name,
    )

    # Get subscriber info if available

    for a in getattr(ont, "assignments", []):
        if a.active and a.subscriber_id:
            from app.models.subscriber import Subscriber

            sub = db.get(Subscriber, str(a.subscriber_id))
            if sub:
                prov_context.subscriber_code = getattr(sub, "account_number", "") or ""
                prov_context.subscriber_name = getattr(sub, "full_name", "") or ""
            break

    # Build spec and generate commands
    spec = build_spec_from_profile(
        profile, prov_context, tr069_profile_id=tr069_olt_profile_id, olt=olt
    )
    command_sets = [
        OltCommandSet(
            step=item.step,
            commands=[mask_credentials(command) for command in item.commands],
            description=item.description,
            requires_config_mode=item.requires_config_mode,
        )
        for item in HuaweiCommandGenerator.generate_full_provisioning(
            spec, prov_context
        )
    ]

    context["command_sets"] = command_sets
    context["spec"] = spec
    context["prov_context"] = prov_context

    return context
