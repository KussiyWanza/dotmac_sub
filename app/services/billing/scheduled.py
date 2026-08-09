"""Scheduled billing service runners.

Celery tasks should be transport wrappers. These runners own the session,
transaction, and logging boundary for scheduled billing automation.
"""

from __future__ import annotations

import logging

from billiard.exceptions import SoftTimeLimitExceeded

from app.services import billing_automation as billing_automation_service
from app.services.billing_enforcement_guards import (
    billing_enforcement_health,
    notification_delivery_health,
)
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)
SessionLocal = db_session_adapter.create_session


def run_invoice_cycle() -> dict[str, int]:
    logger.info("Starting billing invoice cycle")
    session = SessionLocal()
    try:
        result = billing_automation_service.run_invoice_cycle(session)
        processed = result.get("subscriptions_billed", 0)
        errors = result.get("errors", 0)
        logger.info(
            "Billing invoice cycle completed: %d billed, %d invoices created, %d errors",
            processed,
            result.get("invoices_created", 0),
            errors,
        )
        session.commit()
        return {"processed": processed, "errors": errors}
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def mark_invoices_overdue() -> dict[str, int]:
    logger.info("Starting overdue invoice detection")
    session = SessionLocal()
    try:
        result = billing_automation_service.mark_overdue_invoices(session)
        logger.info(
            "Overdue detection completed: %d marked",
            result.get("marked_overdue", 0),
        )
        session.commit()
        return result
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def check_billing_switch_health() -> dict:
    """Observe financial enforcement and customer-message delivery health.

    The historical billing master switch is retired. Health failures remain
    operator-visible observations; they cannot turn the lifecycle off.
    """
    session = SessionLocal()
    try:
        enforcement = billing_enforcement_health(session)
        notification = notification_delivery_health(session)
        result = {
            "ok": enforcement.ok and notification.ok,
            "billing_enforcement_health": {
                "ok": enforcement.ok,
                "reasons": enforcement.reasons,
                "details": enforcement.details,
            },
            "notification_delivery_health": {
                "ok": notification.ok,
                "reasons": notification.reasons,
                "details": notification.details,
            },
        }
        if not enforcement.ok:
            logger.error(
                "billing_enforcement_health_failed: reasons=%s details=%s",
                ",".join(enforcement.reasons),
                enforcement.details,
            )
        if not notification.ok:
            logger.error(
                "billing_notification_delivery_unhealthy: reasons=%s details=%s",
                ",".join(notification.reasons),
                notification.details,
            )
        return result
    finally:
        session.close()


def refresh_billing_health_snapshot() -> dict:
    """Single-flight producer for the bounded snapshot consumed by ``/metrics``."""
    from app.services.billing_health import (
        BILLING_HEALTH_SNAPSHOT_LOCK_KEY,
        BILLING_HEALTH_SNAPSHOT_TASK,
    )
    from app.services.observability import record_task_run, record_task_skip

    with db_session_adapter.advisory_lock(
        BILLING_HEALTH_SNAPSHOT_LOCK_KEY,
        timeout_ms=30_000,
    ) as (session, acquired):
        if not acquired:
            streak = record_task_skip(
                BILLING_HEALTH_SNAPSHOT_TASK, reason="already_running"
            )
            return {"skipped": "already_running", "skip_streak": streak}

        try:
            result: dict = {}
            _append_billing_health_snapshot(session, result)
            snapshot = result.get("billing_health")
            if not isinstance(snapshot, dict):
                raise RuntimeError("billing_health_snapshot_failed")
            session.rollback()
            record_task_run(
                BILLING_HEALTH_SNAPSHOT_TASK,
                status="success",
                counters=snapshot,
            )
            return snapshot
        except SoftTimeLimitExceeded:
            session.rollback()
            error = {"error": "billing_health_snapshot_timed_out"}
            logger.error(error["error"])
            record_task_run(
                BILLING_HEALTH_SNAPSHOT_TASK,
                status="error",
                counters=error,
            )
            return error
        except Exception as exc:  # noqa: BLE001 - runner records bounded failure
            session.rollback()
            error = {"error": str(exc)[:500]}
            logger.exception("billing_health_snapshot_refresh_failed")
            record_task_run(
                BILLING_HEALTH_SNAPSHOT_TASK,
                status="error",
                counters=error,
            )
            return error


