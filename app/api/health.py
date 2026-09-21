"""Health check API endpoints for infrastructure monitoring.

Provides lightweight health endpoints for:
- Application liveness (is the process running?)
- Application readiness (can it handle requests?)
- Redis connectivity and circuit breaker status
- Database connectivity
- Full infrastructure health

These endpoints are designed for use with Kubernetes probes,
load balancer health checks, and monitoring systems.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
def liveness() -> dict[str, str]:
    """Liveness probe - is the application process running?

    This should always return 200 if the process is alive.
    Used by Kubernetes liveness probes.
    """
    return {"status": "alive"}


@router.get("/ready")
def readiness(request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Readiness probe - can the application handle requests?

    Checks that critical dependencies (database) are available.
    Used by Kubernetes readiness probes.
    """
    status = "ready"
    checks: dict[str, Any] = {}
    if not bool(getattr(request.app.state, "routes_ready", False)):
        return {"status": "not_ready", "checks": {"routes": {"status": "starting"}}}

    # Check database
    try:
        start = time.monotonic()
        db.execute(text("SELECT 1"))
        checks["database"] = {
            "status": "up",
            "response_ms": round((time.monotonic() - start) * 1000, 2),
        }
    except Exception as exc:
        status = "not_ready"
        checks["database"] = {"status": "down", "error": str(exc)[:100]}

    return {"status": status, "checks": checks}


@router.get("/redis")
def redis_health(
    force: bool = Query(False, description="Force fresh check, bypass cache"),
) -> dict[str, Any]:
    """Detailed Redis health check with circuit breaker status.

    Returns comprehensive Redis health information including:
    - Connection status
    - Circuit breaker state
    - Memory usage
    - Connection count
    - Hit/miss rates
    """
    from app.services.redis_client import get_circuit_state, redis_health_check

    health = redis_health_check(force=force)
    circuit = get_circuit_state()

    return {
        "status": "up" if health.get("available") else "down",
        "circuit_breaker": circuit,
        "details": {
            k: v
            for k, v in health.items()
            if k not in ("available", "_check_time", "circuit_open", "failure_count")
        },
    }


@router.get("/redis/reset")
def reset_redis_circuit() -> dict[str, Any]:
    """Reset the Redis circuit breaker and attempt reconnection.

    Use this to manually recover from a Redis outage after the
    underlying issue has been resolved.
    """
    from app.services.redis_client import (
        get_circuit_state,
        get_redis,
        reset_redis_client,
    )

    # Reset state
    reset_redis_client()

    # Attempt reconnection
    client = get_redis(force_reconnect=True)

    return {
        "status": "reconnected" if client else "failed",
        "circuit_breaker": get_circuit_state(),
    }


@router.get("/infrastructure")
def infrastructure_health(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Full infrastructure health check.

    Checks all infrastructure dependencies:
    - PostgreSQL
    - Redis
    - VictoriaMetrics
    - GenieACS
    - RADIUS DB
    - MinIO
    - Celery workers
    - Nominatim

    This is a heavier check - use sparingly.
    """
    from app.services.infrastructure_health import check_all_services

    services = check_all_services(db)

    # Determine overall status
    statuses = [s.status for s in services]
    if all(s == "up" for s in statuses):
        overall = "healthy"
    elif any(s == "down" for s in statuses):
        overall = "degraded"
    else:
        overall = "partial"

    return {
        "status": overall,
        "services": [
            {
                "name": s.name,
                "status": s.status,
                "version": s.version or None,
                "response_ms": s.response_ms,
                "details": s.details or None,
            }
            for s in services
        ],
    }


@router.get("/db")
def database_health(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Detailed database health check.

    Returns database connection info and basic statistics.
    """
    from sqlalchemy import text

    result: dict[str, Any] = {"status": "unknown"}

    try:
        start = time.monotonic()

        # Version
        version_row = db.execute(text("SELECT version()")).scalar()
        result["response_ms"] = round((time.monotonic() - start) * 1000, 2)

        if version_row:
            parts = str(version_row).split()
            result["version"] = parts[1] if len(parts) >= 2 else str(version_row)[:50]

        # Connection info
        conn_info = db.execute(
            text(
                """
            SELECT
                (SELECT count(*) FROM pg_stat_activity) as active_connections,
                (SELECT setting::int FROM pg_settings WHERE name = 'max_connections') as max_connections
            """
            )
        ).first()

        if conn_info:
            result["connections"] = {
                "active": conn_info[0],
                "max": conn_info[1],
                "utilization_pct": round(conn_info[0] / conn_info[1] * 100, 1)
                if conn_info[1]
                else None,
            }

        activity = (
            db.execute(
                text(
                    """
                SELECT
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
                    count(*) FILTER (WHERE wait_event_type = 'Lock')::int AS waiting_on_lock
                FROM pg_stat_activity
                """
                )
            )
            .mappings()
            .first()
        )
        degraded = False
        if activity:
            result["stale_sessions"] = {
                "idle_in_transaction": int(activity["idle_in_transaction"] or 0),
                "idle_in_transaction_over_60s": int(
                    activity["idle_in_transaction_over_60s"] or 0
                ),
                "max_idle_in_transaction_seconds": round(
                    float(activity["max_idle_in_transaction_seconds"] or 0), 1
                ),
                "waiting_on_lock": int(activity["waiting_on_lock"] or 0),
            }
            if result["stale_sessions"]["idle_in_transaction_over_60s"] > 0:
                degraded = True
                result["warning"] = "idle-in-transaction sessions over 60 seconds"

        result["status"] = "degraded" if degraded else "up"

    except Exception as exc:
        result["status"] = "down"
        result["error"] = str(exc)[:200]

    return result
