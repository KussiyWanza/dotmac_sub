"""Infrastructure health checks for Docker-hosted services.

Performs lightweight connectivity and version checks against all
infrastructure dependencies (PostgreSQL, Redis, VictoriaMetrics,
GenieACS, FreeRADIUS, MinIO, Celery, Nominatim).
"""

from __future__ import annotations

import logging
import os
import socket
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx

from app.config import settings

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_CHECK_TIMEOUT = 3  # seconds for each health check
_DEFAULT_CELERY_QUEUES = (
    "celery",
    "nin",
    "tr069",
    "acs",
    "bandwidth",
    "monitoring",
    "ingestion",
    "crm",
    "billing",
)
_DEFAULT_CELERY_QUEUE_RESTART_TARGETS = {
    "celery": "celery-worker",
    "nin": "celery-worker",
    "crm": "celery-worker",
    "tr069": "celery-worker-tr069",
    "acs": "celery-worker-tr069",
    "bandwidth": "celery-worker-bandwidth",
    "monitoring": "celery-worker-monitoring",
    "ingestion": "celery-worker-ingestion",
    "billing": "celery-worker-billing",
}

_HEALTH_CHECK_KEYS = frozenset(
    {
        "postgres",
        "redis",
        "victoriametrics",
        "genieacs",
        "radius_db",
        "minio",
        "celery",
        "nominatim",
    }
)


def _skipped_health_checks() -> set[str]:
    """Return explicitly disabled infrastructure probes.

    Non-production environments often omit integrations intentionally. Waiting
    for those network timeouts makes the dashboard look unhealthy and slow even
    though the configured staging dependencies are fine. Production keeps the
    current behaviour because the default is an empty set.
    """
    raw = settings.infrastructure_health_skip_checks
    requested = {
        item.strip().lower().replace("-", "_")
        for item in raw.split(",")
        if item.strip()
    }
    unknown = requested - _HEALTH_CHECK_KEYS
    if unknown:
        logger.warning(
            "Ignoring unknown infrastructure health checks: %s",
            ", ".join(sorted(unknown)),
        )
    return requested & _HEALTH_CHECK_KEYS


@dataclass
class ServiceStatus:
    """Result of a single infrastructure health check."""

    name: str
    status: str  # "up", "down", "degraded"
    version: str = ""
    response_ms: float = 0.0
    details: dict[str, object] = field(default_factory=dict)
    checked_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    icon: str = ""  # SVG icon for the template


def _expected_celery_queues() -> list[str]:
    raw = os.getenv("CELERY_EXPECTED_QUEUES", "")
    if not raw.strip():
        return list(_DEFAULT_CELERY_QUEUES)
    queues = [item.strip() for item in raw.split(",") if item.strip()]
    return queues or list(_DEFAULT_CELERY_QUEUES)


def _celery_queue_restart_targets() -> dict[str, str]:
    raw = os.getenv("CELERY_QUEUE_RESTART_TARGETS", "")
    if not raw.strip():
        return dict(_DEFAULT_CELERY_QUEUE_RESTART_TARGETS)

    targets: dict[str, str] = {}
    for item in raw.split(","):
        if "=" not in item:
            continue
        queue, target = item.split("=", 1)
        queue = queue.strip()
        target = target.strip()
        if queue and target:
            targets[queue] = target
    return targets or dict(_DEFAULT_CELERY_QUEUE_RESTART_TARGETS)


def _celery_worker_restart_enabled() -> bool:
    return os.getenv("CELERY_WORKER_RESTART_ENABLED", "true").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _celery_unavailable_details(error: str) -> dict[str, object]:
    expected_queues = _expected_celery_queues()
    return {
        "error": error,
        "workers": [],
        "worker_details": [],
        "expected_queues": expected_queues,
        "missing_queues": expected_queues,
        "queue_restart_targets": _celery_queue_restart_targets(),
        "restart_enabled": _celery_worker_restart_enabled(),
    }


