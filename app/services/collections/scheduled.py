"""Scheduled collections service runners."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID

from sqlalchemy.orm import Session

from app.schemas.collections import BillingEnforcementRunRequest
from app.services.collections import billing_enforcement_reconciler
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)
SessionLocal = db_session_adapter.create_session


def run_billing_enforcement() -> dict[str, int | str]:
    logger.info("Starting unified billing enforcement run")
    session = SessionLocal()
    try:
        result = billing_enforcement_reconciler.run(
            session, BillingEnforcementRunRequest()
        )
        summary: dict[str, int | str] = {
            "accounts_scanned": int(result.accounts_scanned),
            "cases_created": int(result.cases_created),
            "actions_created": int(result.actions_created),
            "skipped": int(result.skipped),
            "credit_accounts_scanned": int(result.credit_accounts_scanned),
            "credit_accounts_settled": int(result.credit_accounts_settled),
            "credit_invoices_touched": int(result.credit_invoices_touched),
            "credit_settlement_errors": int(result.credit_settlement_errors),
            "credit_applied": str(result.credit_applied),
        }
        logger.info(
            "Billing enforcement run completed: accounts_scanned=%d cases_created=%d "
            "actions_created=%d skipped=%d",
            summary["accounts_scanned"],
            summary["cases_created"],
            summary["actions_created"],
            summary["skipped"],
        )
        session.commit()
        return summary
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def run_bundle_reconcile() -> dict[str, int]:
    from app.services import bundles

    session = SessionLocal()
    try:
        result = bundles.reconcile_bundle_states(session)
        session.commit()
        logger.info(
            "Bundle reconcile completed: bundles_scanned=%d members_converged=%d",
            result["bundles_scanned"],
            result["members_converged"],
        )
        return {
            "bundles_scanned": int(result["bundles_scanned"]),
            "members_converged": int(result["members_converged"]),
        }
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# One coverage-repair confirmation transaction locks its accounts,
# subscriptions, and source rows FOR UPDATE. Chunking keeps that lock set
# bounded so a large backlog cannot stall enforcement or interactive traffic.
_COVERAGE_REPAIR_CHUNK = 200

_QUARANTINE_FINDING_PREFIX = "prepaid-coverage:quarantine:"
#: Finance review window recorded on each blocking-quarantine work item.
_QUARANTINE_SLA_HOURS = 72

#: The run must finish and publish its snapshot before the Celery soft time
#: limit (840s in production) interrupts a query mid-flight; work that does
#: not fit is deferred to the next run instead of dying unreported.
_DEFAULT_SWEEP_BUDGET_SECONDS = 720


class PrepaidCoverageRepairStatus(StrEnum):
    ok = "ok"
    stale_preview = "stale_preview"
    error = "error"


@dataclass(frozen=True, slots=True)
class PrepaidCoverageRepairOutcome:
    """Typed result of one scheduled coverage-repair pass."""

    subscriptions: int
    repairable: int
    quarantined: int
    quarantined_blocking: int
    entitlements_created: int
    stale_preview_chunks: int
    deferred_subscriptions: int
    status: PrepaidCoverageRepairStatus

    def as_stats(self, prefix: str = "coverage_repair_") -> dict[str, int | str]:
        """Serialize for the Celery/task boundary only."""
        return {
            f"{prefix}repairable": self.repairable,
            f"{prefix}quarantined": self.quarantined,
            f"{prefix}quarantined_blocking": self.quarantined_blocking,
            f"{prefix}entitlements_created": self.entitlements_created,
            f"{prefix}stale_preview_chunks": self.stale_preview_chunks,
            f"{prefix}deferred_subscriptions": self.deferred_subscriptions,
            f"{prefix}status": self.status.value,
        }


_REPAIR_FAILED = PrepaidCoverageRepairOutcome(
    subscriptions=0,
    repairable=0,
    quarantined=0,
    quarantined_blocking=0,
    entitlements_created=0,
    stale_preview_chunks=0,
    deferred_subscriptions=0,
    status=PrepaidCoverageRepairStatus.error,
)


def repair_prepaid_coverage_evidence(
    session: Session,
    *,
    now: datetime | None = None,
    deadline: datetime | None = None,
) -> PrepaidCoverageRepairOutcome:
    """Drain exact-evidence prepaid coverage gaps without operator interaction.

    Enforcement fails closed on any subscription whose paid financial evidence
    has no entitlement projection, which parks the prepaid sweep for the whole
    account. This runner previews the full collectible cohort and confirms the
    fingerprint-bound repair for every item backed by exact evidence, so the
    blocked set converges to zero between sweeps. Quarantined evidence
    (ambiguous, conflicting, or malformed sources) still requires a human
    decision: it is never bypassed, and enforcement-blocking quarantine is
    persisted through the owner's run/item evidence, not just logged. All
    decisions stay with ``financial.prepaid_service_coverage_reconciliation``;
    this runner only sequences preview → confirm and bounds transaction size.

    TRANSITIONAL (ADR 0007): this repair exists because historical forward
    billing could commit paid evidence without its entitlement projection.
    Retire it, together with ``prepaid_balance_sweep``, at the Phase 5
    collections cutover — once activation/renewal cannot commit without the
    contract, obligation, and timer, forward transactions can no longer create
    the gaps this pass repairs.
    """
    from app.services.owner_commands import CommandContext
    from app.services.prepaid_coverage_reconciliation import (
        CoverageReconciliationDecision,
        CoverageReconciliationReason,
        PrepaidCoverageReconciliationError,
        ReconcilePrepaidCoverageCommand,
        preview_prepaid_coverage_reconciliation,
        reconcile_prepaid_service_coverage,
    )

    # Quarantine reasons that also block adverse enforcement (the same
    # evidence classes resolve_prepaid_coverage_enforcement_blockers reports).
    blocking_reasons: frozenset[CoverageReconciliationReason] = frozenset(
        {
            CoverageReconciliationReason.malformed_paid_invoice_period,
            CoverageReconciliationReason.malformed_renewal_origin,
            CoverageReconciliationReason.conflicting_financial_sources,
            CoverageReconciliationReason.ambiguous_paid_invoice_lines,
            CoverageReconciliationReason.ambiguous_renewal_adjustments,
        }
    )

    observed_at = now or datetime.now(UTC)
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=UTC)
    cohort = preview_prepaid_coverage_reconciliation(session, as_of=observed_at)
    # The owner-command boundary requires a transaction-free session; the
    # preview above is read-only, so completing its snapshot loses nothing.
    session.commit()

    repairable_ids: list[UUID] = []
    blocking_quarantined_ids: list[UUID] = []
    quarantine_reasons_by_account: dict[UUID, set[str]] = {}
    for item in cohort.items:
        if item.decision == CoverageReconciliationDecision.entitlement_created:
            repairable_ids.append(item.subscription_id)
        elif (
            item.decision == CoverageReconciliationDecision.quarantined
            and item.reason in blocking_reasons
        ):
            blocking_quarantined_ids.append(item.subscription_id)
            quarantine_reasons_by_account.setdefault(item.account_id, set()).add(
                item.reason.value
            )
    if blocking_quarantined_ids:
        reasons = sorted(
            {
                reason
                for values in quarantine_reasons_by_account.values()
                for reason in values
            }
        )
        logger.error(
            "prepaid_coverage_quarantined_evidence_requires_review: "
            "count=%d reasons=%s",
            len(blocking_quarantined_ids),
            ",".join(reasons),
        )
    # Every blocking-quarantined account carries a live, owned, SLA-bound
    # work item; items close automatically once the evidence is corrected
    # and the account leaves quarantine.
    _sync_quarantine_work_items(
        session,
        quarantine_reasons_by_account,
        now=observed_at,
    )
    session.commit()

    created = 0
    stale_chunks = 0
    deferred_subscriptions = 0
    # Blocking quarantine rides along so the owner records immutable run/item
    # evidence for it; identical evidence replays the same run, so repeated
    # sweeps do not multiply records.
    worklist: list[UUID] = sorted((*repairable_ids, *blocking_quarantined_ids), key=str)
    for start in range(0, len(worklist), _COVERAGE_REPAIR_CHUNK):
        if deadline is not None and datetime.now(UTC) >= deadline:
            deferred_subscriptions = len(worklist) - start
            logger.warning(
                "prepaid_coverage_repair_budget_exhausted: deferred=%d "
                "of %d subscriptions to the next run",
                deferred_subscriptions,
                len(worklist),
            )
            break
        chunk = tuple(worklist[start : start + _COVERAGE_REPAIR_CHUNK])
        preview = preview_prepaid_coverage_reconciliation(
            session,
            as_of=observed_at,
            subscription_ids=chunk,
        )
        session.commit()
        if preview.blocker_count == 0:
            continue
        command = ReconcilePrepaidCoverageCommand(
            context=CommandContext.system(
                actor="task:prepaid-coverage-repair",
                scope="financial.prepaid_service_coverage_reconciliation:scheduled",
                reason=(
                    "scheduled repair of exact prepaid coverage evidence "
                    "blocking enforcement"
                ),
                idempotency_key=f"scheduled-coverage-repair:{preview.fingerprint}",
            ),
            as_of=preview.as_of,
            preview_fingerprint=preview.fingerprint,
            subscription_ids=chunk,
        )
        try:
            result = reconcile_prepaid_service_coverage(session, command)
        except PrepaidCoverageReconciliationError as exc:
            if exc.code.endswith("stale_preview"):
                # Evidence moved between preview and confirmation; the next
                # sweep run re-previews and repairs the current state.
                logger.warning(
                    "prepaid_coverage_repair_stale_preview: "
                    "chunk deferred to the next run"
                )
                stale_chunks += 1
                continue
            raise
        created += result.entitlement_created_count
        logger.info(
            "prepaid_coverage_repair_chunk_completed: run_id=%s created=%d "
            "already_covered=%d no_repair=%d quarantined=%d replayed=%s",
            result.run_id,
            result.entitlement_created_count,
            result.already_covered_count,
            result.no_repair_required_count,
            result.quarantined_count,
            result.replayed,
        )

    return PrepaidCoverageRepairOutcome(
        subscriptions=len(cohort.subscription_ids),
        repairable=cohort.repairable_count,
        quarantined=cohort.quarantined_count,
        quarantined_blocking=len(blocking_quarantined_ids),
        entitlements_created=created,
        stale_preview_chunks=stale_chunks,
        deferred_subscriptions=deferred_subscriptions,
        status=(
            PrepaidCoverageRepairStatus.stale_preview
            if stale_chunks
            else PrepaidCoverageRepairStatus.ok
        ),
    )


def _sync_quarantine_work_items(
    session: Session,
    reasons_by_account: dict[UUID, set[str]],
    *,
    now: datetime,
) -> None:
    """Mirror blocking-quarantine facts into owned admin work items."""
    from datetime import timedelta

    from app.models.network_monitoring import AlertSeverity
    from app.services.observability import Finding, record_finding, resolve_findings

    for account_id in sorted(reasons_by_account, key=str):
        record_finding(
            session,
            Finding(
                fingerprint=f"{_QUARANTINE_FINDING_PREFIX}{account_id}",
                domain="prepaid_enforcement",
                source="prepaid_coverage_repair",
                severity=AlertSeverity.warning,
                title="Prepaid coverage evidence needs finance review",
                summary=(
                    "Contradictory or malformed financial evidence blocks "
                    "adverse enforcement for this account. Review the "
                    "reconciliation run evidence and correct the source "
                    "records; the account must never be auto-suspended from "
                    "ambiguous debt."
                ),
                details={
                    "owner": "finance-billing",
                    "account_id": str(account_id),
                    "reason_codes": sorted(reasons_by_account[account_id]),
                    "sla_due_at": (
                        now + timedelta(hours=_QUARANTINE_SLA_HOURS)
                    ).isoformat(),
                },
            ),
        )
    resolve_findings(
        session,
        managed_prefix=_QUARANTINE_FINDING_PREFIX,
        active_fingerprints={
            f"{_QUARANTINE_FINDING_PREFIX}{account_id}"
            for account_id in reasons_by_account
        },
    )


def _publish_prepaid_enforcement_snapshot(
    repair: PrepaidCoverageRepairOutcome,
    sweep: dict[str, int | str],
) -> None:
    """Export bounded repair + enforcement counts for /metrics and alerting."""
    from app.services.observability import StateObservation, publish_state_snapshot

    def _count(key: str) -> float:
        value = sweep.get(key, 0)
        return float(value) if isinstance(value, (int, float)) else 0.0

    signals: dict[str, float] = {
        "coverage_repairable": float(repair.repairable),
        "coverage_quarantined_blocking": float(repair.quarantined_blocking),
        "coverage_quarantined_total": float(repair.quarantined),
        "coverage_entitlements_created": float(repair.entitlements_created),
        "coverage_stale_preview_chunks": float(repair.stale_preview_chunks),
        "coverage_repair_deferred": float(repair.deferred_subscriptions),
        "coverage_repair_failed": float(
            repair.status is PrepaidCoverageRepairStatus.error
        ),
        "coverage_unresolved": _count("coverage_unresolved"),
        "renewal_terms_unresolved": _count("renewal_terms_unresolved"),
        "funding_quarantined": _count("funding_quarantined"),
        "notice_suppressed": _count("notice_suppressed"),
        "no_contact_route": _count("no_contact_route"),
        "delivery_unavailable": _count("delivery_unavailable"),
        "budget_deferred": _count("budget_deferred"),
        "sweep_errors": _count("errors"),
        "suspended": _count("suspended"),
        "warned": _count("warned"),
        "restored": _count("restored"),
    }
    if repair.status is PrepaidCoverageRepairStatus.error or signals["sweep_errors"]:
        status = "error"
    elif any(
        signals[name]
        for name in (
            "coverage_quarantined_blocking",
            "coverage_unresolved",
            "renewal_terms_unresolved",
            "funding_quarantined",
            "no_contact_route",
            "delivery_unavailable",
            "coverage_repair_deferred",
            "budget_deferred",
        )
    ):
        status = "degraded"
    else:
        status = "ok"
    publish_state_snapshot(
        "prepaid_enforcement",
        (
            StateObservation(signal=name, scope="collections", value=value)
            for name, value in signals.items()
        ),
        status=status,
    )


def _sweep_budget_seconds(session: Session) -> int:
    from app.models.domain_settings import SettingDomain
    from app.services.settings_spec import resolve_value

    try:
        value = int(
            resolve_value(
                session,
                SettingDomain.collections,
                "prepaid_balance_sweep_budget_seconds",
            )
            or _DEFAULT_SWEEP_BUDGET_SECONDS
        )
    except (TypeError, ValueError):
        return _DEFAULT_SWEEP_BUDGET_SECONDS
    return value if value > 0 else _DEFAULT_SWEEP_BUDGET_SECONDS


def run_prepaid_balance_sweep() -> dict[str, int | str]:
    from app.services.collections.prepaid_balance_sweep import (
        run_prepaid_balance_sweep as run_sweep,
    )

    logger.info("Starting prepaid balance sweep")
    session = SessionLocal()
    try:
        # The run must always end by publishing its snapshot: work that does
        # not fit the budget is deferred to the next run instead of letting
        # the Celery soft time limit interrupt a query mid-flight and kill
        # the task unreported.
        deadline = datetime.now(UTC) + timedelta(seconds=_sweep_budget_seconds(session))
        # Repair exact-evidence coverage gaps first so this same run evaluates
        # the repaired coverage. A repair failure must never stop enforcement:
        # the sweep still runs and its per-account fail-close still holds.
        try:
            repair = repair_prepaid_coverage_evidence(session, deadline=deadline)
        except Exception:
            session.rollback()
            logger.exception("prepaid_coverage_repair_failed")
            repair = _REPAIR_FAILED
        result = run_sweep(session, deadline=deadline)
        try:
            _publish_prepaid_enforcement_snapshot(repair, result)
        except Exception:
            logger.exception("prepaid_enforcement_snapshot_failed")
        result.update(repair.as_stats())
        logger.info("prepaid_balance_sweep completed: %s", result)
        return result
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
