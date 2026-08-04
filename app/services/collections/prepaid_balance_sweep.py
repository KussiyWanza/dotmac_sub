"""Prepaid balance/expiry enforcement sweep (Item 2 of the prepaid alignment).

Deposit-is-truth prepaid customers are invoiced in advance; the read-side
(``service_status``) already *projects* low-balance / grace / deactivation
dates. This periodic sweep executes the resolved enforcement plan through the
timer-state and subscription-lifecycle owners, then requests restoration once
the account is funded again.
A resolved zero-day policy suspends on the first eligible sweep; a nonzero
configured policy arms the timer and warning first.

SAFETY: this can suspend customers. The owner always evaluates eligible accounts;
safety is account-scoped: canonical funding, coverage, quarantine, profile
validity, shields, grace, and transaction-local locking. Every account is
processed in its own committed unit so one bad row cannot abort the batch.
"""

from __future__ import annotations

import bisect
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import Subscription
from app.models.collections import FinancialAccessOrigin, PrepaidSweepCycleState
from app.models.enforcement_lock import EnforcementReason
from app.models.subscriber import Subscriber
from app.services.access_resolution import PrepaidFundingDecision
from app.services.collections._core import (
    _clear_prepaid_dunning_flags,
    _restore_prepaid_if_funded,
    _suspend_account,
    confirm_financial_access_restoration,
    preview_financial_access_restoration,
)
from app.services.common import coerce_uuid
from app.services.prepaid_enforcement_planner import (
    PrepaidEnforcementAction,
    PrepaidEnforcementPolicy,
    candidate_prepaid_account_ids,
    candidate_prepaid_funding_account_ids,
    plan_prepaid_account,
    prepaid_notice_suppression_reasons,
    resolve_prepaid_enforcement_policy,
)
from app.services.prepaid_enforcement_state import (
    arm_prepaid_low_balance_timer,
    mark_prepaid_deactivated,
)

logger = logging.getLogger(__name__)

_SOURCE = "prepaid_balance_sweep"

_NO_CONTACT_FINDING_PREFIX = "prepaid-enforcement:no-contact-route:"
#: Review window recorded on the staff work item for an unreachable customer.
_NO_CONTACT_SLA_HOURS = 72


class PrepaidNoticeOutcome(StrEnum):
    """Typed result of attempting to queue an enforcement notice.

    Only ``policy_suppressed`` (a live customer-impact shield) may defer the
    collections progression. A missing contact route or an undeliverable
    channel never stops the timer: delivery stays independently retryable in
    the notification queue, and the unreachable customer becomes a staff work
    item instead of an indefinitely parked account.
    """

    queued = "queued"
    no_contact_route = "no_contact_route"
    delivery_unavailable = "delivery_unavailable"
    policy_suppressed = "policy_suppressed"


@dataclass(frozen=True, slots=True)
class PrepaidNoticeDecision:
    outcome: PrepaidNoticeOutcome
    queued_count: int
    suppressed: tuple[str, ...]


def _queue_notice(
    db: Session,
    account: Subscriber,
    subject: str,
    body: str,
    balance: Decimal,
    threshold: Decimal,
    *,
    suppression_reason: str | None = None,
) -> PrepaidNoticeDecision:
    """Queue the notice on every policy-resolved channel with a route.

    Channel selection, per-address policy gates, and row creation belong to
    the communication-intent owner; the queue runner owns delivery and
    retries. ``{balance}`` / ``{threshold}`` placeholders in the body are
    filled; malformed templates fall back to the raw text.
    """
    if suppression_reason:
        logger.info(
            "prepaid_balance_sweep notice suppressed for %s: %s",
            account.id,
            suppression_reason,
        )
        return PrepaidNoticeDecision(
            outcome=PrepaidNoticeOutcome.policy_suppressed,
            queued_count=0,
            suppressed=(suppression_reason,),
        )

    from app.models.notification import NotificationChannel
    from app.services.communication_intents import CommunicationIntent, submit

    try:
        rendered = str(body).format(balance=balance, threshold=threshold)
    except (KeyError, IndexError, ValueError):
        rendered = str(body)
    result = submit(
        db,
        CommunicationIntent(
            subscriber_id=account.id,
            event_type="prepaid_balance_enforcement",
            category="billing",
            subject=str(subject),
            body=rendered,
            # Push is deliberately absent: its address resolver returns the
            # subscriber id unconditionally, which would count every customer
            # as reachable and make no_contact_route unobservable. The
            # notification channel policy can still add push explicitly.
            default_channels=(
                NotificationChannel.email,
                NotificationChannel.sms,
                NotificationChannel.whatsapp,
            ),
        ),
    )
    if result.queued:
        return PrepaidNoticeDecision(
            outcome=PrepaidNoticeOutcome.queued,
            queued_count=len(result.queued),
            suppressed=tuple(result.suppressed),
        )
    subscriber_reasons = tuple(
        reason for reason in result.suppressed if reason.startswith("subscriber:")
    )
    no_route = not subscriber_reasons or all(
        reason.endswith(":missing_address") for reason in subscriber_reasons
    )
    return PrepaidNoticeDecision(
        outcome=(
            PrepaidNoticeOutcome.no_contact_route
            if no_route
            else PrepaidNoticeOutcome.delivery_unavailable
        ),
        queued_count=0,
        suppressed=tuple(result.suppressed),
    )