def check_all_services(db: Session) -> list[ServiceStatus]:
    """Run health checks against all infrastructure services.

    Each check is independent — a failure in one does not affect others.
    """
    checks = [
        ("postgres", "PostgreSQL", _check_postgres),
        ("redis", "Redis", _check_redis),
        ("victoriametrics", "VictoriaMetrics", _check_victoriametrics),
        ("genieacs", "GenieACS", _check_genieacs),
        ("radius_db", "RADIUS DB", _check_radius_db),
        ("minio", "MinIO", _check_minio),
        ("celery", "Celery", _check_celery),
        ("nominatim", "Nominatim", _check_nominatim),
    ]
    skipped = _skipped_health_checks()
    results: list[ServiceStatus] = []
    for key, display_name, check_fn in checks:
        if key in skipped:
            results.append(
                ServiceStatus(
                    name=display_name,
                    status="not_configured",
                    details={
                        "reason": "disabled_by_configuration",
                        "check": key,
                    },
                )
            )
            continue
        try:
            result = check_fn(db)
            results.append(result)
        except Exception as exc:
            logger.error("Infrastructure check %s crashed: %s", check_fn.__name__, exc)
            results.append(
                ServiceStatus(
                    name=check_fn.__name__.replace("_check_", "").title(),
                    status="down",
                    details={"error": str(exc)},
                )
            )
    return results


# ── Individual checks ────────────────────────────────────────────────


def _check_postgres(db: Session) -> ServiceStatus:
    """Check PostgreSQL via the existing DB session."""
    from sqlalchemy import text

    start = time.monotonic()
    try:
        row = db.execute(text("SELECT version()")).scalar()
        activity = _postgres_activity_snapshot(db)
        elapsed = (time.monotonic() - start) * 1000
        version = ""
        if row:
            # "PostgreSQL 16.3 (Debian 16.3-1.pgdg120+1) on x86_64..."
            parts = str(row).split()
            if len(parts) >= 2:
                version = parts[1]
        status = "up"
        if (
            activity.get("idle_in_transaction_over_60s", 0) > 0
            or activity.get("connection_utilization_pct", 0) >= 80
        ):
            status = "degraded"
        return ServiceStatus(
            name="PostgreSQL",
            status=status,
            version=version,
            response_ms=round(elapsed, 1),
            details=activity,
            icon=_ICON_DATABASE,
        )
    except Exception as exc:
        elapsed = (time.monotonic() - start) * 1000
        try:
            db.rollback()
        except Exception:
            logger.debug(
                "Rollback failed after PostgreSQL health check error", exc_info=True
            )
        return ServiceStatus(
            name="PostgreSQL",
            status="down",
            response_ms=round(elapsed, 1),
            details={"error": str(exc)[:200]},
            icon=_ICON_DATABASE,
        )


def _postgres_activity_snapshot(db: Session) -> dict[str, object]:
    """Return connection and transaction age indicators for stale-session alerts."""
    from sqlalchemy import text

    row = (
        db.execute(
            text(
                """
            SELECT
                count(*)::int AS total_connections,
                count(*) FILTER (WHERE state = 'active')::int AS active_connections,
                count(*) FILTER (WHERE state = 'idle')::int AS idle_connections,
                count(*) FILTER (WHERE state = 'idle in transaction')::int AS idle_in_transaction,
                count(*) FILTER (
                    WHERE state = 'idle in transaction'
                    AND now() - COALESCE(xact_start, state_change) > interval '60 seconds'
                )::int AS idle_in_transaction_over_60s,
                COALESCE(
                    EXTRACT(EPOCH FROM max(now() - COALESCE(xact_start, state_change))
                        FILTER (WHERE state = 'idle in transaction')),
                    0
                )::float AS max_idle_in_transaction_seconds,
                count(*) FILTER (WHERE wait_event_type = 'Lock')::int AS waiting_on_lock,
                (SELECT setting::int FROM pg_settings WHERE name = 'max_connections') AS max_connections
            FROM pg_stat_activity
            """
            )
        )
        .mappings()
        .first()
    )
    if not row:
        return {}
    max_connections = int(row["max_connections"] or 0)
    total = int(row["total_connections"] or 0)
    utilization = round(total / max_connections * 100, 1) if max_connections else 0
    return {
        "total_connections": total,
        "active_connections": int(row["active_connections"] or 0),
        "idle_connections": int(row["idle_connections"] or 0),
        "idle_in_transaction": int(row["idle_in_transaction"] or 0),
        "idle_in_transaction_over_60s": int(row["idle_in_transaction_over_60s"] or 0),
        "max_idle_in_transaction_seconds": round(
            float(row["max_idle_in_transaction_seconds"] or 0), 1
        ),
        "waiting_on_lock": int(row["waiting_on_lock"] or 0),
        "max_connections": max_connections,
        "connection_utilization_pct": utilization,
    }


