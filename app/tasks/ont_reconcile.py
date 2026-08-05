"""Bounded scheduled Huawei ONT desired/observed reconciliation."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter
from app.services.network_operation_dispatch import managed_network_operation_dispatch
from app.tasks._postgres_lock import postgres_session_advisory_lock

logger = logging.getLogger(__name__)

_ADVISORY_LOCK_KEY = 0x6F_6E_74  # "ont"


@dataclass(frozen=True, slots=True)
class OverdueHoldAlertSyncOutcome:
    overdue: int
    opened: int
    escalated: int
    updated: int
    resolved: int

    def as_payload(self) -> dict[str, int]:
        return {
            "overdue": self.overdue,
            "opened": self.opened,
            "escalated": self.escalated,
            "updated": self.updated,
            "resolved": self.resolved,
        }


def _sync_overdue_reconcile_hold_alerts(
    db: Session,
) -> OverdueHoldAlertSyncOutcome:
    """Persist the eligibility owner's alert projections through the sink."""
    from app.models.network_monitoring import AlertSeverity
    from app.services.admin_alerts import (
        AlertFinding,
        sync_managed_alerts,
    )
    from app.services.network.ont_reconcile_eligibility import (
        OVERDUE_ALERT_PREFIX,
        overdue_hold_alerts,
    )

    alerts = overdue_hold_alerts(db)
    findings = tuple(
        AlertFinding(
            fingerprint=alert.fingerprint,
            category="network",
            source="network.ont_reconcile_eligibility",
            severity=AlertSeverity(alert.severity.value),
            title=alert.title,
            summary=alert.summary,
            details={
                "hold_id": alert.hold_id,
                "ont_unit_id": alert.ont_unit_id,
                "scope": alert.scope,
                "reason_code": alert.reason_code,
                "review_due_at": alert.review_due_at,
            },
            target_url=alert.target_url,
        )
        for alert in alerts
    )
    sink_outcome = sync_managed_alerts(
        db,
        findings=findings,
        managed_prefix=OVERDUE_ALERT_PREFIX,
    )
    return OverdueHoldAlertSyncOutcome(
        overdue=len(alerts),
        opened=sink_outcome.opened,
        escalated=sink_outcome.escalated,
        updated=sink_outcome.updated,
        resolved=sink_outcome.resolved,
    )


def _reconcile_payload(result: Any) -> dict[str, Any]:
    failure = result.failure
    failure_payload = None
    if failure is not None:
        failure_payload = {
            "reason": failure.reason,
            "message": failure.message,
        }
        failure_evidence = getattr(failure, "evidence", None)
        if failure_evidence is not None:
            failure_payload["evidence"] = failure_evidence

    actions = []
    for action in result.actions_applied:
        action_payload = {
            "field": action.field,
            "surface": action.surface,
            "duration_ms": action.duration_ms,
        }
        action_evidence = getattr(action, "evidence", None)
        if action_evidence is not None:
            action_payload["evidence"] = action_evidence
        actions.append(action_payload)

    return {
        "success": result.success,
        "sync_status": result.sync_status,
        "duration_ms": result.duration_ms,
        "failure": failure_payload,
        # Values can contain subscriber credentials. Operation history records
        # only which fields changed, not their old/new values.
        "actions": actions,
        "drift_before": [drift.field for drift in result.drift_before],
        "drift_after": [drift.field for drift in result.drift_after],
    }


@celery_app.task(
    name="app.tasks.ont_reconcile.reconcile_huawei_ont",
    soft_time_limit=150,
    time_limit=180,
)
@managed_network_operation_dispatch("app.tasks.ont_reconcile.reconcile_huawei_ont")
def reconcile_huawei_ont(
    ont_id: str,
    operation_id: str,
    *,
    _network_dispatch_id: str | None = None,
) -> dict[str, Any]:
    """Run one tracked desired/observed reconcile and persist its outcome."""
    from app.services.network.reconcile.core import reconcile_ont
    from app.services.network_operations import network_operations

    with db_session_adapter.session() as db:
        try:
            from app.models.network import OntUnit
            from app.services import tr069 as tr069_service
            from app.services.network.acs_resolution import resolve_acs_for_ont

            operation = network_operations.mark_running(db, operation_id)
            parent_id = str(operation.parent_id) if operation.parent_id else None
            db.commit()
            ont = db.get(OntUnit, ont_id)
            desired_acs = (
                resolve_acs_for_ont(db, ont).server if ont is not None else None
            )
            proposed_change = (
                {
                    "acs_url": desired_acs.cwmp_url,
                    "acs_username": desired_acs.cwmp_username,
                    "acs_password_ref": desired_acs.cwmp_password,
                }
                if desired_acs is not None
                else None
            )
            result = reconcile_ont(
                db,
                ont_id,
                proposed_change=proposed_change,
                mode="sweep",
                timeout_sec=120,
            )
            payload = _reconcile_payload(result)
            if result.success:
                if ont is not None and desired_acs is not None:
                    tr069_service.sync_ont_acs_server(db, ont, desired_acs.id)
                network_operations.mark_succeeded(
                    db, operation_id, output_payload=payload
                )
            else:
                message = (
                    result.failure.message
                    if result.failure is not None
                    else "ONT reconciliation did not converge"
                )
                network_operations.mark_failed(
                    db, operation_id, message, output_payload=payload
                )
            if parent_id:
                network_operations.update_parent_status(db, parent_id)
            db.commit()
            return {"ont_id": ont_id, "operation_id": operation_id, **payload}
        except Exception as exc:
            db.rollback()
            try:
                operation = network_operations.get(db, operation_id)
                network_operations.mark_failed(
                    db,
                    operation_id,
                    str(exc),
                    output_payload={"success": False, "message": str(exc)},
                )
                if operation.parent_id:
                    network_operations.update_parent_status(
                        db, str(operation.parent_id)
                    )
                db.commit()
            except Exception:
                db.rollback()
                logger.exception(
                    "Failed to record Huawei ONT reconcile failure for %s",
                    operation_id,
                )
            logger.exception("Queued Huawei ONT reconcile failed for %s", ont_id)
            return {
                "ont_id": ont_id,
                "operation_id": operation_id,
                "success": False,
                "message": str(exc),
            }