def _record_no_contact_route_finding(
    db: Session,
    account: Subscriber,
    decision: PrepaidNoticeDecision,
    *,
    now: datetime,
) -> None:
    """Create/refresh the staff work item for an unreachable customer."""
    from datetime import timedelta

    from app.models.network_monitoring import AlertSeverity
    from app.services.observability import Finding, record_finding

    record_finding(
        db,
        Finding(
            fingerprint=f"{_NO_CONTACT_FINDING_PREFIX}{account.id}",
            domain="prepaid_enforcement",
            source=_SOURCE,
            severity=AlertSeverity.warning,
            title="Prepaid customer has no deliverable warning channel",
            summary=(
                "The account entered low-balance enforcement with no "
                "deliverable customer channel. The collections timer is armed "
                "and enforcement continues; a staff contact route is required "
                "within the review SLA."
            ),
            details={
                "owner": "support-collections",
                "account_id": str(account.id),
                "sla_due_at": (
                    now + timedelta(hours=_NO_CONTACT_SLA_HOURS)
                ).isoformat(),
                "suppressed_routes": list(decision.suppressed),
            },
        ),
    )


def _reconcile_funded(
    db: Session, account: Subscriber, funding: PrepaidFundingDecision
) -> str:
    """Balance is at/above threshold: undo any prepaid enforcement. Idempotent."""
    had_timers = (
        account.prepaid_low_balance_at is not None
        or account.prepaid_deactivation_at is not None
    )
    restored = _restore_prepaid_if_funded(
        db,
        account,
        funding,
        resolved_by=f"{_SOURCE}:{account.id}",
    )
    if had_timers:
        _clear_prepaid_dunning_flags(db, str(account.id))
    if had_timers or restored:
        logger.info(
            "prepaid_balance_sweep recovered account %s (restored=%d)",
            account.id,
            restored,
        )
        return "restored"
    return "ok"


def _reconcile_stale_prepaid_locks(db: Session, account: Subscriber) -> str:
    """Resolve only locks proven obsolete by the canonical billing profile."""
    preview = preview_financial_access_restoration(
        db,
        str(account.id),
        origin=FinancialAccessOrigin.prepaid_enforcement,
    )
    result = confirm_financial_access_restoration(
        db,
        str(account.id),
        preview_fingerprint=preview.fingerprint,
        idempotency_key=(
            f"prepaid-stale-lock-repair:{account.id}:{preview.fingerprint[:24]}"
        ),
        origin=FinancialAccessOrigin.prepaid_enforcement,
        resolved_by=f"{_SOURCE}:stale_lock:{account.id}",
    )
    repaired = bool(
        result.consequence.result.get("enforcement_lock_ids")
        or preview.decision_inputs.get("clear_prepaid_timers")
    )
    return "restored" if repaired else "state_drift"