def publish_database_pressure_snapshot(
    service: ServiceStatus, *, now: datetime | None = None
) -> bool:
    """Publish bounded PostgreSQL pressure state for scrape-time export."""
    from app.services.observability import StateObservation, publish_state_snapshot

    details = service.details if isinstance(service.details, dict) else {}
    observations = [
        StateObservation(
            signal="probe_success",
            scope="postgres",
            value=1.0 if service.status in {"up", "degraded"} else 0.0,
        )
    ]
    for signal in (
        "total_connections",
        "active_connections",
        "idle_connections",
        "idle_in_transaction",
        "idle_in_transaction_over_60s",
        "max_idle_in_transaction_seconds",
        "waiting_on_lock",
        "max_connections",
        "connection_utilization_pct",
    ):
        value = details.get(signal)
        if isinstance(value, (int, float)):
            observations.append(
                StateObservation(signal=signal, scope="postgres", value=float(value))
            )
    if isinstance(service.response_ms, (int, float)):
        observations.append(
            StateObservation(
                signal="response_ms",
                scope="postgres",
                value=float(service.response_ms),
            )
        )
    status = (
        "ok"
        if service.status == "up"
        else "degraded"
        if service.status == "degraded"
        else "error"
    )
    return publish_state_snapshot(
        "database_pressure",
        observations,
        status=status,
        now=now,
    )


def _check_redis(db: Session) -> ServiceStatus:
    """Check Redis via the centralized client with circuit breaker."""
    from app.services.redis_client import get_circuit_state, get_redis

    start = time.monotonic()
    circuit_state = get_circuit_state()

    try:
        client = get_redis()
        if client is None:
            elapsed = (time.monotonic() - start) * 1000
            details: dict[str, object] = {
                "error": "No Redis client available",
                "circuit_open": circuit_state["circuit_open"],
                "failure_count": circuit_state["failure_count"],
            }
            if circuit_state["retry_after_seconds"] > 0:
                details["retry_after_seconds"] = round(
                    circuit_state["retry_after_seconds"], 1
                )
            return ServiceStatus(
                name="Redis",
                status="down",
                details=details,
                icon=_ICON_CACHE,
            )
        client.ping()
        all_info: dict = client.info()  # type: ignore[assignment]
        elapsed = (time.monotonic() - start) * 1000
        version = str(all_info.get("redis_version", ""))
        uptime_seconds = all_info.get("uptime_in_seconds", 0)
        used_memory_human = str(all_info.get("used_memory_human", ""))
        connected_clients = all_info.get("connected_clients", 0)

        # Calculate hit rate
        hits = all_info.get("keyspace_hits", 0)
        misses = all_info.get("keyspace_misses", 0)
        hit_rate = None
        if hits + misses > 0:
            hit_rate = round(hits / (hits + misses) * 100, 1)

        details = {
            "memory": used_memory_human,
            "clients": connected_clients,
            "circuit_open": False,
        }
        if hit_rate is not None:
            details["hit_rate"] = f"{hit_rate}%"
        if uptime_seconds:
            days = int(uptime_seconds) // 86400
            hours = (int(uptime_seconds) % 86400) // 3600
            details["uptime"] = f"{days}d {hours}h" if days else f"{hours}h"

        return ServiceStatus(
            name="Redis",
            status="up",
            version=version,
            response_ms=round(elapsed, 1),
            details=details,
            icon=_ICON_CACHE,
        )
    except Exception as exc:
        elapsed = (time.monotonic() - start) * 1000
        return ServiceStatus(
            name="Redis",
            status="down",
            response_ms=round(elapsed, 1),
            details={
                "error": str(exc)[:200],
                "circuit_open": circuit_state["circuit_open"],
                "failure_count": circuit_state["failure_count"],
            },
            icon=_ICON_CACHE,
        )


