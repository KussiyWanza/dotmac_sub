"""Billing liveness / anomaly signals for monitoring (metrics + alerts).

These are MONITORING checks — surfaced as Prometheus gauges and operator
alerts. They are deliberately NOT fleet-wide enforcement gates. They answer
"is the billing system producing correct, complete output?", not "should this
specific customer be cut?". Canonical account facts, shields, and the financial
access owner decide that consequence.

Signals:
* paid-with-balance — invoices marked ``paid`` that still carry a non-zero
  ``balance_due`` (AR-integrity defect; dashboards/restore logic trust ``paid``).
* invoice-scan coverage — the latest billing run's ``subscriptions_scanned`` vs
  the count of active subscriptions; a sharp drop means the cycle silently
  stopped scanning a cohort (the 4,041->108 incident).
* payment-volume — last-24h succeeded payments vs the trailing-7-day daily
  average; a collapse means intake broke (the handoff recording gap), using a
  REAL baseline rather than a static floor.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import bindparam, func, select, text
from sqlalchemy.orm import Session

from app.models.billing import (
    BillingRun,
    Invoice,
    InvoiceStatus,
    Payment,
    PaymentStatus,
)
from app.models.catalog import (
    BillingMode,
    Subscription,
)
from app.models.domain_settings import SettingDomain
from app.models.enforcement_lock import EnforcementLock, EnforcementReason
from app.models.scheduler import ScheduledTask
from app.models.subscriber import Subscriber
from app.services import settings_spec
from app.services.access_resolution import (
    postpaid_billing_filters,
    prepaid_enforcement_filters,
)
from app.services.billing_settings import COLLECTIBLE_SERVICE_STATUSES
from app.services.billing_statuses import (
    BILLABLE_SUBSCRIBER_STATUS_VALUES,
    BILLABLE_SUBSCRIBER_STATUSES,
)
from app.services.customer_financial_position import prepaid_available_balances
from app.services.job_heartbeat import (
    PAYMENT_RECONCILIATION_TASK,
    get_last_result,
    get_last_success,
)
from app.services.prepaid_enforcement_planner import (
    candidate_prepaid_funding_account_ids,
)
from app.services.prepaid_funding_reconstruction import (
    PrepaidFundingBaselineMissingError,
    prepaid_funding_incomplete_source_account_ids,
)

# Alert thresholds. Conservative defaults; tune via ops experience.
SCAN_MIN_RATIO = 0.5  # alert if a run scanned < 50% of active subs
PAYMENT_VOLUME_MIN_RATIO = 0.4  # alert if last-24h volume < 40% of 7d daily avg
# Don't cry "collapse" on naturally low-traffic systems: require a real baseline.
PAYMENT_BASELINE_MIN_DAILY = 5.0

# A runner is "stale" if it has not succeeded within interval x this multiplier.
HEARTBEAT_STALE_MULTIPLIER = 3.0
# Critical billing/collections runners whose silence is a revenue/enforcement
# risk. Only the ENABLED ones are judged (a disabled runner is intentional).
_CRITICAL_RUNNERS = (
    "app.tasks.billing.run_invoice_cycle",
    "app.tasks.collections.run_billing_enforcement",
    "app.tasks.billing.mark_invoices_overdue",
    "app.tasks.billing.check_billing_switch",
    PAYMENT_RECONCILIATION_TASK,
)

logger = logging.getLogger(__name__)

BILLING_HEALTH_OBSERVABILITY_DOMAIN = "billing_health"
BILLING_HEALTH_SNAPSHOT_TASK = "app.tasks.billing.refresh_billing_health_snapshot"
# Stable Postgres advisory-lock key for the single-flight snapshot producer.
BILLING_HEALTH_SNAPSHOT_LOCK_KEY = 0x62686C74  # "bhlt"


@dataclass(frozen=True)
class RunnerHeartbeat:
    task_name: str
    enabled: bool
    interval_seconds: int | None
    last_success: datetime | None
    age_seconds: float | None
    stale: bool
    # Last-run result blob {status, at, detail} from job_heartbeat.get_last_result,
    # or None when unknown (never recorded / Redis down). Optional so older
    # positional call sites and tests stay valid.
    last_result: dict | None = None

    @property
    def last_result_status(self) -> str | None:
        """ "ok" / "error" / None (unknown)."""
        if not isinstance(self.last_result, dict):
            return None
        status = self.last_result.get("status")
        return status if isinstance(status, str) else None

    @property
    def last_result_at(self) -> datetime | None:
        """Timestamp of the last recorded result, or None. Never raises."""
        if not isinstance(self.last_result, dict):
            return None
        raw = self.last_result.get("at")
        if not isinstance(raw, str):
            return None
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed

    @property
    def last_result_detail(self) -> dict | None:
        """Returned counts (success) or {"error": msg} (failure), or None."""
        if not isinstance(self.last_result, dict):
            return None
        detail = self.last_result.get("detail")
        return detail if isinstance(detail, dict) else None

    @property
    def last_result_summary(self) -> str:
        """Compact one-line rendering of the last-run result for display."""
        status = self.last_result_status
        if status is None:
            return "No result yet"
        detail = self.last_result_detail or {}
        if status == "error":
            msg = detail.get("error")
            return f"errored: {msg}" if msg else "errored"
        if status == "partial":
            if not detail:
                return "partial"
            counts = ", ".join(f"{k}={v}" for k, v in detail.items())
            return f"partial — {counts}" if counts else "partial"
        if not detail:
            return "ok"
        counts = ", ".join(f"{k}={v}" for k, v in detail.items())
        return f"ok — {counts}" if counts else "ok"


@dataclass(frozen=True)
class BillingHealthSnapshot:
    paid_with_balance_count: int
    paid_with_balance_total: Decimal
    last_scanned: int | None
    eligible_active_subs: int
    scan_ratio: float | None
    payments_24h: int
    payments_7d_daily_avg: float
    payment_volume_ratio: float | None
    payment_volume_collapsed: bool
    # §6.3 runner heartbeats / §6.6 enforcement drift (defaults keep older
    # call sites and tests valid).
    runners: tuple[RunnerHeartbeat, ...] = ()
    covered_but_locked: int = 0
    # §6.1 billing-path coverage.
    unbilled_no_path: int = 0
    active_subs_on_terminal_account: int = 0
    # None means "could not be measured" (a prepaid funding baseline is
    # missing), which is deliberately distinct from a measured zero.
    negative_prepaid_balance_count: int | None = 0
    negative_prepaid_balance_total: Decimal | None = Decimal("0.00")
    # Accounts the sweep excludes from funding enforcement, and which the two
    # counts above therefore deliberately exclude too. A real measured number,
    # never None: it is the reason exposure is measured over a smaller cohort.
    negative_prepaid_funding_quarantined_count: int = 0
    billing_profile_mismatch_count: int = 0
    billing_profile_mixed_count: int = 0
    account_credit_invariant_count: int = 0
    # Drafts nobody has come back to. Reported, never acted on.
    aged_draft_count: int = 0
    aged_draft_total: Decimal = Decimal("0.00")
    # Invoices past draft with no issue date. They cannot age, cannot go
    # overdue, and drop out of anything filtering on issue date.
    paid_without_issue_count: int = 0
    # The opening balance carried in at the handoff. Observed so the boundary
    # stays visible, but never an anomaly: no reconciler can produce
    # allocations for invoices that were carried in already settled.
    account_credit_invariant_opening_balance_count: int = 0
    prepaid_coverage_unresolved_count: int = 0
    prepaid_coverage_repairable_count: int = 0
    prepaid_coverage_quarantined_count: int = 0
    scan_min_ratio: float = SCAN_MIN_RATIO
    payment_volume_min_ratio: float = PAYMENT_VOLUME_MIN_RATIO
    payment_baseline_min_daily: float = PAYMENT_BASELINE_MIN_DAILY

    @property
    def stale_runners(self) -> list[str]:
        return [r.task_name for r in self.runners if r.stale]

    @property
    def anomalies(self) -> list[str]:
        out: list[str] = []
        if self.paid_with_balance_count > 0:
            out.append("paid_invoices_with_balance")
        if self.aged_draft_count > 0:
            out.append("aged_draft_invoices")
        if self.paid_without_issue_count > 0:
            out.append("invoices_paid_without_issue")
        if self.scan_ratio is not None and self.scan_ratio < self.scan_min_ratio:
            out.append("invoice_scan_count_low")
        if self.payment_volume_collapsed:
            out.append("payment_volume_collapse")
        if self.stale_runners:
            out.append("runner_heartbeat_stale")
        if self.covered_but_locked > 0:
            out.append("enforcement_covered_but_locked")
        if self.unbilled_no_path > 0:
            out.append("active_subs_without_billing_path")
        if self.negative_prepaid_balance_count is None:
            out.append("negative_prepaid_exposure_unmeasurable")
        elif self.negative_prepaid_balance_count > 0:
            out.append("negative_prepaid_balances")
        if self.negative_prepaid_funding_quarantined_count > 0:
            out.append("prepaid_funding_quarantined")
        if self.billing_profile_mismatch_count > 0:
            out.append("billing_profile_mismatch")
        if self.billing_profile_mixed_count > 0:
            out.append("billing_profile_mixed_modes")
        if self.account_credit_invariant_count > 0:
            out.append("account_credit_invariant_violations")
        if self.prepaid_coverage_unresolved_count > 0:
            out.append("prepaid_coverage_unresolved")
        if self.prepaid_coverage_repairable_count > 0:
            out.append("prepaid_coverage_repair_required")
        if self.prepaid_coverage_quarantined_count > 0:
            out.append("prepaid_coverage_quarantined")
        return out


def billing_health_observations(snapshot: BillingHealthSnapshot):  # noqa: ANN201
    """Convert a health result into bounded, scrape-safe observations."""
    from app.services.observability import StateObservation

    values: list[tuple[str, str, int | float | Decimal]] = [
        ("paid_invoices_with_balance", "all", snapshot.paid_with_balance_count),
        ("aged_draft_invoices", "all", snapshot.aged_draft_count),
        ("aged_draft_invoice_total", "all", snapshot.aged_draft_total),
        ("invoices_paid_without_issue", "all", snapshot.paid_without_issue_count),
        ("invoice_last_scanned", "all", snapshot.last_scanned or 0),
        ("active_subscriptions", "all", snapshot.eligible_active_subs),
        ("payments_succeeded_24h", "all", snapshot.payments_24h),
        (
            "payments_succeeded_7d_daily_avg",
            "all",
            snapshot.payments_7d_daily_avg,
        ),
        (
            "payment_volume_collapsed",
            "all",
            1.0 if snapshot.payment_volume_collapsed else 0.0,
        ),
        (
            "enforcement_covered_but_locked",
            "all",
            snapshot.covered_but_locked,
        ),
        (
            "prepaid_coverage_unresolved",
            "all",
            snapshot.prepaid_coverage_unresolved_count,
        ),
        (
            "prepaid_coverage_repair_required",
            "all",
            snapshot.prepaid_coverage_repairable_count,
        ),
        (
            "prepaid_coverage_quarantined",
            "all",
            snapshot.prepaid_coverage_quarantined_count,
        ),
        (
            "prepaid_funding_quarantined",
            "all",
            snapshot.negative_prepaid_funding_quarantined_count,
        ),
        (
            "billing_profile_mismatch_accounts",
            "all",
            snapshot.billing_profile_mismatch_count,
        ),
        (
            "billing_profile_mixed_accounts",
            "all",
            snapshot.billing_profile_mixed_count,
        ),
        (
            "account_credit_invariant_violations",
            "all",
            snapshot.account_credit_invariant_count,
        ),
        (
            "account_credit_invariant_violations",
            "opening_balance",
            snapshot.account_credit_invariant_opening_balance_count,
        ),
        (
            "unbilled_active_subscriptions",
            "no_billing_path",
            snapshot.unbilled_no_path,
        ),
        (
            "unbilled_active_subscriptions",
            "terminal_account",
            snapshot.active_subs_on_terminal_account,
        ),
    ]
    # Omitted rather than zeroed when the funding baseline is missing: a false
    # "0 accounts negative" would read as healthy.
    if snapshot.negative_prepaid_balance_count is not None:
        values.append(
            (
                "negative_prepaid_balance_accounts",
                "all",
                snapshot.negative_prepaid_balance_count,
            )
        )
    if snapshot.negative_prepaid_balance_total is not None:
        values.append(
            (
                "negative_prepaid_balance_total",
                "all",
                snapshot.negative_prepaid_balance_total,
            )
        )
    if snapshot.scan_ratio is not None:
        values.append(("invoice_scan_ratio", "all", snapshot.scan_ratio))
    if snapshot.payment_volume_ratio is not None:
        values.append(("payment_volume_ratio", "all", snapshot.payment_volume_ratio))
    for runner in snapshot.runners:
        if not runner.enabled:
            continue
        values.append(
            (
                "runner_heartbeat_stale",
                runner.task_name,
                1.0 if runner.stale else 0.0,
            )
        )
        if runner.age_seconds is not None:
            values.append(
                (
                    "runner_heartbeat_age_seconds",
                    runner.task_name,
                    max(runner.age_seconds, 0.0),
                )
            )
    return [
        StateObservation(signal=signal, scope=scope, value=float(value))
        for signal, scope, value in values
    ]


def publish_billing_health_snapshot(
    snapshot: BillingHealthSnapshot, *, now: datetime | None = None
) -> bool:
    """Publish the latest bounded health snapshot for metrics collectors."""
    from app.services.observability import publish_state_snapshot

    return publish_state_snapshot(
        BILLING_HEALTH_OBSERVABILITY_DOMAIN,
        billing_health_observations(snapshot),
        status="degraded" if snapshot.anomalies else "ok",
        now=now,
    )


AGED_DRAFT_DAYS = 30


def aged_draft_invoices(
    db: Session, now: datetime | None = None
) -> tuple[int, Decimal]:
    """Count + sum of drafts old enough that nobody is coming back for them.

    A draft is a legitimate holding state: awaiting confirmation, provisioning,
    or a quote nobody has accepted. So this reports and never acts — auto
    issuing would send bills nobody meant to send, and the data cannot tell a
    deliberate hold from an abandoned one.

    What it prevents is the third case: a draft that is simply forgotten. One
    hand-made invoice for over a million naira sat unissued for days because
    nothing counted drafts, so nothing could notice. An aged draft has three
    honest dispositions — issue it, void it, or keep holding it deliberately —
    and all three are fine. Not knowing it exists is not.
    """
    cutoff = (now or datetime.now(UTC)) - timedelta(days=AGED_DRAFT_DAYS)
    count_, total = db.execute(
        select(
            func.count(Invoice.id),
            func.coalesce(func.sum(Invoice.total), 0),
        )
        .where(Invoice.is_active.is_(True))
        .where(Invoice.status == InvoiceStatus.draft)
        .where(Invoice.total > 0)
        .where(Invoice.created_at < cutoff)
    ).one()
    return int(count_ or 0), Decimal(str(total or 0))


def paid_with_balance(db: Session) -> tuple[int, Decimal]:
    """Count + sum of invoices that are ``paid`` yet retain a balance_due."""
    count_, total = db.execute(
        select(
            func.count(Invoice.id),
            func.coalesce(func.sum(Invoice.balance_due), 0),
        )
        .where(Invoice.status == InvoiceStatus.paid)
        .where(Invoice.balance_due != 0)
    ).one()
    return int(count_ or 0), Decimal(str(total or 0))


def _resolve_float_setting(
    db: Session,
    key: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    value = settings_spec.resolve_value(db, SettingDomain.billing, key)
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _health_thresholds(db: Session) -> tuple[float, float, float]:
    scan_min_ratio = _resolve_float_setting(
        db,
        "billing_health_scan_min_ratio",
        SCAN_MIN_RATIO,
        minimum=0.0,
        maximum=1.0,
    )
    payment_volume_min_ratio = _resolve_float_setting(
        db,
        "billing_health_payment_volume_min_ratio",
        PAYMENT_VOLUME_MIN_RATIO,
        minimum=0.0,
        maximum=1.0,
    )
    payment_baseline_min_daily = _resolve_float_setting(
        db,
        "billing_health_payment_baseline_min_daily",
        PAYMENT_BASELINE_MIN_DAILY,
        minimum=0.0,
        maximum=100000.0,
    )
    return scan_min_ratio, payment_volume_min_ratio, payment_baseline_min_daily


def invoice_scan_coverage(db: Session) -> tuple[int | None, int, float | None]:
    """(last_run_scanned, eligible_active_subs, ratio)."""
    last_scanned = db.execute(
        select(BillingRun.subscriptions_scanned)
        .order_by(BillingRun.created_at.desc())
        .limit(1)
    ).scalar()
    # Mirror run_invoice_cycle's eligibility: ACTIVE subscriptions whose account
    # is in a billable state and that are not prepaid. Counting only
    # SubscriptionStatus.active (with no subscriber/billing_mode filter) both
    # missed blocked/suspended/delinquent accounts that ARE billed and counted
    # prepaid subs that are NOT — skewing the coverage ratio.
    eligible = (
        db.execute(
            select(func.count(Subscription.id))
            .join(Subscriber, Subscriber.id == Subscription.subscriber_id)
            .where(*postpaid_billing_filters(Subscription, Subscriber))
        ).scalar()
        or 0
    )
    ratio = (
        float(last_scanned) / float(eligible)
        if last_scanned is not None and eligible > 0
        else None
    )
    return (
        int(last_scanned) if last_scanned is not None else None,
        int(eligible),
        ratio,
    )


def payment_volume(
    db: Session, now: datetime | None = None
) -> tuple[int, float, float | None, bool]:
    """(count_24h, daily_avg_prev_7d, ratio, collapsed) for succeeded payments."""
    now = now or datetime.now(UTC)
    _, payment_volume_min_ratio, payment_baseline_min_daily = _health_thresholds(db)
    last_24h = now - timedelta(hours=24)
    baseline_start = now - timedelta(days=8)  # 7-day window ending 24h ago

    count_24h = (
        db.execute(
            select(func.count(Payment.id))
            .where(Payment.status == PaymentStatus.succeeded)
            .where(Payment.paid_at.is_not(None))
            .where(Payment.paid_at >= last_24h)
        ).scalar()
        or 0
    )
    count_prev_7d = (
        db.execute(
            select(func.count(Payment.id))
            .where(Payment.status == PaymentStatus.succeeded)
            .where(Payment.paid_at.is_not(None))
            .where(Payment.paid_at >= baseline_start)
            .where(Payment.paid_at < last_24h)
        ).scalar()
        or 0
    )
    daily_avg = float(count_prev_7d) / 7.0
    ratio = float(count_24h) / daily_avg if daily_avg > 0 else None
    collapsed = (
        daily_avg >= payment_baseline_min_daily
        and ratio is not None
        and ratio < payment_volume_min_ratio
    )
    return int(count_24h), daily_avg, ratio, collapsed


def runner_heartbeats(
    db: Session, now: datetime | None = None
) -> list[RunnerHeartbeat]:
    """Freshness of critical runners' last SUCCESS (Redis heartbeat).

    Only ENABLED runners are judged stale (a disabled runner is intentional).
    A runner that has never succeeded while enabled is stale (covers a freshly
    armed-but-dead consumer); it self-clears after the first success.
    """
    now = now or datetime.now(UTC)
    out: list[RunnerHeartbeat] = []
    for task_name in _CRITICAL_RUNNERS:
        row = db.execute(
            select(ScheduledTask.enabled, ScheduledTask.interval_seconds)
            .where(ScheduledTask.task_name == task_name)
            .limit(1)
        ).first()
        enabled = bool(row[0]) if row else False
        interval = int(row[1]) if row and row[1] else None
        last_result = get_last_result(task_name)
        if not enabled:
            out.append(
                RunnerHeartbeat(
                    task_name, False, interval, None, None, False, last_result
                )
            )
            continue
        last = get_last_success(task_name)
        if last is not None and last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        age = (now - last).total_seconds() if last else None
        if interval:
            stale = last is None or (
                age is not None and age > interval * HEARTBEAT_STALE_MULTIPLIER
            )
        else:
            stale = last is None
        out.append(
            RunnerHeartbeat(task_name, True, interval, last, age, stale, last_result)
        )
    return out


def _default_currency(db: Session) -> str:
    """Billing default currency setting (NGN when unset)."""
    value = settings_spec.resolve_value(db, SettingDomain.billing, "default_currency")
    code = str(value or "NGN").strip().upper()
    return code or "NGN"


def covered_but_locked(db: Session) -> int:
    """Accounts with an active prepaid lock on exactly covered service."""
    from app.services.prepaid_service_coverage import (
        PrepaidCoverageStatus,
        resolve_prepaid_service_coverage,
    )

    subscriptions = list(
        db.scalars(
            select(Subscription)
            .join(
                EnforcementLock,
                EnforcementLock.subscription_id == Subscription.id,
            )
            .where(
                EnforcementLock.is_active.is_(True),
                EnforcementLock.reason == EnforcementReason.prepaid,
            )
            .distinct()
        ).all()
    )
    decisions = resolve_prepaid_service_coverage(db, subscriptions)
    return len(
        {
            subscription.subscriber_id
            for subscription in subscriptions
            if decisions[subscription.id].status == PrepaidCoverageStatus.covered
        }
    )


def prepaid_coverage_unresolved(db: Session) -> int:
    """Count collectible services with a future anchor but no coverage evidence."""
    from app.services.prepaid_service_coverage import (
        PrepaidCoverageStatus,
        resolve_prepaid_service_coverage,
    )

    subscriptions = list(
        db.scalars(
            select(Subscription).where(
                Subscription.billing_mode == BillingMode.prepaid,
                Subscription.status.in_(COLLECTIBLE_SERVICE_STATUSES),
            )
        ).all()
    )
    decisions = resolve_prepaid_service_coverage(db, subscriptions)
    return sum(
        1
        for decision in decisions.values()
        if decision.status == PrepaidCoverageStatus.unresolved_projection
    )


def prepaid_coverage_reconciliation_counts(db: Session) -> tuple[int, int]:
    """Count exact backfills and evidence gaps blocking adverse enforcement."""
    from app.services.prepaid_coverage_reconciliation import (
        preview_prepaid_coverage_reconciliation,
    )

    preview = preview_prepaid_coverage_reconciliation(db)
    return preview.repairable_count, preview.quarantined_count


def billing_path_coverage(db: Session) -> tuple[int, int]:
    """§6.1: (unbilled_no_path, active_subs_on_terminal_account).

    Mirrors run_invoice_cycle's invoice-row selection. A billable-account active
    Every active prepaid subscription has the canonical renewal owner; postpaid
    subscriptions use invoice generation. ``active_subs_on_terminal_account``
    is an active sub whose account is non-billable, so the cycle never touches
    it (lifecycle drift, low volume).
    """
    # Static SQL — the status set is a fixed constant from billing_statuses,
    # never user input, so this is not an injection surface.
    terminal = (
        db.execute(
            text(
                """
            SELECT count(*) FROM subscriptions sub
            JOIN subscribers s ON s.id = sub.subscriber_id
            WHERE sub.status = 'active'
              AND s.status NOT IN :billable_statuses
            """
            ).bindparams(bindparam("billable_statuses", expanding=True)),
            {"billable_statuses": BILLABLE_SUBSCRIBER_STATUS_VALUES},
        ).scalar()
        or 0
    )

    return 0, int(terminal)


def negative_prepaid_balance_exposure(
    db: Session,
) -> tuple[int | None, Decimal | None, int]:
    """Negative prepaid wallet exposure over the cohort enforcement acts on.

    Returns ``(negative count, absolute total, incomplete-source count)``.

    This is a monitoring signal only; the permanent prepaid sweep owns warning,
    suspension, and restoration consequences. It must therefore measure the
    same cohort the sweep acts on: the sweep excludes incomplete-source
    accounts before resolving any balance, so measuring them here would report
    exposure for accounts that can never be enforced -- and would hit the
    fail-closed resolver on accounts the sweep deliberately never asks about.
    Incomplete-source accounts are reported as their own count instead of being
    silently folded into (or silently dropped from) the exposure figure.
    """
    account_ids = set(
        db.execute(
            select(Subscriber.id)
            .join(Subscription, Subscription.subscriber_id == Subscriber.id)
            .where(*prepaid_enforcement_filters(Subscription, Subscriber))
            .distinct()
        )
        .scalars()
        .all()
    )
    # Same two owning helpers, same order, as collections.prepaid_balance_sweep:
    # one cohort definition, not two.
    incomplete_source_ids = prepaid_funding_incomplete_source_account_ids(
        db, account_ids & candidate_prepaid_funding_account_ids(db)
    )
    measurable_ids = account_ids - incomplete_source_ids
    count = 0
    total = Decimal("0.00")
    try:
        balances = prepaid_available_balances(db, measurable_ids)
    except PrepaidFundingBaselineMissingError:
        # The balance resolver is fail-closed on purpose: enforcement must never
        # act on a balance it cannot verify. But this function is monitoring
        # only, and it shares that resolver -- so a single un-baselined account
        # used to abort the whole billing-health snapshot, publishing nothing
        # and taking EVERY billing gauge off the dashboard, including the ones
        # that would reveal an invoicing or payment-intake failure.
        #
        # Degrade this one signal instead. Returning None marks it unavailable
        # so the gauge is omitted rather than reported as a false zero, and the
        # rest of the snapshot still publishes.
        # Retained as a backstop. Source filtering above removes the known
        # un-baselined cohort, so reaching here means an account outside that
        # cohort cannot be resolved -- unexpected, and worth an anomaly, but
        # still never a reason to take the whole snapshot down.
        logger.exception(
            "billing_negative_prepaid_exposure_unavailable: prepaid funding "
            "baseline missing outside the source-incomplete cohort; reporting this "
            "signal as unavailable and publishing the rest of the snapshot"
        )
        return None, None, len(incomplete_source_ids)
    for account_id in measurable_ids:
        balance = balances[account_id]
        if balance < Decimal("0.00"):
            count += 1
            total += abs(balance)
    return count, total, len(incomplete_source_ids)


def billing_profile_integrity(db: Session) -> tuple[int, int]:
    """Return (account/subscription mismatch count, mixed subscription count)."""
    rows = db.execute(
        select(
            Subscriber.id,
            Subscriber.billing_mode,
            Subscription.billing_mode,
        )
        .join(Subscription, Subscription.subscriber_id == Subscriber.id)
        .where(Subscription.status.in_(COLLECTIBLE_SERVICE_STATUSES))
        .where(Subscriber.status.in_(BILLABLE_SUBSCRIBER_STATUSES))
    ).all()
    account_modes: dict[object, BillingMode | None] = {}
    subscription_modes: dict[object, set[BillingMode]] = {}
    for account_id, account_mode, subscription_mode in rows:
        account_modes[account_id] = account_mode
        if subscription_mode is not None:
            subscription_modes.setdefault(account_id, set()).add(subscription_mode)

    mismatch = 0
    mixed = 0
    for account_id, modes in subscription_modes.items():
        if len(modes) > 1:
            mixed += 1
            continue
        only_mode = next(iter(modes))
        if (
            account_modes.get(account_id) is not None
            and account_modes[account_id] != only_mode
        ):
            mismatch += 1
    return mismatch, mixed


def invoices_past_draft_without_issue_date(db: Session) -> int:
    """Count invoices that left draft without an issue date.

    An invoice past draft is asserting it was issued, so it must carry the date
    that makes that true: ageing, dunning runway, statements and every
    issue-date filter read it. A NULL there is not cosmetic — the invoice simply
    stops existing for those questions, quietly, without failing anything.

    Voided invoices are excluded: void is terminal and may be reached from
    states that never issued.
    """
    return int(
        db.query(func.count(Invoice.id))
        .filter(
            Invoice.is_active.is_(True),
            Invoice.status.not_in([InvoiceStatus.draft, InvoiceStatus.void]),
            Invoice.issued_at.is_(None),
        )
        .scalar()
        or 0
    )


def billing_health_snapshot(
    db: Session, now: datetime | None = None
) -> BillingHealthSnapshot:
    scan_min_ratio, payment_volume_min_ratio, payment_baseline_min_daily = (
        _health_thresholds(db)
    )
    pwb_count, pwb_total = paid_with_balance(db)
    aged_draft_count, aged_draft_total = aged_draft_invoices(db, now=now)
    paid_without_issue_count = invoices_past_draft_without_issue_date(db)
    last_scanned, eligible, scan_ratio = invoice_scan_coverage(db)
    c24, avg7, ratio, collapsed = payment_volume(db, now=now)
    no_path, terminal = billing_path_coverage(db)
    (
        negative_prepaid_count,
        negative_prepaid_total,
        negative_prepaid_quarantined,
    ) = negative_prepaid_balance_exposure(db)
    profile_mismatch_count, profile_mixed_count = billing_profile_integrity(db)
    from app.services.billing.account_credit import AccountCreditApplications

    account_credit_invariant_count = AccountCreditApplications.summarize_invariants(
        db
    ).total
    account_credit_invariant_opening_balance_count = (
        AccountCreditApplications.count_opening_balance_underfunded_invoices(db)
    )
    coverage_repairable_count, coverage_quarantined_count = (
        prepaid_coverage_reconciliation_counts(db)
    )
    return BillingHealthSnapshot(
        paid_with_balance_count=pwb_count,
        aged_draft_count=aged_draft_count,
        aged_draft_total=aged_draft_total,
        paid_without_issue_count=paid_without_issue_count,
        paid_with_balance_total=pwb_total,
        last_scanned=last_scanned,
        eligible_active_subs=eligible,
        scan_ratio=scan_ratio,
        payments_24h=c24,
        payments_7d_daily_avg=avg7,
        payment_volume_ratio=ratio,
        payment_volume_collapsed=collapsed,
        runners=tuple(runner_heartbeats(db, now=now)),
        covered_but_locked=covered_but_locked(db),
        prepaid_coverage_unresolved_count=prepaid_coverage_unresolved(db),
        prepaid_coverage_repairable_count=coverage_repairable_count,
        prepaid_coverage_quarantined_count=coverage_quarantined_count,
        unbilled_no_path=no_path,
        active_subs_on_terminal_account=terminal,
        negative_prepaid_balance_count=negative_prepaid_count,
        negative_prepaid_balance_total=negative_prepaid_total,
        negative_prepaid_funding_quarantined_count=negative_prepaid_quarantined,
        billing_profile_mismatch_count=profile_mismatch_count,
        billing_profile_mixed_count=profile_mixed_count,
        account_credit_invariant_count=account_credit_invariant_count,
        account_credit_invariant_opening_balance_count=(
            account_credit_invariant_opening_balance_count
        ),
        scan_min_ratio=scan_min_ratio,
        payment_volume_min_ratio=payment_volume_min_ratio,
        payment_baseline_min_daily=payment_baseline_min_daily,
    )