def _reconcile_low(
    db: Session,
    account: Subscriber,
    now: datetime,
    cfg: PrepaidEnforcementPolicy,
    balance: Decimal,
    threshold: Decimal,
    *,
    notice_suppression_reason: str | None = None,
    suspend_now: bool = False,
) -> str:
    """Apply the low-balance action already resolved by the policy owner."""
    result = "ok"
    just_armed = False
    if account.prepaid_low_balance_at is None:
        decision: PrepaidNoticeDecision | None = None
        if not suspend_now:
            decision = _queue_notice(
                db,
                account,
                cfg.warning_subject,
                cfg.warning_body,
                balance,
                threshold,
                suppression_reason=notice_suppression_reason,
            )
            # Only a live customer-impact shield defers the progression; the
            # grace clock must not start while the customer was deliberately
            # not warned during an outage they are inside.
            if decision.outcome is PrepaidNoticeOutcome.policy_suppressed:
                return "notice_suppressed"
        # A missing contact route or undeliverable channel never stops the
        # collections timer: delivery is the queue runner's retryable concern,
        # and the unreachable customer becomes a staff work item.
        just_armed = arm_prepaid_low_balance_timer(
            db,
            account.id,
            armed_at=now,
        )
        if suspend_now:
            result = "ok"
        elif decision is not None and decision.outcome is PrepaidNoticeOutcome.queued:
            result = "warned"
        elif (
            decision is not None
            and decision.outcome is PrepaidNoticeOutcome.no_contact_route
        ):
            _record_no_contact_route_finding(db, account, decision, now=now)
            result = "no_contact_route"
        else:
            result = "delivery_unavailable"
        logger.info(
            "prepaid_balance_sweep armed low-balance for account %s", account.id
        )

    # Non-zero configured grace arms and warns on the first observation. A
    # resolved zero-grace suspension deliberately continues in this locked unit.
    if account.prepaid_deactivation_at is not None or (just_armed and not suspend_now):
        return result

    # Arm the deactivation timer ONLY on a successful suspend. _suspend_account
    # fails-closed (returns False) for shielded / dedicated-bundle / canceled
    # accounts; if we armed the timer first, _reconcile_low would short-circuit
    # on the next run and never retry — leaving an armed-but-active account
    # showing a bogus deactivation date. Leaving the timer None makes the next
    # sweep re-attempt the suspension once the block clears.
    suspended = _suspend_account(
        db,
        str(account.id),
        reason=EnforcementReason.prepaid,
        source=_SOURCE,
    )
    if suspended:
        mark_prepaid_deactivated(db, account.id, deactivated_at=now)
        # Suspension already happened; the deactivation notice is best-effort
        # and its delivery stays with the queue runner.
        _queue_notice(
            db,
            account,
            cfg.deactivation_subject,
            cfg.deactivation_body,
            balance,
            threshold,
            suppression_reason=notice_suppression_reason,
        )
        logger.info("prepaid_balance_sweep suspended account %s", account.id)
        return "suspended"
    return result