def _check_victoriametrics(db: Session) -> ServiceStatus:
    """Check VictoriaMetrics via its build info API."""
    url = os.getenv("VICTORIAMETRICS_URL", "http://victoriametrics:8428")
    start = time.monotonic()
    try:
        resp = httpx.get(
            f"{url.rstrip('/')}/api/v1/status/buildinfo",
            timeout=_CHECK_TIMEOUT,
        )
        elapsed = (time.monotonic() - start) * 1000
        version = ""
        if resp.status_code == 200:
            data = resp.json()
            # VictoriaMetrics returns {"status":"success","data":{"version":"..."}}
            version = data.get("data", {}).get("version", "") or data.get("version", "")
        return ServiceStatus(
            name="VictoriaMetrics",
            status="up" if resp.status_code == 200 else "degraded",
            version=version,
            response_ms=round(elapsed, 1),
            icon=_ICON_CHART,
        )
    except Exception as exc:
        elapsed = (time.monotonic() - start) * 1000
        return ServiceStatus(
            name="VictoriaMetrics",
            status="down",
            response_ms=round(elapsed, 1),
            details={"error": str(exc)[:200]},
            icon=_ICON_CHART,
        )


def _check_genieacs(db: Session) -> ServiceStatus:
    """Check GenieACS NBI API reachability."""
    from app.models.tr069 import Tr069AcsServer

    start = time.monotonic()
    try:
        # Get base_url from first active ACS server
        from sqlalchemy import select

        server = db.scalars(
            select(Tr069AcsServer).where(Tr069AcsServer.is_active.is_(True)).limit(1)
        ).first()
        if not server:
            return ServiceStatus(
                name="GenieACS",
                status="degraded",
                details={"error": "No ACS server configured"},
                icon=_ICON_DEVICE,
            )

        base_url = server.base_url.rstrip("/")
        resp = httpx.get(
            f"{base_url}/devices/?projection=_id&limit=1",
            timeout=_CHECK_TIMEOUT,
        )
        elapsed = (time.monotonic() - start) * 1000
        device_count = None
        if resp.status_code == 200:
            try:
                device_count = len(resp.json())
            except Exception:
                logger.debug(
                    "Failed to decode GenieACS device count response",
                    exc_info=True,
                )

        details: dict[str, object] = {"url": base_url}
        if device_count is not None:
            details["reachable"] = True

        return ServiceStatus(
            name="GenieACS",
            status="up" if resp.status_code == 200 else "degraded",
            version=server.name,
            response_ms=round(elapsed, 1),
            details=details,
            icon=_ICON_DEVICE,
        )
    except Exception as exc:
        elapsed = (time.monotonic() - start) * 1000
        return ServiceStatus(
            name="GenieACS",
            status="down",
            response_ms=round(elapsed, 1),
            details={"error": str(exc)[:200]},
            icon=_ICON_DEVICE,
        )