def _close_expired_remote_access() -> dict[str, int]:
    from sqlalchemy import select

    from app.models.network import OntUnit
    from app.services.network.ont_desired_config import desired_config
    from app.services.network.ont_features import OntFeatureService
    from app.services.network.reconcile.candidates import (
        restrict_to_reconcile_candidates,
    )

    stats = {"checked": 0, "closed": 0, "failed": 0}
    with db_session_adapter.session() as db:
        # Same population the sweeper walks, from the same predicate: remote
        # access is granted on devices reconciliation owns, so the two must
        # never drift apart.
        onts = list(db.scalars(restrict_to_reconcile_candidates(select(OntUnit))))
        now = datetime.now(UTC)
        for ont in onts:
            access = desired_config(ont).get("access") or {}
            expires_raw = access.get("wan_remote_expires_at")
            if not access.get("wan_remote") or not expires_raw:
                continue
            stats["checked"] += 1
            try:
                expires_at = datetime.fromisoformat(str(expires_raw))
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=UTC)
            except ValueError:
                expires_at = now
            if expires_at > now:
                continue
            result = OntFeatureService.toggle_wan_remote_access(
                db, str(ont.id), enabled=False
            )
            if result.success:
                stats["closed"] += 1
            else:
                stats["failed"] += 1
                logger.warning(
                    "expired_ont_remote_access_close_failed ont=%s message=%s",
                    ont.id,
                    result.message,
                )
    return stats


def _reconcile_dialer_credentials() -> dict[str, Any]:
    """Converge every assigned ONT's PPPoE dialer onto its access credential.

    Runs inside the ONT sweep because the ONT reconciler is what delivers the
    projection to the CPE: projecting first means the very next per-ONT
    reconcile in this same sweep carries the corrected value. Gated on its own
    control so it can be stopped without stopping the ONT sweep.

    Never returns or logs credential values — the stats payload carries
    fingerprint prefixes only.
    """
    from app.services import control_registry
    from app.services.cpe_dialer_credential_reconcile import (
        DialerFingerprintUnavailable,
        reconcile_cpe_dialer_credentials,
    )

    with db_session_adapter.session() as db:
        if not control_registry.is_enabled(db, "network.cpe_dialer_credential_sync"):
            return {"skipped": "cpe_dialer_credential_sync_disabled"}
        try:
            # The owner manages its own transaction; this task is a thin
            # scheduling adapter around it.
            stats = reconcile_cpe_dialer_credentials(db)
        except DialerFingerprintUnavailable as exc:
            logger.error("cpe_dialer_credential_reconcile_unavailable: %s", exc)
            return {"skipped": "credential_encryption_key_missing"}
        return stats.as_payload()


@celery_app.task(
    name="app.tasks.ont_reconcile.alert_overdue_reconcile_holds",
    time_limit=120,
)
def alert_overdue_reconcile_holds() -> dict[str, int]:
    """Surface reconciliation holds past their review date.

    Deliberately NOT gated on ``network.ont_reconcile``. The point of a hold is
    that reconciliation is suppressed, and the fleet-wide control is often off
    while holds are in force -- gating this on it would silence the alarm
    exactly when the holds are active and least supervised.

    A hold is never released here. Overdue is a reporting state; only an
    explicit release command ends a hold.
    """
    with db_session_adapter.session() as db:
        outcome = _sync_overdue_reconcile_hold_alerts(db)

    if outcome.overdue:
        logger.warning(
            "ont_reconcile_holds_overdue",
            extra=outcome.as_payload(),
        )
    return outcome.as_payload()


@celery_app.task(
    name="app.tasks.ont_reconcile.run_ont_reconcile_sweep",
    soft_time_limit=840,
    time_limit=900,
)
def run_ont_reconcile_sweep(max_onts: int = 25) -> dict[str, Any]:
    """Reconcile the least-recently checked active ONTs without overlap."""
    from app.services import control_registry
    from app.services.network.reconcile.sweeper import run_sweep_once

    bounded = max(1, min(int(max_onts), 100))
    with db_session_adapter.session() as db:
        if not control_registry.is_enabled(db, "network.ont_reconcile"):
            return {"skipped": "ont_reconcile_disabled"}
    with postgres_session_advisory_lock(_ADVISORY_LOCK_KEY) as acquired:
        if not acquired:
            return {"skipped": "already_running"}
        stats = run_sweep_once(
            db_session_adapter.create_session,
            timeout_sec=45,
            max_onts=bounded,
            max_duration_sec=720,
        )
        remote_access = _close_expired_remote_access()
        dialer_credentials = _reconcile_dialer_credentials()
        return {
            "total_onts": stats.total_onts,
            "reconciled": stats.reconciled,
            "succeeded": stats.succeeded,
            "failed": stats.failed,
            "deferred": stats.deferred,
            "held": stats.held,
            "skipped_unreachable": stats.skipped_unreachable,
            "errors": stats.errors,
            "duration_sec": stats.duration_sec,
            "remote_access": remote_access,
            "dialer_credentials": dialer_credentials,
        }