def _process_account(
    db: Session,
    account: Subscriber,
    now: datetime,
    cfg: PrepaidEnforcementPolicy,
    *,
    notice_suppression_reason: str | None,
    prefetch: _SweepPrefetch | None = None,
) -> str:
    if prefetch is not None:
        decision = plan_prepaid_account(
            db,
            account,
            now=now,
            policy=cfg,
            notice_suppression_reason=notice_suppression_reason,
            subscriptions=prefetch.subscriptions_by_account.get(account.id, []),
            funding=prefetch.funding_by_account.get(account.id),
            active_prepaid_lock_count=prefetch.lock_counts.get(account.id, 0),
            dedicated_bundle=account.id in prefetch.dedicated_account_ids,
            shield_reason=prefetch.shield_reasons.get(account.id),
            shield_evaluated=True,
            window_block_reason=prefetch.window_block_reason,
            window_evaluated=True,
            include_derived_status=False,
        )
    else:
        decision = plan_prepaid_account(
            db,
            account,
            now=now,
            policy=cfg,
            notice_suppression_reason=notice_suppression_reason,
        )
    if decision.action == PrepaidEnforcementAction.billing_profile_invalid:
        logger.warning(
            "prepaid_balance_sweep skipped account %s: %s",
            account.id,
            decision.reason,
        )
        return "billing_profile_invalid"
    if decision.action == PrepaidEnforcementAction.clear_stale_timers:
        _clear_prepaid_dunning_flags(db, str(account.id))
        return "restored"
    if decision.action == PrepaidEnforcementAction.repair_stale_locks:
        return _reconcile_stale_prepaid_locks(db, account)
    if decision.action == PrepaidEnforcementAction.restore:
        return _reconcile_funded(
            db,
            account,
            PrepaidFundingDecision(
                account_id=str(account.id),
                available_balance=decision.available_balance,
                required_balance=decision.required_balance,
                currency=decision.currency,
                covered_subscription_ids=decision.covered_subscription_ids,
                non_billable_subscription_ids=(decision.non_billable_subscription_ids),
                actionable_uncovered_subscription_ids=(
                    decision.actionable_uncovered_subscription_ids
                ),
                unresolved_projection_subscription_ids=(
                    decision.unresolved_projection_subscription_ids
                ),
                unresolved_renewal_subscription_ids=(
                    decision.unresolved_renewal_subscription_ids
                ),
            ),
        )
    if decision.action == PrepaidEnforcementAction.coverage_unresolved:
        logger.error(
            "prepaid_balance_sweep blocked adverse action for account %s: %s (%s)",
            account.id,
            decision.reason,
            ",".join(
                str(value) for value in decision.unresolved_projection_subscription_ids
            ),
        )
        return "coverage_unresolved"
    if decision.action == PrepaidEnforcementAction.renewal_terms_unresolved:
        logger.error(
            "prepaid_balance_sweep blocked adverse action for account %s: %s (%s)",
            account.id,
            decision.reason,
            ",".join(
                str(value) for value in decision.unresolved_renewal_subscription_ids
            ),
        )
        return "renewal_terms_unresolved"
    if decision.action == PrepaidEnforcementAction.deferred:
        return "deferred"
    if decision.action == PrepaidEnforcementAction.shielded:
        return "shielded"
    if decision.action == PrepaidEnforcementAction.state_drift:
        logger.warning(
            "prepaid_balance_sweep skipped account %s: enforcement state drift (%s)",
            account.id,
            decision.reason,
        )
        return "state_drift"
    if decision.action in {
        PrepaidEnforcementAction.warn,
        PrepaidEnforcementAction.suspend,
    }:
        return _reconcile_low(
            db,
            account,
            now,
            cfg,
            decision.available_balance,
            decision.required_balance,
            notice_suppression_reason=decision.notice_suppression_reason,
            suspend_now=decision.action == PrepaidEnforcementAction.suspend,
        )
    if decision.action == PrepaidEnforcementAction.not_applicable:
        return "ok"
    return "ok"


@dataclass(frozen=True, slots=True)
class _SweepPrefetch:
    """Run-level observations for the per-account planner.

    Pure reads resolved once for the run slice; every decision and write
    stays per-account inside its own committed unit. Planning from a
    run-start snapshot is safe because the consequence path re-resolves
    live funding before any suspension is confirmed.
    """

    subscriptions_by_account: dict[UUID, list[Subscription]]
    funding_by_account: dict[UUID, PrepaidFundingDecision]
    funding_candidate_ids: set[UUID]
    lock_counts: dict[UUID, int]
    dedicated_account_ids: set[UUID]
    shield_reasons: dict[UUID, str]
    window_block_reason: str | None


_CYCLE_RUNNER = "prepaid_balance_sweep"


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _build_sweep_prefetch(
    db: Session,
    account_order: list,
    *,
    funding_candidate_ids: set,
    now: datetime,
) -> _SweepPrefetch:
    from collections import defaultdict

    from app.services.access_resolution import resolve_prepaid_fundings
    from app.services.prepaid_enforcement_planner import (
        _bulk_dunning_shield_reasons,
        _dedicated_bundle_account_ids,
        _prepaid_lock_counts,
        _window_block_reason,
    )

    ids = [coerce_uuid(str(value)) for value in account_order]
    subscriptions_by_account: dict[UUID, list[Subscription]] = defaultdict(list)
    if ids:
        for subscription in db.scalars(
            select(Subscription).where(Subscription.subscriber_id.in_(ids))
        ):
            subscriptions_by_account[subscription.subscriber_id].append(subscription)
    funding_ids = [value for value in ids if value in funding_candidate_ids]
    return _SweepPrefetch(
        subscriptions_by_account=dict(subscriptions_by_account),
        funding_by_account=resolve_prepaid_fundings(db, funding_ids, now=now),
        funding_candidate_ids=set(funding_candidate_ids),
        lock_counts=_prepaid_lock_counts(db, ids) if ids else {},
        dedicated_account_ids=(
            _dedicated_bundle_account_ids(db, ids) if ids else set()
        ),
        shield_reasons=_bulk_dunning_shield_reasons(db, set(ids)) if ids else {},
        window_block_reason=_window_block_reason(db, now=now),
    )


