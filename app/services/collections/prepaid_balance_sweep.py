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

import logging
import random
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.collections import FinancialAccessOrigin
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
) -> str:
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
    the run always ends cleanly and publishes its snapshot; iteration order
    is shuffled so a persistent tail cannot be starved across runs.
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
        prepaid_funding_quarantined_account_ids,
    )

    funding_candidate_ids = candidate_prepaid_funding_account_ids(db)
    quarantined_ids = prepaid_funding_quarantined_account_ids(
        db, set(account_ids) & funding_candidate_ids
    )
    enforceable_ids = set(account_ids) - quarantined_ids
    notice_reasons = prepaid_notice_suppression_reasons(db, enforceable_ids)
    stats["accounts_scanned"] = len(account_ids)
    stats["funding_quarantined"] = len(quarantined_ids)
    no_contact_account_ids: set[str] = set()
    account_order = sorted(enforceable_ids, key=str)
    # Fair coverage under a budget: a fixed order would starve the same tail
    # every run once the cohort outgrows the budget.
    random.shuffle(account_order)
    for position, account_id in enumerate(account_order):
        if deadline is not None and datetime.now(UTC) >= deadline:
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