def _append_billing_health_snapshot(session, result: dict) -> None:
    try:
        from app.services.billing_health import (
            AGED_DRAFT_DAYS,
            billing_health_snapshot,
            publish_billing_health_snapshot,
        )

        health = billing_health_snapshot(session)
        if not publish_billing_health_snapshot(health):
            raise RuntimeError("billing_health_snapshot_publish_failed")
        anomalies = set(health.anomalies)
        result["billing_health"] = {
            "published": True,
            "paid_with_balance_count": health.paid_with_balance_count,
            "last_scanned": health.last_scanned,
            "eligible_active_subs": health.eligible_active_subs,
            "scan_ratio": health.scan_ratio,
            "payments_24h": health.payments_24h,
            "payments_7d_daily_avg": health.payments_7d_daily_avg,
            "payment_volume_ratio": health.payment_volume_ratio,
            "stale_runners": health.stale_runners,
            "covered_but_locked": health.covered_but_locked,
            "unbilled_no_path": health.unbilled_no_path,
            "active_subs_on_terminal_account": (health.active_subs_on_terminal_account),
            "negative_prepaid_balance_count": health.negative_prepaid_balance_count,
            "negative_prepaid_balance_total": (
                None
                if health.negative_prepaid_balance_total is None
                else str(health.negative_prepaid_balance_total)
            ),
            "negative_prepaid_funding_quarantined_count": (
                health.negative_prepaid_funding_quarantined_count
            ),
            "anomalies": sorted(anomalies),
        }
        if "paid_invoices_with_balance" in anomalies:
            logger.error(
                "billing_paid_invoices_with_balance: %d invoices status=paid "
                "carry non-zero balance_due (total %s) - AR-integrity defect",
                health.paid_with_balance_count,
                health.paid_with_balance_total,
            )
        if "invoice_scan_count_low" in anomalies:
            logger.error(
                "billing_invoice_scan_count_low: last run scanned %s of ~%d "
                "eligible subscriptions (ratio %.2f) - cycle may have stopped "
                "scanning a cohort",
                health.last_scanned,
                health.eligible_active_subs,
                health.scan_ratio,
            )
        if "payment_volume_collapse" in anomalies:
            logger.error(
                "billing_payment_volume_collapse: last-24h %d succeeded payments "
                "vs 7d daily avg %.1f (ratio %.2f) - payment intake may be broken",
                health.payments_24h,
                health.payments_7d_daily_avg,
                health.payment_volume_ratio,
            )
        if "runner_heartbeat_stale" in anomalies:
            logger.error(
                "billing_runner_heartbeat_stale: no fresh success for %s - a "
                "billing/collections runner may be stalled or dead",
                ",".join(health.stale_runners),
            )
        if "enforcement_covered_but_locked" in anomalies:
            logger.error(
                "billing_enforcement_covered_but_locked: %d accounts under a "
                "billing lock despite ledger balance >= 0 - wrongful-suspension drift",
                health.covered_but_locked,
            )
        if "active_subs_without_billing_path" in anomalies:
            logger.error(
                "billing_active_subs_without_billing_path: %d active prepaid "
                "subscription(s) have no canonical recurring billing path - "
                "revenue leak",
                health.unbilled_no_path,
            )
        if "negative_prepaid_balances" in anomalies:
            logger.error(
                "billing_negative_prepaid_balances: %d prepaid account(s) have "
                "wallet balance below zero (total exposure %s)",
                health.negative_prepaid_balance_count,
                health.negative_prepaid_balance_total,
            )
        if "prepaid_funding_quarantined" in anomalies:
            # Warning, not error: quarantine is the correct, deliberate outcome
            # for an account with no reviewed opening baseline. It is a data
            # remediation backlog to work down, not a defect to page on.
            logger.warning(
                "billing_prepaid_funding_quarantined: %d prepaid account(s) "
                "have no reviewed opening baseline, so they are excluded from "
                "funding enforcement and from the negative-exposure figures",
                health.negative_prepaid_funding_quarantined_count,
            )
        if "invoices_paid_without_issue" in anomalies:
            logger.error(
                "billing_invoices_paid_without_issue: %d invoice(s) are past "
                "draft with no issue date - they cannot age, cannot go "
                "overdue, and drop out of any read filtering on issue date",
                health.paid_without_issue_count,
            )
        if "account_credit_invariant_violations" in anomalies:
            logger.error(
                "billing_account_credit_invariant_violations: %d account "
                "credit invariant violation(s) - customer credit no longer "
                "reconciles to its allocations",
                health.account_credit_invariant_count,
            )
        if "billing_profile_mismatch" in anomalies:
            logger.error(
                "billing_profile_mismatch: %d account/subscription billing-mode "
                "mismatch(es) - enforcement reads a mode the subscription "
                "contradicts",
                health.billing_profile_mismatch_count,
            )
        if "billing_profile_mixed_modes" in anomalies:
            logger.error(
                "billing_profile_mixed_modes: %d account(s) carry subscriptions "
                "in more than one billing mode, so no single enforcement policy "
                "applies to the account",
                health.billing_profile_mixed_count,
            )
        if "aged_draft_invoices" in anomalies:
            # Warning, not error: a draft is a legitimate holding state and
            # this signal never acts on one. Issue, void, or keep holding are
            # all fine; not knowing the draft exists is not.
            logger.warning(
                "billing_aged_draft_invoices: %d draft invoice(s) older than "
                "%d days totalling %s await a disposition - issue, void, or a "
                "deliberate hold",
                health.aged_draft_count,
                AGED_DRAFT_DAYS,
                health.aged_draft_total,
            )
        if "prepaid_coverage_quarantined" in anomalies:
            # Warning: quarantined coverage evidence is a human review queue
            # with its own SLA-bound work items, not a runtime defect.
            logger.warning(
                "billing_prepaid_coverage_quarantined: %d subscription(s) hold "
                "coverage evidence the repair owner cannot resolve without a "
                "human decision",
                health.prepaid_coverage_quarantined_count,
            )
        if "prepaid_coverage_unresolved" in anomalies:
            logger.error(
                "billing_prepaid_coverage_unresolved: %d subscription(s) have "
                "unresolved coverage projections, so prepaid enforcement is "
                "parked for their accounts",
                health.prepaid_coverage_unresolved_count,
            )
        if "prepaid_coverage_repair_required" in anomalies:
            # Info: the scheduled repair drains this every sweep. It is only
            # notable when it persists, which the alert rule watches for.
            logger.info(
                "billing_prepaid_coverage_repair_required: %d subscription(s) "
                "are repairable by the next scheduled coverage repair",
                health.prepaid_coverage_repairable_count,
            )
        if "negative_prepaid_exposure_unmeasurable" in anomalies:
            logger.error(
                "billing_negative_prepaid_exposure_unmeasurable: prepaid "
                "exposure could not be measured, so a real figure is being "
                "reported as unavailable rather than as a false zero"
            )
    except SoftTimeLimitExceeded:
        raise
    except Exception:
        logger.exception(
            "billing_health_snapshot_failed: monitoring snapshot raised; "
            "skipping liveness anomaly alerts (enforcement guard unaffected)"
        )