def _load_cycle_state(db: Session) -> PrepaidSweepCycleState:
    state = db.execute(
        select(PrepaidSweepCycleState)
        .where(PrepaidSweepCycleState.runner == _CYCLE_RUNNER)
        .with_for_update()
    ).scalar_one_or_none()
    if state is None:
        state = PrepaidSweepCycleState(
            runner=_CYCLE_RUNNER, cycle_started_at=datetime.now(UTC)
        )
        db.add(state)
        db.flush()
    return state


def _safe_rollback(db: Session) -> None:
    """Roll back, invalidating the connection when rollback itself fails.

    A Celery soft-timeout signal can interrupt psycopg mid-command; a plain
    ROLLBACK on that connection raises ``another command is already in
    progress`` and would escape the per-account handler, killing the whole
    run unreported. Invalidate hands the session a fresh connection instead.
    """
    try:
        db.rollback()
    except Exception:
        logger.exception(
            "prepaid_balance_sweep_rollback_failed: invalidating connection"
        )
        try:
            db.invalidate()
        except Exception:
            logger.exception("prepaid_balance_sweep_invalidate_failed")


def run_prepaid_balance_sweep(
    db: Session,
    *,
    now: datetime | None = None,
    deadline: datetime | None = None,
) -> dict[str, int | str]:
    """Reconcile every active prepaid account against its balance threshold.

    The lifecycle is permanently active. Commits per account so a single
    failure never aborts the batch; quarantine and evidence failures remain
    account-scoped. When ``deadline`` is set, accounts that do not fit the
    budget are deferred to the next run (counted as ``budget_deferred``) so
    the run always ends cleanly and publishes its snapshot. Fair coverage is
    a persistent keyset cursor, not shuffling: accounts are processed in
    stable key order and each bounded run resumes after the last processed
    key, so a full cycle visits every account exactly once and no tail can
    be starved. ``cycle_remaining``/``cycle_age_seconds`` expose cycle
    progress for alerting.
    """
    run_at = now or datetime.now(UTC)
    cfg = resolve_prepaid_enforcement_policy(db)
    stats: dict[str, int | str] = {
        "accounts_scanned": 0,
        "warned": 0,
        "suspended": 0,
        "restored": 0,
        "deferred": 0,
        "shielded": 0,
        "billing_profile_invalid": 0,
        "coverage_unresolved": 0,
        "renewal_terms_unresolved": 0,
        "funding_quarantined": 0,
        "notice_suppressed": 0,
        "no_contact_route": 0,
        "delivery_unavailable": 0,
        "state_drift": 0,
        "budget_deferred": 0,
        "ok": 0,
        "errors": 0,
    }
    account_ids = candidate_prepaid_account_ids(db)
    from app.services.prepaid_funding_reconstruction import (
        prepaid_funding_incomplete_source_account_ids,
    )

    funding_candidate_ids = candidate_prepaid_funding_account_ids(db)
    incomplete_source_ids = prepaid_funding_incomplete_source_account_ids(
        db, set(account_ids) & funding_candidate_ids
    )
    enforceable_ids = set(account_ids) - incomplete_source_ids
    notice_reasons = prepaid_notice_suppression_reasons(db, enforceable_ids)
    stats["accounts_scanned"] = len(account_ids)
    # Compatibility metric name; complete-history materialization drives it to zero.
    stats["funding_quarantined"] = len(incomplete_source_ids)
    no_contact_account_ids: set[str] = set()
    ordered = sorted(enforceable_ids, key=str)
    cursor = _load_cycle_state(db).cursor_key
    if cursor is None:
        start_index = 0
    else:
        start_index = bisect.bisect_right([str(a) for a in ordered], cursor)
        if start_index >= len(ordered):
            # The previous cycle's tail is done; wrap to a fresh cycle.
            start_index = 0
            cursor = None
    account_order = ordered[start_index:]
    prefetch: _SweepPrefetch | None = None
    try:
        prefetch = _build_sweep_prefetch(
            db,
            account_order,
            funding_candidate_ids=funding_candidate_ids,
            now=run_at,
        )
        db.commit()
    except Exception:
        # Prefetch is an optimization, never a gate: fall back to the
        # per-account resolution path.
        _safe_rollback(db)
        prefetch = None
        logger.exception("prepaid_balance_sweep_prefetch_failed")
    stopped_at: int | None = None
    for position, account_id in enumerate(account_order):
        if deadline is not None and datetime.now(UTC) >= deadline:
            stopped_at = position
            deferred = len(account_order) - position
            stats["budget_deferred"] = int(stats["budget_deferred"]) + deferred
            logger.warning(
                "prepaid_balance_sweep_budget_exhausted: deferred=%d of %d "
                "accounts to the next run",
                deferred,
                len(account_order),
            )
            break
        try:
            account = db.execute(
                select(Subscriber)
                .where(Subscriber.id == coerce_uuid(str(account_id)))
                .with_for_update()
            ).scalar_one_or_none()
            if account is None:
                continue
            outcome = _process_account(
                db,
                account,
                run_at,
                cfg,
                notice_suppression_reason=notice_reasons.get(account.id),
                prefetch=prefetch,
            )
            if outcome == "no_contact_route":
                no_contact_account_ids.add(str(account.id))
            db.commit()
            stats[outcome] = int(stats.get(outcome, 0)) + 1
        except SoftTimeLimitExceeded:
            # The worker's soft limit fired before our own deadline (or none
            # was set): stop DB work immediately, count the remainder as
            # deferred, and let the run end by publishing its snapshot.
            _safe_rollback(db)
            stopped_at = position
            deferred = len(account_order) - position
            stats["budget_deferred"] = int(stats["budget_deferred"]) + deferred
            stats["errors"] = int(stats["errors"]) + 1
            logger.exception(
                "prepaid_balance_sweep_soft_time_limit: deferred=%d accounts",
                deferred,
            )
            break
        except Exception:
            _safe_rollback(db)
            stats["errors"] = int(stats["errors"]) + 1
            logger.exception(
                "prepaid_balance_sweep_account_failed",
                extra={"account_id": str(account_id)},
            )
    try:
        # Checkpoint the coverage cycle: resume after the last processed key
        # next run, or reset when this run reached the end of the cohort.
        state = _load_cycle_state(db)
        if cursor is None:
            state.cycle_started_at = run_at
        if stopped_at is None:
            if account_order:
                state.cursor_key = None
                state.cycles_completed = int(state.cycles_completed) + 1
            cycle_remaining = 0
        elif stopped_at > 0:
            state.cursor_key = str(account_order[stopped_at - 1])
            cycle_remaining = len(account_order) - stopped_at
        else:
            cycle_remaining = len(account_order)
        cycle_age = max(
            0.0,
            (datetime.now(UTC) - _aware(state.cycle_started_at)).total_seconds(),
        )
        stats["cycle_total"] = len(ordered)
        stats["cycle_remaining"] = cycle_remaining
        stats["cycle_age_seconds"] = int(cycle_age)
        stats["cycles_completed"] = int(state.cycles_completed)
        db.commit()
    except Exception:
        _safe_rollback(db)
        logger.exception("prepaid_balance_sweep_cycle_checkpoint_failed")
    try:
        # A work item stays open while its account remains inside prepaid
        # enforcement (armed timer or deactivation marker) — the outcome only
        # fires on the arming pass, so absence from this run's set does not
        # mean the customer became reachable. It closes when the account
        # exits enforcement (restored/funded), which clears both timers.
        from app.services.observability import resolve_findings

        still_enforced = set(
            db.scalars(
                select(Subscriber.id).where(
                    (Subscriber.prepaid_low_balance_at.is_not(None))
                    | (Subscriber.prepaid_deactivation_at.is_not(None))
                )
            ).all()
        )
        resolve_findings(
            db,
            managed_prefix=_NO_CONTACT_FINDING_PREFIX,
            active_fingerprints={
                f"{_NO_CONTACT_FINDING_PREFIX}{value}"
                for value in (
                    no_contact_account_ids | {str(item) for item in still_enforced}
                )
            },
        )
        db.commit()
    except Exception:
        _safe_rollback(db)
        logger.exception("prepaid_balance_sweep_finding_resolution_failed")
    logger.info("prepaid_balance_sweep completed: %s", stats)
    return stats