def _check_radius_db(db: Session) -> ServiceStatus:
    """Check RADIUS database reachability via TCP connect."""
    host = os.getenv("RADIUS_DB_HOST", "radius-db")
    try:
        port = int(os.getenv("RADIUS_DB_PORT", "5432"))
    except ValueError:
        port = 5432
    start = time.monotonic()
    try:
        sock = socket.create_connection((host, port), timeout=_CHECK_TIMEOUT)
        sock.close()
        elapsed = (time.monotonic() - start) * 1000
        return ServiceStatus(
            name="RADIUS DB",
            status="up",
            version=f"{host}:{port}",
            response_ms=round(elapsed, 1),
            details={"host": host, "port": port},
            icon=_ICON_SHIELD,
        )
    except Exception as exc:
        elapsed = (time.monotonic() - start) * 1000
        return ServiceStatus(
            name="RADIUS DB",
            status="down",
            response_ms=round(elapsed, 1),
            details={"error": str(exc)[:200], "host": host, "port": port},
            icon=_ICON_SHIELD,
        )


def _check_minio(db: Session) -> ServiceStatus:
    """Check MinIO/S3 via its health endpoint."""
    url = os.getenv("S3_ENDPOINT_URL", "http://minio:9000")
    start = time.monotonic()
    try:
        resp = httpx.get(
            f"{url.rstrip('/')}/minio/health/live",
            timeout=_CHECK_TIMEOUT,
        )
        elapsed = (time.monotonic() - start) * 1000
        return ServiceStatus(
            name="MinIO",
            status="up" if resp.status_code == 200 else "degraded",
            response_ms=round(elapsed, 1),
            icon=_ICON_STORAGE,
        )
    except Exception as exc:
        elapsed = (time.monotonic() - start) * 1000
        return ServiceStatus(
            name="MinIO",
            status="down",
            response_ms=round(elapsed, 1),
            details={"error": str(exc)[:200]},
            icon=_ICON_STORAGE,
        )


def _check_celery(db: Session) -> ServiceStatus:
    """Check Celery worker availability."""
    start = time.monotonic()
    try:
        from app.celery_app import celery_app
        from app.services.redis_client import get_redis

        inspector = celery_app.control.inspect(timeout=1.0)
        ping_result = inspector.ping()
        active = inspector.active() or {}
        reserved = inspector.reserved() or {}
        scheduled = inspector.scheduled() or {}
        active_queues = inspector.active_queues() or {}
        elapsed = (time.monotonic() - start) * 1000

        if not ping_result:
            return ServiceStatus(
                name="Celery",
                status="down",
                response_ms=round(elapsed, 1),
                details=_celery_unavailable_details("No workers responding"),
                icon=_ICON_WORKER,
            )

        worker_names = sorted(ping_result.keys())
        expected_queues = _expected_celery_queues()
        restart_targets = _celery_queue_restart_targets()
        queue_consumers: dict[str, list[str]] = {queue: [] for queue in expected_queues}
        worker_details: list[dict[str, object]] = []
        for worker in worker_names:
            queues = [
                str(queue.get("name"))
                for queue in active_queues.get(worker, [])
                if queue.get("name")
            ]
            for queue in queues:
                if queue in queue_consumers:
                    queue_consumers[queue].append(worker)
            worker_restart_targets = sorted(
                {restart_targets[queue] for queue in queues if queue in restart_targets}
            )
            worker_details.append(
                {
                    "name": worker,
                    "queues": queues,
                    "restart_targets": worker_restart_targets,
                    "active_tasks": len(active.get(worker, []) or []),
                    "reserved_tasks": len(reserved.get(worker, []) or []),
                    "scheduled_tasks": len(scheduled.get(worker, []) or []),
                }
            )
        missing_queues = [
            queue for queue, consumers in queue_consumers.items() if not consumers
        ]
        now = time.time()
        long_running: list[dict[str, object]] = []
        active_count = 0
        reserved_count = 0
        scheduled_count = 0
        for worker, tasks in active.items():
            active_count += len(tasks)
            for task in tasks:
                started_at = task.get("time_start")
                age_seconds = now - float(started_at) if started_at else None
                if age_seconds is not None and age_seconds > 1800:
                    long_running.append(
                        {
                            "worker": worker,
                            "task_id": task.get("id"),
                            "task_name": task.get("name"),
                            "age_seconds": round(age_seconds, 1),
                        }
                    )
        for tasks in reserved.values():
            reserved_count += len(tasks)
        for tasks in scheduled.values():
            scheduled_count += len(tasks)

        queue_lengths: dict[str, int] = {}
        redis_client = get_redis()
        if redis_client is not None:
            for queue_name in expected_queues:
                queue_lengths[queue_name] = int(redis_client.llen(queue_name))

        status = "up"
        if (
            missing_queues
            or long_running
            or reserved_count > 100
            or any(v > 500 for v in queue_lengths.values())
        ):
            status = "degraded"
        return ServiceStatus(
            name="Celery",
            status=status,
            version=f"{len(worker_names)} worker{'s' if len(worker_names) != 1 else ''}",
            response_ms=round(elapsed, 1),
            details={
                "workers": worker_names,
                "worker_details": worker_details,
                "expected_queues": expected_queues,
                "missing_queues": missing_queues,
                "queue_restart_targets": restart_targets,
                "restart_enabled": _celery_worker_restart_enabled(),
                "active_tasks": active_count,
                "reserved_tasks": reserved_count,
                "scheduled_tasks": scheduled_count,
                "queue_lengths": queue_lengths,
                "long_running_tasks_over_30m": long_running[:20],
            },
            icon=_ICON_WORKER,
        )
    except Exception as exc:
        elapsed = (time.monotonic() - start) * 1000
        return ServiceStatus(
            name="Celery",
            status="down",
            response_ms=round(elapsed, 1),
            details=_celery_unavailable_details(str(exc)[:200]),
            icon=_ICON_WORKER,
        )


