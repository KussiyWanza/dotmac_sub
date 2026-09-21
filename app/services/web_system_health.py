"""Service helpers for admin system health page."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from app.models.domain_settings import SettingDomain
from app.models.scheduler import ScheduledTask
from app.services import billing_invoice_pdf as billing_invoice_pdf_service
from app.services import infrastructure_health as infrastructure_health_service
from app.services import job_heartbeat, settings_spec
from app.services import system_health as system_health_service

logger = logging.getLogger(__name__)


def build_health_data(db) -> dict[str, object]:
    health = system_health_service.get_system_health()
    thresholds_raw: dict[str, Any] = {
        "disk_warn_pct": settings_spec.resolve_value(
            db, SettingDomain.network_monitoring, "server_health_disk_warn_pct"
        ),
        "disk_crit_pct": settings_spec.resolve_value(
            db, SettingDomain.network_monitoring, "server_health_disk_crit_pct"
        ),
        "mem_warn_pct": settings_spec.resolve_value(
            db, SettingDomain.network_monitoring, "server_health_mem_warn_pct"
        ),
        "mem_crit_pct": settings_spec.resolve_value(
            db, SettingDomain.network_monitoring, "server_health_mem_crit_pct"
        ),
        "load_warn": settings_spec.resolve_value(
            db, SettingDomain.network_monitoring, "server_health_load_warn"
        ),
        "load_crit": settings_spec.resolve_value(
            db, SettingDomain.network_monitoring, "server_health_load_crit"
        ),
    }
    thresholds: dict[str, float | None] = {}
    for key, value in thresholds_raw.items():
        try:
            thresholds[key] = float(str(value)) if value is not None else None
        except (TypeError, ValueError):
            thresholds[key] = None
    health_status = system_health_service.evaluate_health(health, thresholds)
    invoice_cache_stats = billing_invoice_pdf_service.get_cache_dashboard_stats(db)
    infrastructure_services = infrastructure_health_service.check_all_services(db)
    return {
        "health": health,
        "health_status": health_status,
        "invoice_cache_stats": invoice_cache_stats,
        "infrastructure_services": infrastructure_services,
        "worker_health": _build_worker_health(infrastructure_services),
        "replication_health": _build_replication_health(db),
        "task_activity": _build_task_activity(db),
        "erp_sync_ownership": _build_erp_sync_ownership(db),
        "erp_delivered_unlinked": _build_erp_delivered_unlinked(db),
    }


def _build_erp_delivered_unlinked(db) -> dict[str, object]:
    """Surface delivered-but-unlinked ``field_erp_sync_events`` rows per flow.

    A ``sent`` row can never satisfy a status poller gated on "the source
    entity already has an ERP reference" — that row has no reference BY
    DEFINITION (see ``dotmac_erp.outbox``'s module docstring). An ``accepted``
    row can also land here when its same-transaction write-back silently
    failed. Neither self-heals without an operator noticing, so this makes
    the count and oldest age visible on the same deploy/health surface as
    ``erp_sync_ownership`` instead of only inside the outbox table.

    INFORMATIONAL ONLY — not an alert or an SLA. This surface reports the raw
    per-flow count and oldest age; it does not flag, highlight, or compare
    against any threshold. Purchase-order/invoice write-backs affect
    accounts-payable and expense-claim write-backs are payroll-adjacent, so a
    shared numeric cutoff would be an invented, unowned SLA. Treat this as
    data for a human to read until a later, separately reviewed change gives
    each flow its own explicitly owned threshold.
    """
    try:
        from app.services.dotmac_erp.outbox import delivered_unlinked_diagnostics

        flows = delivered_unlinked_diagnostics(db)
    except Exception as exc:
        logger.debug("ERP delivered-unlinked surface unavailable", exc_info=True)
        return {"status": "unknown", "flows": {}, "error": str(exc)[:200]}

    return {"flows": flows}


def _build_erp_sync_ownership(db) -> dict[str, object]:
    """Surface the single-writer flow ownership for the ERP re-home.

    Makes "which flows may sub write to ERP" visible on the deploy/health
    surface — the deploy-check half of the "enforced by code + deploy checks"
    control. Sub owns a flow only when its row reads ``sub``; every flow defaults
    to CRM until explicitly cut over.
    """
    try:
        from app.models.field_erp_sync import SyncFlowOwner, get_flow_ownership
        from app.services.integrations.backoffice_contracts import (
            ERP_OUTBOX_CAPABILITY,
        )
        from app.services.integrations.erp_capability import capability_enabled

        ownership = get_flow_ownership(db)
        sync_enabled = capability_enabled(db, ERP_OUTBOX_CAPABILITY)
    except Exception as exc:
        logger.debug("ERP sync ownership surface unavailable", exc_info=True)
        return {
            "status": "unknown",
            "sync_enabled": False,
            "flows": {},
            "sub_owned_flows": [],
            "error": str(exc)[:200],
        }

    sub_owned = sorted(
        flow for flow, owner in ownership.items() if owner == SyncFlowOwner.sub.value
    )
    return {
        # The master switch is off until cutover; sub owning nothing is the
        # expected steady state pre-cutover, so that reads as "ok".
        "status": "ok",
        "sync_enabled": sync_enabled,
        "flows": ownership,
        "sub_owned_flows": sub_owned,
    }


def _build_worker_health(services: Sequence[object]) -> dict[str, object]:
    celery = next(
        (
            service
            for service in services
            if getattr(service, "name", "").lower() == "celery"
        ),
        None,
    )
    if celery is None:
        return {
            "status": "unknown",
            "worker_count": 0,
            "workers": [],
            "worker_details": [],
            "expected_queues": [],
            "missing_queues": [],
            "queue_restart_targets": {},
            "restart_enabled": False,
            "active_tasks": 0,
            "reserved_tasks": 0,
            "scheduled_tasks": 0,
            "queue_lengths": {},
            "long_running_tasks": [],
            "message": "Celery health check did not run.",
        }

    details = getattr(celery, "details", {}) or {}
    workers = list(details.get("workers") or [])
    return {
        "status": getattr(celery, "status", "unknown"),
        "worker_count": len(workers),
        "workers": workers,
        "worker_details": list(details.get("worker_details") or []),
        "expected_queues": list(details.get("expected_queues") or []),
        "missing_queues": list(details.get("missing_queues") or []),
        "queue_restart_targets": dict(details.get("queue_restart_targets") or {}),
        "restart_enabled": bool(details.get("restart_enabled")),
        "active_tasks": int(details.get("active_tasks") or 0),
        "reserved_tasks": int(details.get("reserved_tasks") or 0),
        "scheduled_tasks": int(details.get("scheduled_tasks") or 0),
        "queue_lengths": dict(details.get("queue_lengths") or {}),
        "long_running_tasks": list(details.get("long_running_tasks_over_30m") or []),
        "response_ms": getattr(celery, "response_ms", 0),
        "message": (details.get("error") if isinstance(details, dict) else None),
    }


def _build_replication_health(db) -> dict[str, object]:
    try:
        standby_rows = (
            db.execute(
                text(
                    """
                    SELECT
                        application_name,
                        client_addr::text AS client_addr,
                        state,
                        sync_state,
                        sent_lsn::text AS sent_lsn,
                        write_lsn::text AS write_lsn,
                        flush_lsn::text AS flush_lsn,
                        replay_lsn::text AS replay_lsn,
                        pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn)::bigint
                            AS bytes_behind,
                        EXTRACT(EPOCH FROM write_lag)::float AS write_lag_seconds,
                        EXTRACT(EPOCH FROM flush_lag)::float AS flush_lag_seconds,
                        EXTRACT(EPOCH FROM replay_lag)::float AS replay_lag_seconds
                    FROM pg_stat_replication
                    ORDER BY application_name, client_addr
                    """
                )
            )
            .mappings()
            .all()
        )
        slot_rows = (
            db.execute(
                text(
                    """
                    SELECT
                        slot_name,
                        slot_type,
                        active,
                        active_pid,
                        wal_status,
                        CASE
                            WHEN restart_lsn IS NULL THEN NULL
                            ELSE pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)::bigint
                        END AS retained_bytes
                    FROM pg_replication_slots
                    WHERE slot_type = 'physical'
                    ORDER BY slot_name
                    """
                )
            )
            .mappings()
            .all()
        )
    except Exception as exc:
        try:
            db.rollback()
        except Exception:
            logger.debug(
                "Rollback failed after replication health error", exc_info=True
            )
        return {
            "status": "unknown",
            "summary": "Replication status unavailable.",
            "error": str(exc)[:200],
            "standbys": [],
            "slots": [],
        }

    standbys = [
        {
            "application_name": row["application_name"] or "standby",
            "client_addr": row["client_addr"] or "-",
            "state": row["state"] or "unknown",
            "sync_state": row["sync_state"] or "async",
            "bytes_behind": int(row["bytes_behind"] or 0),
            "bytes_behind_display": _format_bytes(int(row["bytes_behind"] or 0)),
            "replay_lag_seconds": _round_optional(row["replay_lag_seconds"]),
            "replay_lag_display": _format_seconds(row["replay_lag_seconds"]),
        }
        for row in standby_rows
    ]
    slots = [
        {
            "slot_name": row["slot_name"],
            "active": bool(row["active"]),
            "wal_status": row["wal_status"] or "unknown",
            "retained_bytes": int(row["retained_bytes"] or 0),
            "retained_display": _format_bytes(int(row["retained_bytes"] or 0)),
        }
        for row in slot_rows
    ]

    inactive_slots = [slot for slot in slots if not slot["active"]]
    lagging_standbys = [
        row for row in standbys if int(row["bytes_behind"] or 0) > 64 * 1024 * 1024
    ]
    if standbys and not inactive_slots and not lagging_standbys:
        status = "up"
        summary = f"{len(standbys)} standby connection(s) streaming."
    elif standbys:
        status = "degraded"
        summary = "Standby is connected, but replication needs attention."
    elif slots:
        status = "degraded"
        summary = "Replication slot exists, but no standby is connected."
    else:
        status = "unknown"
        summary = "No physical standby slots or active standby connections found."

    return {
        "status": status,
        "summary": summary,
        "standbys": standbys,
        "slots": slots,
    }


def _build_task_activity(db, *, limit: int = 10) -> list[dict[str, object]]:
    rows = (
        db.query(ScheduledTask)
        .filter(ScheduledTask.enabled.is_(True))
        .order_by(ScheduledTask.name.asc())
        .limit(limit)
        .all()
    )
    activity: list[dict[str, object]] = []
    for row in rows:
        last_success = job_heartbeat.get_last_success(row.task_name)
        if last_success is not None and last_success.tzinfo is None:
            last_success = last_success.replace(tzinfo=UTC)
        age_seconds = None
        if last_success is not None:
            age_seconds = (datetime.now(UTC) - last_success).total_seconds()
        interval = int(row.interval_seconds or 0)
        stale = last_success is None or (
            interval > 0 and age_seconds is not None and age_seconds > interval * 3
        )
        activity.append(
            {
                "name": row.name,
                "task_name": row.task_name,
                "interval_seconds": interval or None,
                "last_success": last_success,
                "age_display": _format_seconds(age_seconds),
                "stale": stale,
            }
        )
    return activity


def _format_bytes(value: int | float | None) -> str:
    return system_health_service._format_bytes(value)


def _format_seconds(value: object) -> str:
    try:
        seconds = float(str(value)) if value is not None else None
    except (TypeError, ValueError):
        seconds = None
    return system_health_service._format_duration(seconds)


def _round_optional(value: object) -> float | None:
    try:
        return round(float(str(value)), 3) if value is not None else None
    except (TypeError, ValueError):
        return None