def audit_cutover_balance_invariant() -> dict:
    from app.services.cutover_balance_audit import audit_cutover_balance_invariant

    session = SessionLocal()
    try:
        result = audit_cutover_balance_invariant(session)
        if result.get("ok"):
            logger.info(
                "cutover_balance_invariant_ok: population=%s",
                result.get("population"),
            )
        else:
            logger.error(
                "cutover_balance_invariant_drift: population=%s raw_drift=%s "
                "unregistered_drift=%s overcredited=%s/%s understated=%s/%s "
                "inactive_seed_drift=%s post_adjustment_drift=%s "
                "post_adjustments=%s/%s excluded_adjustments=%s/%s "
                "registered_variance=%s/%s stale_registered_variance=%s",
                result.get("population"),
                result.get("raw_drift_count"),
                result.get("drift_count"),
                result.get("overcredited_count"),
                result.get("overcredited_total"),
                result.get("understated_count"),
                result.get("understated_total"),
                result.get("inactive_seed_drift_count"),
                result.get("post_adjustment_drift_count"),
                result.get("post_adjustment_entry_count"),
                result.get("post_adjustment_net"),
                result.get("excluded_adjustment_entry_count"),
                result.get("excluded_adjustment_net"),
                result.get("registered_variance_count"),
                result.get("registered_variance_total"),
                result.get("stale_registered_variance_count"),
            )
        return result
    finally:
        session.close()


def audit_funded_inactive_exposure() -> dict:
    from app.services.funded_inactive_exposure import funded_inactive_exposure

    session = SessionLocal()
    try:
        result = funded_inactive_exposure(session)
        log_fn = logger.error if result.get("refund_review_count") else logger.info
        log_fn(
            "funded_inactive_exposure: ok=%s inactive_positive=%s/%s "
            "refund_review=%s/%s disabled=%s/%s canceled=%s/%s "
            "suspended=%s/%s blocked=%s/%s soft_deleted=%s/%s "
            "sibling_candidates=%s material=%s candidates=%s "
            "invalid_profiles=%s non_prepaid=%s funding_source=%s",
            result.get("ok"),
            result.get("inactive_positive_count"),
            result.get("inactive_positive_total"),
            result.get("refund_review_count"),
            result.get("refund_review_total"),
            result.get("disabled_count"),
            result.get("disabled_total"),
            result.get("canceled_count"),
            result.get("canceled_total"),
            result.get("suspended_count"),
            result.get("suspended_total"),
            result.get("blocked_count"),
            result.get("blocked_total"),
            result.get("soft_deleted_count"),
            result.get("soft_deleted_total"),
            result.get("sibling_candidate_count"),
            result.get("material_count"),
            result.get("candidate_count"),
            result.get("invalid_billing_profile_count"),
            result.get("non_prepaid_candidate_count"),
            result.get("funding_source"),
        )
        return result
    finally:
        session.close()


def run_billing_notifications() -> dict[str, int | bool]:
    logger.info("Starting billing notifications run")
    session = SessionLocal()
    try:
        result = billing_automation_service.run_billing_notifications(session)
        logger.info(
            "Billing notifications run completed: %s reminders%s",
            result.get("invoice_reminders_sent", 0),
            " (outside send window)" if result.get("skipped_outside_window") else "",
        )
        session.commit()
        return result
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