def _check_nominatim(db: Session) -> ServiceStatus:
    """Check Nominatim geocoding service."""
    url = os.getenv("GEOCODING_BASE_URL", "http://nominatim:8080")
    start = time.monotonic()
    try:
        resp = httpx.get(
            f"{url.rstrip('/')}/status",
            timeout=_CHECK_TIMEOUT,
        )
        elapsed = (time.monotonic() - start) * 1000
        return ServiceStatus(
            name="Nominatim",
            status="up" if resp.status_code == 200 else "degraded",
            response_ms=round(elapsed, 1),
            icon=_ICON_MAP,
        )
    except Exception as exc:
        elapsed = (time.monotonic() - start) * 1000
        return ServiceStatus(
            name="Nominatim",
            status="down",
            response_ms=round(elapsed, 1),
            details={"error": str(exc)[:200]},
            icon=_ICON_MAP,
        )


# ── SVG Icons (inline, no external dependencies) ────────────────────

_ICON_DATABASE = '<svg class="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4.03 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4.03 3 9 3s9-1.34 9-3V5"/></svg>'

_ICON_CACHE = '<svg class="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10"/></svg>'

_ICON_CHART = '<svg class="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z"/></svg>'

_ICON_DEVICE = '<svg class="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 3v2m6-2v2M9 19v2m6-2v2M5 9H3m2 6H3m18-6h-2m2 6h-2M7 19h10a2 2 0 002-2V7a2 2 0 00-2-2H7a2 2 0 00-2 2v10a2 2 0 002 2z"/></svg>'

_ICON_SHIELD = '<svg class="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z"/></svg>'

_ICON_STORAGE = '<svg class="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 8h14M5 8a2 2 0 110-4h14a2 2 0 110 4M5 8v10a2 2 0 002 2h10a2 2 0 002-2V8m-9 4h4"/></svg>'

_ICON_WORKER = '<svg class="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.066 2.573c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.573 1.066c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.066-2.573c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z"/><circle cx="12" cy="12" r="3"/></svg>'

_ICON_MAP = '<svg class="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17.657 16.657L13.414 20.9a1.998 1.998 0 01-2.827 0l-4.244-4.243a8 8 0 1111.314 0z"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 11a3 3 0 11-6 0 3 3 0 016 0z"/></svg>'
