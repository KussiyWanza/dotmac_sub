"""Shared observability recording helpers.

This is a thin policy boundary over existing sinks: Prometheus metrics,
Redis-backed task/job heartbeats, and admin alerts. It gives domain code one
place to record operational signals without each task reimplementing routing.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models.network_monitoring import AlertSeverity
from app.services import admin_alerts, job_heartbeat, task_heartbeat

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Finding:
    fingerprint: str
    domain: str
    source: str
    severity: AlertSeverity
    title: str
    summary: str
    details: dict[str, Any] = field(default_factory=dict)
    target_url: str = "/admin/system/health"


@dataclass(frozen=True)
class StateObservation:
    signal: str
    scope: str
    value: float


_STATE_SNAPSHOT_SPECS = {
    "billing_health": {"max_observations": 64, "ttl_seconds": 7 * 86_400},
    # Raw freshness plus the validated health-contract signals per external
    # channel; the channel set is bounded by InboxChannelType.
    "channel_ingestion": {"max_observations": 96, "ttl_seconds": 86_400},
    "celery_queues": {"max_observations": 64, "ttl_seconds": 86_400},
    "credentials": {"max_observations": 500, "ttl_seconds": 7 * 86_400},
    "database_pressure": {"max_observations": 16, "ttl_seconds": 86_400},
    "nas_lifecycle": {"max_observations": 32, "ttl_seconds": 7 * 86_400},
    "network_operations": {"max_observations": 32, "ttl_seconds": 86_400},
    # Prepaid enforcement + transitional coverage-repair counts published by
    # every prepaid_balance_sweep run; snapshot age doubles as its heartbeat.
    "prepaid_enforcement": {"max_observations": 32, "ttl_seconds": 7 * 86_400},
    "router_sot": {"max_observations": 16, "ttl_seconds": 7 * 86_400},
}
_STATE_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]+$")
_STATE_STATUSES = frozenset({"ok", "degraded", "error"})


def state_snapshot_domains() -> tuple[str, ...]:
    """Registered bounded-cardinality state domains exported by /metrics."""
    return tuple(sorted(_STATE_SNAPSHOT_SPECS))


def _state_snapshot_key(domain: str) -> str:
    from app.services.app_cache import cache_key

    return cache_key("observability", "state", domain)


def _validate_state_token(value: str, *, label: str, max_length: int) -> str:
    normalized = str(value).strip()
    if (
        not normalized
        or len(normalized) > max_length
        or not _STATE_TOKEN.fullmatch(normalized)
    ):
        raise ValueError(f"Invalid observability {label}")
    return normalized


def publish_state_snapshot(
    domain: str,
    observations: Iterable[StateObservation],
    *,
    status: str = "ok",
    now: datetime | None = None,
) -> bool:
    """Persist a redacted latest-state snapshot for web-process metric export."""
    spec = _STATE_SNAPSHOT_SPECS.get(domain)
    if spec is None:
        raise ValueError("Unregistered observability state domain")
    if status not in _STATE_STATUSES:
        raise ValueError("Invalid observability snapshot status")

    normalized: dict[tuple[str, str], float] = {}
    for observation in observations:
        signal = _validate_state_token(
            observation.signal,
            label="signal",
            max_length=80,
        )
        scope = _validate_state_token(
            observation.scope,
            label="scope",
            max_length=160,
        )
        value = float(observation.value)
        if not math.isfinite(value):
            raise ValueError("Observability state values must be finite")
        normalized[(signal, scope)] = value
    if len(normalized) > int(spec["max_observations"]):
        raise ValueError("Observability snapshot exceeds registered cardinality")

    payload = {
        "domain": domain,
        "status": status,
        "observed_at": (now or datetime.now(UTC)).astimezone(UTC).isoformat(),
        "observations": [
            {"signal": signal, "scope": scope, "value": value}
            for (signal, scope), value in sorted(normalized.items())
        ],
    }
    from app.services.app_cache import set_json

    stored = set_json(
        _state_snapshot_key(domain),
        payload,
        int(spec["ttl_seconds"]),
    )
    logger.info(
        "observability_state_snapshot domain=%s status=%s stored=%s "
        "observations=%s nonzero=%s",
        domain,
        status,
        stored,
        len(normalized),
        [
            {"signal": signal, "scope": scope, "value": value}
            for (signal, scope), value in sorted(normalized.items())
            if value
        ],
    )
    return stored


def load_state_snapshot(domain: str) -> dict[str, Any] | None:
    """Load a registered state snapshot; malformed cache data is ignored."""
    if domain not in _STATE_SNAPSHOT_SPECS:
        return None
    try:
        from app.services.app_cache import get_json

        payload = get_json(_state_snapshot_key(domain))
    except Exception:
        logger.debug(
            "observability_state_snapshot_load_failed domain=%s",
            domain,
            exc_info=True,
        )
        return None
    if not isinstance(payload, dict) or payload.get("domain") != domain:
        return None
    return payload


def record_task_run(
    task_name: str,
    *,
    status: str,
    counters: dict[str, Any] | None = None,
    duration_seconds: float | None = None,
    next_attempt_at: datetime | None = None,
    now: datetime | None = None,
) -> None:
    """Record a task run across the existing heartbeat/metrics sinks."""
    if not task_name:
        return
    normalized_status = "ok" if status in {"ok", "success"} else status
    metric_status = status or normalized_status
    counters = counters if isinstance(counters, dict) else {}
    try:
        if normalized_status == "ok":
            job_heartbeat.record_success(task_name, now=now)
            task_heartbeat.record_success(task_name, counters, now=now)
        job_heartbeat.record_result(
            task_name,
            status=normalized_status,
            detail=counters,
            next_attempt_at=next_attempt_at,
            now=now,
        )
    except Exception:
        logger.exception("observability_task_run_record_failed task=%s", task_name)

    try:
        from app.metrics import OBSERVABILITY_EVENTS_TOTAL, observe_job

        OBSERVABILITY_EVENTS_TOTAL.labels(
            domain="task",
            signal=task_name,
            status=normalized_status,
        ).inc()
        if duration_seconds is not None:
            observe_job(task_name, metric_status, duration_seconds)
    except Exception:
        logger.debug(
            "observability_task_metrics_record_failed task=%s",
            task_name,
            exc_info=True,
        )


def record_task_skip(
    task_name: str,
    *,
    reason: str = "skipped",
    now: datetime | None = None,
) -> int:
    """Record a task skip and return the consecutive skip streak."""
    if not task_name:
        return 0
    streak = 0
    detail: dict[str, Any] = {"reason": reason}
    try:
        streak = task_heartbeat.record_skip(task_name)
        detail["skip_streak"] = streak
        job_heartbeat.record_result(
            task_name,
            status="skipped",
            detail=detail,
            now=now,
        )
    except Exception:
        logger.exception("observability_task_skip_record_failed task=%s", task_name)

    try:
        from app.metrics import OBSERVABILITY_EVENTS_TOTAL

        OBSERVABILITY_EVENTS_TOTAL.labels(
            domain="task",
            signal=task_name,
            status="skipped",
        ).inc()
    except Exception:
        logger.debug(
            "observability_task_skip_metrics_record_failed task=%s",
            task_name,
            exc_info=True,
        )
    return streak


def record_celery_task_success(
    task_name: str,
    *,
    result: Any = None,
    now: datetime | None = None,
) -> None:
    """Record framework-level Celery task success signals."""
    if not task_name:
        return
    detail = result if isinstance(result, dict) else None
    try:
        job_heartbeat.record_success(task_name, now=now)
        if task_name in job_heartbeat.MONEY_JOB_TASKS:
            status = "ok"
            if (
                task_name == job_heartbeat.PAYMENT_RECONCILIATION_TASK
                and isinstance(detail, dict)
                and int(detail.get("errors") or 0) > 0
            ):
                status = "partial"
            job_heartbeat.record_result(
                task_name,
                status=status,
                detail=detail,
                now=now,
            )
    except Exception:
        logger.debug(
            "observability_celery_success_record_failed task=%s",
            task_name,
            exc_info=True,
        )

    try:
        from app.metrics import OBSERVABILITY_EVENTS_TOTAL

        OBSERVABILITY_EVENTS_TOTAL.labels(
            domain="celery",
            signal=task_name,
            status="success",
        ).inc()
    except Exception:
        logger.debug(
            "observability_celery_success_metrics_failed task=%s",
            task_name,
            exc_info=True,
        )


def record_celery_task_failure(
    task_name: str,
    *,
    error: str | None = None,
    now: datetime | None = None,
) -> None:
    """Record framework-level Celery task failure signals."""
    if not task_name:
        return
    try:
        if task_name in job_heartbeat.MONEY_JOB_TASKS:
            msg = error if error is not None else "unknown error"
            job_heartbeat.record_result(
                task_name,
                status="error",
                detail={"error": msg[:500]},
                now=now,
            )
    except Exception:
        logger.debug(
            "observability_celery_failure_record_failed task=%s",
            task_name,
            exc_info=True,
        )

    try:
        from app.metrics import OBSERVABILITY_EVENTS_TOTAL

        OBSERVABILITY_EVENTS_TOTAL.labels(
            domain="celery",
            signal=task_name,
            status="error",
        ).inc()
    except Exception:
        logger.debug(
            "observability_celery_failure_metrics_failed task=%s",
            task_name,
            exc_info=True,
        )


def record_metric(
    *,
    domain: str,
    signal: str,
    status: str = "observed",
    count: int = 1,
) -> None:
    """Record a lightweight Prometheus counter signal."""
    if count <= 0:
        return
    try:
        from app.metrics import OBSERVABILITY_EVENTS_TOTAL

        OBSERVABILITY_EVENTS_TOTAL.labels(
            domain=domain,
            signal=signal,
            status=status,
        ).inc(count)
    except Exception:
        logger.debug(
            "observability_metric_record_failed domain=%s signal=%s",
            domain,
            signal,
            exc_info=True,
        )


def record_finding(db: Session, finding: Finding) -> str:
    """Sync a single operational finding into the admin alert lifecycle."""
    return admin_alerts.sync_alert(
        db,
        admin_alerts.AlertFinding(
            fingerprint=finding.fingerprint,
            category=finding.domain,
            source=finding.source,
            severity=finding.severity,
            title=finding.title,
            summary=finding.summary,
            details=finding.details,
            target_url=finding.target_url,
        ),
    )


def resolve_findings(
    db: Session,
    *,
    managed_prefix: str,
    active_fingerprints: set[str],
) -> int:
    """Resolve admin alerts under a managed prefix that are no longer active."""
    return admin_alerts.resolve_missing_alerts(
        db,
        managed_prefix=managed_prefix,
        active_fingerprints=active_fingerprints,
    )


def record_notification_queue_result(
    db: Session,
    *,
    task_name: str,
    result: dict[str, int],
    duration_seconds: float | None = None,
) -> None:
    """Record notification delivery batch counters and failure findings."""
    counters = {key: int(value or 0) for key, value in result.items()}
    record_task_run(
        task_name,
        status="ok",
        counters=counters,
        duration_seconds=duration_seconds,
        now=datetime.now(UTC),
    )
    try:
        from app.metrics import NOTIFICATION_QUEUE_OUTCOMES_TOTAL

        for outcome, count in counters.items():
            if count > 0:
                NOTIFICATION_QUEUE_OUTCOMES_TOTAL.labels(outcome=outcome).inc(count)
    except Exception:
        logger.debug("notification_queue_metrics_record_failed", exc_info=True)

    failed = counters.get("failed", 0) + counters.get("talk_failed", 0)
    stuck_dropped = counters.get("stuck_dropped", 0)
    stale_due = counters.get("stale_due", 0)
    if failed <= 0 and stuck_dropped <= 0 and stale_due <= 0:
        try:
            resolve_findings(
                db,
                managed_prefix="observability:notification:queue-failures",
                active_fingerprints=set(),
            )
        except Exception:
            logger.exception("notification_queue_finding_resolution_failed")
        return

    severity = (
        AlertSeverity.critical
        if failed >= 10 or stale_due >= 100
        else AlertSeverity.warning
    )
    try:
        record_finding(
            db,
            Finding(
                fingerprint="observability:notification:queue-failures",
                domain="notification",
                source="notification_queue",
                severity=severity,
                title="Notification queue requires attention",
                summary=(
                    f"{failed} notification(s) failed; "
                    f"{stuck_dropped} stuck send(s) dropped; "
                    f"{stale_due} due notification(s) remain stale."
                ),
                details=counters,
                target_url="/admin/notifications",
            ),
        )
    except Exception:
        logger.exception("notification_queue_finding_record_failed")
