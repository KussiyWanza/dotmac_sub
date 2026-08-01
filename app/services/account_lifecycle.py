"""Subscription / account lifecycle state machine.

All subscription status mutations should go through the domain operations
in this module. Each operation validates preconditions, manages enforcement
locks, emits exactly one canonical event, and recomputes account status.

This module uses ``db.flush()`` intentionally so callers can compose
lifecycle operations within a single transaction. The caller is responsible
for committing or rolling back.

Usage::

    from app.services.account_lifecycle import (
        suspend_subscription,
        restore_subscription,
        activate_subscription,
        expire_subscription,
        cancel_subscription,
        compute_account_status,
    )

    lock = suspend_subscription(
        db, subscription_id,
        reason=EnforcementReason.overdue,
        source=f"dunning_case:{case_id}",
    )

    restored = restore_subscription(
        db, subscription_id,
        trigger="payment",
        resolved_by=f"payment:{payment_id}",
    )
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import SimpleNamespace
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select

from app.models.catalog import (
    BillingMode,
    Subscription,
    SubscriptionAddOn,
    SubscriptionStatus,
)
from app.models.enforcement_lock import (
    AccessRestrictionMode,
    EnforcementLock,
    EnforcementReason,
)
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.prepaid_enforcement_state import clear_prepaid_enforcement_timers

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Allowed restorer triggers per enforcement reason
# ---------------------------------------------------------------------------

ALLOWED_RESTORERS: dict[EnforcementReason, set[str]] = {
    EnforcementReason.overdue: {"payment", "collections_resolution", "admin"},
    EnforcementReason.fup: {"cap_reset", "top_up", "admin"},
    # A payment is only an observation that money moved; it says nothing about
    # whether the prepaid funding threshold was reached.  The access-resolution
    # owner authorizes a reason-scoped ``top_up`` restore only after that gate.
    EnforcementReason.prepaid: {"top_up", "admin"},
    EnforcementReason.admin: {"admin"},
    EnforcementReason.customer_hold: {"customer", "admin"},
    EnforcementReason.fraud: {"admin"},
    EnforcementReason.system: {"system", "admin"},
}

# Verify ALLOWED_RESTORERS covers every enum member at import time
_missing_restorers = set(EnforcementReason) - set(ALLOWED_RESTORERS.keys())
if _missing_restorers:
    raise RuntimeError(f"ALLOWED_RESTORERS missing reasons: {_missing_restorers}")

# Statuses that can be suspended
_SUSPENDABLE = {
    SubscriptionStatus.active,
    SubscriptionStatus.pending,
}

# Statuses treated as equivalent to "suspended" in account derivation
# Legacy statuses that mean the service is not running.
SUSPENDED_EQUIVALENT = {
    SubscriptionStatus.suspended,
    SubscriptionStatus.blocked,
    SubscriptionStatus.stopped,
}

# Terminal statuses — locks are resolved when entering these
_TERMINAL = {
    SubscriptionStatus.canceled,
    SubscriptionStatus.expired,
    SubscriptionStatus.hidden,
    SubscriptionStatus.archived,
}

# Public alias for callers outside this module (e.g. the catalog write path)
# that need to reason about terminal subscription states.
TERMINAL_STATUSES = frozenset(_TERMINAL)
ACCOUNT_OVERRIDE_STATUSES = frozenset(
    {
        SubscriberStatus.new,
        SubscriberStatus.active,
        SubscriberStatus.blocked,
        SubscriberStatus.suspended,
        SubscriberStatus.disabled,
        SubscriberStatus.canceled,
    }
)


@dataclass(frozen=True, slots=True)
class AccountUnsuspendResult:
    """Exact administrative suspension consequences reversed by one command."""

    account_id: UUID
    status: SubscriberStatus
    resolved_lock_ids: tuple[UUID, ...]
    restored_subscription_ids: tuple[UUID, ...]


def is_terminal_status(status: SubscriptionStatus | None) -> bool:
    """True if ``status`` is a terminal (sink) subscription status."""
    return status in _TERMINAL


def _require_billing_approval(
    db: Session,
    *,
    subscriber_id: str,
) -> Subscriber:
    """Fail closed before any transition that would provide active service."""
    subscriber = db.get(Subscriber, subscriber_id)
    if subscriber is None:
        raise ValueError(f"Subscriber {subscriber_id} not found")
    if not subscriber.billing_enabled:
        raise ValueError(
            "Account billing approval is required before service activation"
        )
    return subscriber


def set_account_lifecycle_override(
    db: Session,
    subscriber_id: str,
    status: SubscriberStatus,
    *,
    reason: str,
    source: str,
) -> SubscriberStatus:
    """Persist an explicit account-level lifecycle fact and refresh projection."""
    if status not in ACCOUNT_OVERRIDE_STATUSES:
        raise ValueError(f"{status.value} is derived and cannot be an override")
    subscriber = db.get(Subscriber, subscriber_id)
    if subscriber is None:
        raise ValueError(f"Subscriber {subscriber_id} not found")
    subscriber.lifecycle_override_status = status
    subscriber.lifecycle_override_reason = reason
    subscriber.lifecycle_override_source = source
    subscriber.lifecycle_override_at = datetime.now(UTC)
    return compute_account_status(db, subscriber_id)


def clear_account_lifecycle_override(
    db: Session,
    subscriber_id: str,
    *,
    reason: str,
    source: str,
) -> SubscriberStatus:
    """Clear an administrative account fact and re-derive from subscriptions."""
    subscriber = db.get(Subscriber, subscriber_id)
    if subscriber is None:
        raise ValueError(f"Subscriber {subscriber_id} not found")
    logger.info(
        "Account lifecycle override cleared: subscriber=%s status=%s reason=%s source=%s",
        subscriber_id,
        subscriber.lifecycle_override_status.value
        if subscriber.lifecycle_override_status
        else None,
        reason,
        source,
    )
    subscriber.lifecycle_override_status = None
    subscriber.lifecycle_override_reason = None
    subscriber.lifecycle_override_source = None
    subscriber.lifecycle_override_at = None
    return compute_account_status(db, subscriber_id)


def apply_requested_account_status(
    db: Session,
    subscriber_id: str,
    status: SubscriberStatus,
    *,
    reason: str,
    source: str,
) -> SubscriberStatus:
    """Translate an account status command into an explicit fact or derivation."""
    if status == SubscriberStatus.delinquent:
        raise ValueError("delinquent is derived from collections and cannot be set")
    if status == SubscriberStatus.new:
        return clear_account_lifecycle_override(
            db, subscriber_id, reason=reason, source=source
        )
    if status == SubscriberStatus.active:
        _require_billing_approval(db, subscriber_id=subscriber_id)
        has_subscription = (
            db.scalars(
                select(Subscription.id)
                .where(Subscription.subscriber_id == subscriber_id)
                .limit(1)
            ).first()
            is not None
        )
        if has_subscription:
            return clear_account_lifecycle_override(
                db, subscriber_id, reason=reason, source=source
            )
    return set_account_lifecycle_override(
        db, subscriber_id, status, reason=reason, source=source
    )


def assert_legal_subscription_transition(
    from_status: SubscriptionStatus | None,
    to_status: SubscriptionStatus | None,
) -> None:
    """Compatibility wrapper for the shared lifecycle transition guard.

    Terminal statuses (canceled/expired/hidden/archived) are sinks:
    once a subscription enters one, the only legal "transition" is to stay put.
    This is the single rule the raw catalog write path
    (``Subscriptions.update``) must not bypass — without it an admin/web edit
    can resurrect a dead service straight back to ``active`` (the domain
    operations in this module already reject these edges; the form path did
    not). Re-activation of a terminal service must go through a deliberate new
    subscription, never a status flip.

    Raises:
        ValueError: if ``from_status`` is terminal and differs from
            ``to_status``.
    """
    from app.services.subscription_lifecycle import assert_subscription_transition

    assert_subscription_transition(from_status, to_status)


# ---------------------------------------------------------------------------
# Domain operations
# ---------------------------------------------------------------------------


def suspend_subscription(
    db: Session,
    subscription_id: str,
    reason: EnforcementReason,
    source: str,
    *,
    access_mode: AccessRestrictionMode = AccessRestrictionMode.hard_reject,
    notes: str | None = None,
    emit: bool = True,
) -> EnforcementLock:
    """Create an enforcement lock and suspend the subscription.

    If the subscription is already suspended, the lock is still created
    for audit purposes but no ``subscription_suspended`` event is emitted.
    A newly created lock emits ``enforcement_lock_created``. Reusing an
    existing lock does not emit a second creation event.

    If an active lock with the same reason already exists for this
    subscription, the existing lock is reused. Any drift in the subscription
    or parent-account status is still repaired; lock-row idempotency must not
    leave a service active while its enforcement lock is active.

    Args:
        db: Database session.
        subscription_id: Subscription UUID.
        reason: Why the subscription is being suspended.
        source: Who/what initiated this (e.g. ``"dunning_case:{id}"``).
        notes: Optional human-readable notes.
        emit: Whether to emit events.

    Returns:
        The enforcement lock (new or existing duplicate).

    Raises:
        ValueError: If the subscription cannot be suspended.
    """
    # Lock the subscription row to prevent concurrent mutations
    subscription = db.execute(
        select(Subscription).where(Subscription.id == subscription_id).with_for_update()
    ).scalar_one_or_none()
    if not subscription:
        raise ValueError(f"Subscription {subscription_id} not found")

    prev_status = subscription.status
    was_already_suspended = subscription.status in SUSPENDED_EQUIVALENT

    if subscription.status not in _SUSPENDABLE and not was_already_suspended:
        raise ValueError(
            f"Cannot suspend subscription in status {subscription.status.value}"
        )

    # Check for existing active lock with same reason (idempotent)
    existing = db.scalars(
        select(EnforcementLock).where(
            EnforcementLock.subscription_id == subscription.id,
            EnforcementLock.reason == reason,
            EnforcementLock.is_active.is_(True),
        )
    ).first()
    if existing:
        if (
            access_mode == AccessRestrictionMode.hard_reject
            and existing.access_mode != AccessRestrictionMode.hard_reject
        ):
            existing.access_mode = AccessRestrictionMode.hard_reject
        status_changed = False
        if not was_already_suspended:
            subscription.status = SubscriptionStatus.suspended
            status_changed = True
        db.flush()

        if emit and status_changed:
            emit_event(
                db,
                EventType.subscription_suspended,
                {
                    "subscription_id": str(subscription.id),
                    "reason": reason.value,
                    "access_mode": existing.access_mode.value,
                    "source": source,
                    "from_status": prev_status.value if prev_status else None,
                    "to_status": SubscriptionStatus.suspended.value,
                    "offer_name": subscription.offer.name
                    if subscription.offer
                    else None,
                },
                subscription_id=subscription.id,
                account_id=subscription.subscriber_id,
            )

        compute_account_status(db, str(subscription.subscriber_id))
        logger.info(
            "Existing lock reused: subscription=%s reason=%s source=%s "
            "existing=%s status_repaired=%s",
            subscription_id,
            reason.value,
            source,
            existing.id,
            status_changed,
        )
        return existing

    # Create the enforcement lock
    lock = EnforcementLock(
        subscription_id=subscription.id,
        subscriber_id=subscription.subscriber_id,
        reason=reason,
        access_mode=access_mode,
        source=source,
        is_active=True,
        notes=notes,
    )
    db.add(lock)

    # Transition status if not already suspended
    status_changed = False
    if not was_already_suspended:
        subscription.status = SubscriptionStatus.suspended
        status_changed = True

    db.flush()

    if emit:
        if status_changed:
            emit_event(
                db,
                EventType.subscription_suspended,
                {
                    "subscription_id": str(subscription.id),
                    "reason": reason.value,
                    "access_mode": access_mode.value,
                    "source": source,
                    "from_status": prev_status.value if prev_status else None,
                    "to_status": SubscriptionStatus.suspended.value,
                    "offer_name": subscription.offer.name
                    if subscription.offer
                    else None,
                },
                subscription_id=subscription.id,
                account_id=subscription.subscriber_id,
            )
        # Always emit lock_created, even if status didn't change
        emit_event(
            db,
            EventType.enforcement_lock_created,
            {
                "lock_id": str(lock.id),
                "subscription_id": str(subscription.id),
                "reason": reason.value,
                "access_mode": access_mode.value,
                "source": source,
            },
            subscription_id=subscription.id,
            account_id=subscription.subscriber_id,
        )

    compute_account_status(db, str(subscription.subscriber_id))

    logger.info(
        "Enforcement lock created: subscription=%s reason=%s source=%s "
        "(status_changed=%s)",
        subscription_id,
        reason.value,
        source,
        status_changed,
    )
    return lock


class RestorationOutcome(StrEnum):
    """Why a restoration attempt ended the way it did.

    ``False`` used to carry five different meanings. An operator watching a
    customer who has paid and is still offline could not tell "a payment cannot
    lift a fraud block" from "nothing happened".
    """

    restored = "restored"
    already_active = "already_active"
    blocked_by_remaining_locks = "blocked_by_remaining_locks"
    blocked_by_unauthorized_trigger = "blocked_by_unauthorized_trigger"
    blocked_by_active_login = "blocked_by_active_login"
    blocked_by_lifecycle_override = "blocked_by_lifecycle_override"


# Triggers a settled payment or top-up is allowed to present. Kept as data so
# ``remaining_blockers`` reports exactly what ALLOWED_RESTORERS permits: note
# that ``top_up`` clears BOTH ``fup`` and ``prepaid``, so FUP must never be
# reported as payment-unclearable when the caller is a top-up.
PAYMENT_TRIGGERS: frozenset[str] = frozenset(
    {"payment", "top_up", "collections_resolution"}
)


def payment_clearable_reasons(triggers: frozenset[str] = PAYMENT_TRIGGERS) -> set[str]:
    """Return lock reasons some payment-side trigger is authorized to clear."""
    return {
        reason.value
        for reason, allowed in ALLOWED_RESTORERS.items()
        if allowed & triggers
    }


@dataclass(frozen=True, slots=True)
class SubscriptionRestorationResult:
    """Typed, operator-readable outcome of one restoration attempt."""

    subscription_id: str
    account_id: str
    trigger: str
    outcome: RestorationOutcome
    payment_settled: bool
    financial_lock_cleared: bool
    # Did THIS call write `status = active`? This is the legacy boolean the
    # `restore_subscription` facade returns, so its truthiness is unchanged for
    # the ~10 existing callers: "nothing needed doing" must never read as
    # "I restored something", or a caller notifies a customer, clears an alert,
    # or resolves a worklist entry for an account that was never suspended.
    subscription_reactivated: bool
    # Does the customer actually have service now? Narrower than the above: an
    # account-level lifecycle override keeps them dark even when the
    # subscription row says active.
    access_restored: bool
    resolved_lock_count: int
    remaining_blockers: tuple[str, ...]
    payment_clearable_blockers: tuple[str, ...]
    lifecycle_override_status: str | None
    lifecycle_override_reason: str | None
    lifecycle_override_source: str | None
    required_action: str | None

    @property
    def financially_settled_but_access_blocked(self) -> bool:
        """Money landed and its lock cleared, yet the customer is still off."""
        return (
            self.payment_settled
            and not self.access_restored
            and self.outcome is not RestorationOutcome.already_active
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "subscription_id": self.subscription_id,
            "account_id": self.account_id,
            "trigger": self.trigger,
            "outcome": self.outcome.value,
            "payment_settled": self.payment_settled,
            "financial_lock_cleared": self.financial_lock_cleared,
            "subscription_reactivated": self.subscription_reactivated,
            "access_restored": self.access_restored,
            "resolved_lock_count": self.resolved_lock_count,
            "remaining_blockers": list(self.remaining_blockers),
            "payment_clearable_blockers": list(self.payment_clearable_blockers),
            "lifecycle_override_status": self.lifecycle_override_status,
            "lifecycle_override_reason": self.lifecycle_override_reason,
            "lifecycle_override_source": self.lifecycle_override_source,
            "required_action": self.required_action,
            "financially_settled_but_access_blocked": (
                self.financially_settled_but_access_blocked
            ),
        }


_LIFECYCLE_OVERRIDE_BLOCKER = "lifecycle_override"


def _active_blockers(db: Session, subscription: Subscription) -> tuple[str, ...]:
    reasons = sorted(
        {
            lock.reason.value
            for lock in db.scalars(
                select(EnforcementLock).where(
                    EnforcementLock.subscription_id == subscription.id,
                    EnforcementLock.is_active.is_(True),
                )
            ).all()
        }
    )
    return tuple(reasons)


def _required_action(
    outcome: RestorationOutcome,
    blockers: tuple[str, ...],
    payment_clearable: tuple[str, ...],
) -> str | None:
    if outcome is RestorationOutcome.restored:
        return None
    if outcome is RestorationOutcome.already_active:
        return None
    if outcome is RestorationOutcome.blocked_by_active_login:
        return "resolve_duplicate_active_login"
    if outcome is RestorationOutcome.blocked_by_lifecycle_override:
        return "clear_lifecycle_override"
    if _LIFECYCLE_OVERRIDE_BLOCKER in blockers:
        return "clear_lifecycle_override"
    if payment_clearable:
        # A payment-clearable reason survived the attempt: the trigger used was
        # not the one authorized for it (e.g. `payment` cannot clear `fup`;
        # only `top_up` can).
        return "retry_with_authorized_trigger"
    if blockers:
        return "admin_review"
    return "admin_review"


def _build_restoration_result(
    db: Session,
    subscription: Subscription,
    *,
    trigger: str,
    outcome: RestorationOutcome,
    resolved_count: int,
    subscription_reactivated: bool,
    access_restored: bool,
) -> SubscriptionRestorationResult:
    blockers = list(_active_blockers(db, subscription))
    subscriber = db.get(Subscriber, subscription.subscriber_id)
    override_status = getattr(subscriber, "lifecycle_override_status", None)
    override_blocks = (
        override_status is not None and override_status != SubscriberStatus.active
    )
    if override_blocks:
        # Payment never clears an explicit lifecycle override. It must still be
        # visible, or the operator sees a settled account that stays dark with
        # no stated reason.
        blockers.append(_LIFECYCLE_OVERRIDE_BLOCKER)
        # The subscription row may say active while the account override keeps
        # the customer off. Reporting that as restored is the same silence in
        # a different costume.
        if access_restored:
            access_restored = False
            outcome = RestorationOutcome.blocked_by_lifecycle_override
    blocker_tuple = tuple(dict.fromkeys(blockers))
    clearable = tuple(
        item for item in blocker_tuple if item in payment_clearable_reasons()
    )
    payment_settled = trigger in PAYMENT_TRIGGERS
    result = SubscriptionRestorationResult(
        subscription_id=str(subscription.id),
        account_id=str(subscription.subscriber_id),
        trigger=trigger,
        outcome=outcome,
        payment_settled=payment_settled,
        financial_lock_cleared=payment_settled and resolved_count > 0,
        subscription_reactivated=subscription_reactivated,
        access_restored=access_restored,
        resolved_lock_count=resolved_count,
        remaining_blockers=blocker_tuple,
        payment_clearable_blockers=clearable,
        lifecycle_override_status=(
            override_status.value if override_status is not None else None
        ),
        lifecycle_override_reason=getattr(
            subscriber, "lifecycle_override_reason", None
        ),
        lifecycle_override_source=getattr(
            subscriber, "lifecycle_override_source", None
        ),
        required_action=_required_action(outcome, blocker_tuple, clearable),
    )
    if result.financially_settled_but_access_blocked:
        # Staged here, at the single point where every outcome is built, rather
        # than at the tail of `restore_subscription_detailed`. The early returns
        # for an unauthorized trigger and a duplicate active login are exactly
        # the "paid but still dark" cases the worklist exists for, and staging
        # further down silently skipped both of them.
        from app.services.settled_access_blocked import (
            stage_financially_settled_but_access_blocked,
        )

        stage_financially_settled_but_access_blocked(db, result)
    return result


def restore_subscription_detailed(
    db: Session,
    subscription_id: str,
    trigger: str,
    resolved_by: str,
    *,
    reason: EnforcementReason | None = None,
    notes: str | None = None,
    emit: bool = True,
) -> SubscriptionRestorationResult:
    """Resolve enforcement locks, restore if clear, and say exactly what happened.

    Reason scoping is intentional and unchanged: a payment does not lift a FUP,
    admin, or fraud lock, and no payment path clears an explicit lifecycle
    override. The defect this replaces was silence — the operator was told
    nothing, so a customer who had genuinely paid stayed dark with no worklist
    entry and no stated required action.
    """
    # Lock the subscription row to prevent concurrent restore races
    subscription = db.execute(
        select(Subscription).where(Subscription.id == subscription_id).with_for_update()
    ).scalar_one_or_none()
    if not subscription:
        raise ValueError(f"Subscription {subscription_id} not found")

    if subscription.status not in SUSPENDED_EQUIVALENT:
        logger.warning(
            "restore_subscription called but subscription %s is %s, not suspended",
            subscription_id,
            subscription.status.value,
        )
        return _build_restoration_result(
            db,
            subscription,
            trigger=trigger,
            outcome=RestorationOutcome.already_active,
            resolved_count=0,
            subscription_reactivated=False,
            access_restored=subscription.status == SubscriptionStatus.active,
        )

    _require_billing_approval(
        db,
        subscriber_id=str(subscription.subscriber_id),
    )

    resolved_count, remaining = resolve_locks_for_trigger(
        db,
        subscription,
        trigger=trigger,
        resolved_by=resolved_by,
        reason=reason,
        notes=notes,
        emit=emit,
    )

    if resolved_count == 0 and remaining is not None:
        # Locks exist but this trigger may not clear them. Refusing is correct:
        # e.g. a payment must not lift a fraud block.
        logger.warning(
            "No locks resolved for subscription %s with trigger %r "
            "(active_locks=%d, trigger not authorized)",
            subscription_id,
            trigger,
            len(get_active_locks(db, subscription_id=str(subscription.id))),
        )
        return _build_restoration_result(
            db,
            subscription,
            trigger=trigger,
            outcome=RestorationOutcome.blocked_by_unauthorized_trigger,
            resolved_count=0,
            subscription_reactivated=False,
            access_restored=False,
        )

    # resolved_count == 0 with NO remaining lock means the subscription is
    # suspended with nothing holding it down — a lock resolved out of band, or
    # legacy split-brain drift. That IS restorable, and the owner says so once,
    # here. Previously it returned False and every caller invented its own
    # fallback: the reseller portal raw-wrote ``status = active``, which emitted
    # no event, so the IP was never re-provisioned and RADIUS never re-synced —
    # the UI said active while the customer stayed offline.

    restored = False
    outcome = RestorationOutcome.blocked_by_remaining_locks
    if remaining is None:
        if reactivation_blocked_by_active_login(db, subscription):
            logger.warning(
                "Subscription %s not restored: subscriber %s already has an "
                "active subscription on login %r",
                subscription.id,
                subscription.subscriber_id,
                subscription.login,
            )
            compute_account_status(db, str(subscription.subscriber_id))
            return _build_restoration_result(
                db,
                subscription,
                trigger=trigger,
                outcome=RestorationOutcome.blocked_by_active_login,
                resolved_count=resolved_count,
                subscription_reactivated=False,
                access_restored=False,
            )

        prev_status = subscription.status
        subscription.status = SubscriptionStatus.active
        db.flush()
        restored = True
        outcome = RestorationOutcome.restored

        if emit:
            emit_event(
                db,
                EventType.subscription_resumed,
                {
                    "subscription_id": str(subscription.id),
                    "trigger": trigger,
                    "resolved_by": resolved_by,
                    "from_status": prev_status.value if prev_status else None,
                    "to_status": SubscriptionStatus.active.value,
                    "offer_name": subscription.offer.name
                    if subscription.offer
                    else None,
                },
                subscription_id=subscription.id,
                account_id=subscription.subscriber_id,
            )

        logger.info(
            "Subscription %s restored to active (trigger=%s resolved_by=%s)",
            subscription_id,
            trigger,
            resolved_by,
        )
    else:
        logger.info(
            "Subscription %s still has active locks after resolving %d (trigger=%s)",
            subscription_id,
            resolved_count,
            trigger,
        )

    compute_account_status(db, str(subscription.subscriber_id))
    # Worklist staging lives in `_build_restoration_result`, so every outcome —
    # including the early-return branches above — reaches the operator queue.
    return _build_restoration_result(
        db,
        subscription,
        trigger=trigger,
        outcome=outcome,
        resolved_count=resolved_count,
        subscription_reactivated=restored,
        access_restored=restored,
    )


def restore_subscription(
    db: Session,
    subscription_id: str,
    trigger: str,
    resolved_by: str,
    *,
    reason: EnforcementReason | None = None,
    notes: str | None = None,
    emit: bool = True,
) -> bool:
    """Boolean facade over :func:`restore_subscription_detailed`.

    Returns ``subscription_reactivated``, NOT ``access_restored``: the legacy
    contract is "did this call transition the subscription to active", and the
    existing callers were not reviewed against any other meaning. In
    particular an already-active subscription must keep returning False —
    "nothing needed doing" is not "I restored something", and a caller that
    reads True as a restoration may notify a customer, clear an alert, or
    resolve a worklist entry for an account that was never suspended.

    Callers that need the richer picture — remaining blockers, a lifecycle
    override still holding the customer off, the required operator action —
    should call :func:`restore_subscription_detailed` directly.

    Returns:
        True if this call actually restored the subscription to active.
    """
    return restore_subscription_detailed(
        db,
        subscription_id,
        trigger,
        resolved_by,
        reason=reason,
        notes=notes,
        emit=emit,
    ).subscription_reactivated


def resolve_stale_lock_without_restoration(
    db: Session,
    subscription_id: str,
    *,
    trigger: str,
    resolved_by: str,
    reason: EnforcementReason,
    notes: str | None = None,
    emit: bool = True,
) -> int:
    """Resolve an obsolete reason lock without activating a non-service.

    Financial reconciliation uses this only when its locked preview proves the
    linked subscription is no longer restorable service (for example terminal
    after leaving the prepaid cohort). Suspended services must use
    ``restore_subscription`` so status and access consequences stay atomic.
    """
    subscription = db.execute(
        select(Subscription).where(Subscription.id == subscription_id).with_for_update()
    ).scalar_one_or_none()
    if subscription is None:
        raise ValueError(f"Subscription {subscription_id} not found")
    if subscription.status in SUSPENDED_EQUIVALENT:
        raise ValueError(
            "A suspended subscription lock must be resolved through restoration"
        )
    resolved_count, _remaining = resolve_locks_for_trigger(
        db,
        subscription,
        trigger=trigger,
        resolved_by=resolved_by,
        reason=reason,
        notes=notes,
        emit=emit,
    )
    compute_account_status(db, str(subscription.subscriber_id))
    return resolved_count


def activate_subscription(
    db: Session,
    subscription_id: str,
    *,
    start_at: datetime | None = None,
    emit: bool = True,
) -> None:
    """Transition subscription from pending to active.

    Args:
        db: Database session.
        subscription_id: Subscription UUID.
        start_at: Override start date. Defaults to now.
        emit: Whether to emit events.

    Raises:
        ValueError: If the subscription is not in pending status.
    """
    subscription = db.get(Subscription, subscription_id)
    if not subscription:
        raise ValueError(f"Subscription {subscription_id} not found")

    if subscription.status != SubscriptionStatus.pending:
        raise ValueError(
            f"Cannot activate subscription in status {subscription.status.value}"
        )

    _require_billing_approval(
        db,
        subscriber_id=str(subscription.subscriber_id),
    )

    # Sales-created service contracts are gated by the canonical provisioning
    # result.  Billing settlement may fund the pending Subscription, but it may
    # not bypass verified implementation and a successful ServiceOrder.
    from app.models.provisioning import ServiceOrder, ServiceOrderStatus

    sales_service_orders = list(
        db.scalars(
            select(ServiceOrder).where(
                ServiceOrder.subscription_id == subscription.id,
                ServiceOrder.sales_order_line_id.is_not(None),
            )
        ).all()
    )
    if sales_service_orders and any(
        order.status != ServiceOrderStatus.active for order in sales_service_orders
    ):
        raise ValueError(
            "Sales subscription requires successful service-order provisioning"
        )

    prev_status = subscription.status
    subscription.status = SubscriptionStatus.active
    if start_at and not subscription.start_at:
        subscription.start_at = start_at
    elif not subscription.start_at:
        subscription.start_at = datetime.now(UTC)

    db.flush()

    subscriber = subscription.subscriber
    if subscriber is not None and subscriber.party_id is not None:
        from app.models.party import PartyRoleStatus, PartyRoleType
        from app.services import party as party_service

        party_service.ensure_role(
            db,
            party_id=subscriber.party_id,
            role_type=PartyRoleType.subscriber,
            status=PartyRoleStatus.active,
            source="account_lifecycle.activate_subscription",
        )

    if emit:
        emit_event(
            db,
            EventType.subscription_activated,
            {
                "subscription_id": str(subscription.id),
                "from_status": prev_status.value if prev_status else None,
                "to_status": SubscriptionStatus.active.value,
                "offer_name": subscription.offer.name if subscription.offer else None,
            },
            subscription_id=subscription.id,
            account_id=subscription.subscriber_id,
        )

    compute_account_status(db, str(subscription.subscriber_id))

    logger.info("Subscription %s activated", subscription_id)


def _release_service_ips(db: Session, subscription: Subscription) -> None:
    """Forward fix (idempotent, guarded): terminal service owns no service IPs.
    Wrapped so an IP-release failure never breaks the lifecycle transaction."""
    try:
        from app.services.ip_lifecycle import release_service_ips_for_subscription

        release_service_ips_for_subscription(db, subscription)
    except Exception:
        logger.warning(
            "service-IP release failed for terminal subscription %s",
            subscription.id,
            exc_info=True,
        )


def expire_subscription(
    db: Session,
    subscription_id: str,
    *,
    emit: bool = True,
) -> None:
    """Transition subscription to expired (terminal state).

    Resolves all active enforcement locks since they no longer apply.

    Raises:
        ValueError: If the subscription is already in a terminal state.
    """
    subscription = db.get(Subscription, subscription_id)
    if not subscription:
        raise ValueError(f"Subscription {subscription_id} not found")

    if subscription.status in _TERMINAL:
        raise ValueError(
            f"Cannot expire subscription already in {subscription.status.value}"
        )

    resolved_count = resolve_all_locks(db, subscription, "expired")

    prev_status = subscription.status
    subscription.status = SubscriptionStatus.expired
    db.flush()

    _release_service_ips(db, subscription)
    compute_account_status(db, str(subscription.subscriber_id))

    if emit:
        emit_event(
            db,
            EventType.subscription_expired,
            {
                "subscription_id": str(subscription.id),
                "reason": "expired",
                "from_status": prev_status.value if prev_status else None,
                "to_status": SubscriptionStatus.expired.value,
                "offer_name": subscription.offer.name if subscription.offer else None,
            },
            subscription_id=subscription.id,
            account_id=subscription.subscriber_id,
        )
    logger.info(
        "Subscription %s expired (locks_resolved=%d)", subscription_id, resolved_count
    )


def cancel_subscription(
    db: Session,
    subscription_id: str,
    cancel_reason: str,
    source: str,
    *,
    emit: bool = True,
    generate_credit: bool = True,
) -> None:
    """Transition subscription to canceled (terminal state).

    Args:
        db: Database session.
        subscription_id: Subscription UUID.
        cancel_reason: Cancellation reason (stored on subscription).
        source: Who/what canceled this.
        emit: Whether to emit events.

    Raises:
        ValueError: If subscription is already canceled.
    """
    subscription = db.get(Subscription, subscription_id)
    if not subscription:
        raise ValueError(f"Subscription {subscription_id} not found")

    if subscription.status == SubscriptionStatus.canceled:
        raise ValueError("Subscription is already canceled")

    resolved_count = resolve_all_locks(db, subscription, "canceled")

    prev_status = subscription.status
    subscription.status = SubscriptionStatus.canceled
    subscription.canceled_at = datetime.now(UTC)
    subscription.cancel_reason = cancel_reason

    # End any active add-ons so they stop billing once the service is canceled.
    for sub_addon in db.scalars(
        select(SubscriptionAddOn).where(
            SubscriptionAddOn.subscription_id == subscription.id,
            SubscriptionAddOn.end_at.is_(None),
        )
    ).all():
        sub_addon.end_at = subscription.canceled_at

    db.flush()

    _release_service_ips(db, subscription)

    # Generate credit note for unused portion of the billing period.
    # Use a savepoint so a credit note failure doesn't corrupt the
    # cancel transaction (the cancellation itself is already flushed).
    if generate_credit:
        try:
            from app.services.billing_automation import generate_cancellation_credit

            # Keep credit generation inside its own managed savepoint. Calling
            # Session.rollback() here would roll back the entire cancellation
            # transaction, not just the failed credit operation.
            with db.begin_nested():
                generate_cancellation_credit(db, subscription)
        except Exception as exc:
            logger.warning(
                "Cancellation credit generation failed for subscription %s: %s",
                subscription_id,
                exc,
            )

    compute_account_status(db, str(subscription.subscriber_id))

    if emit:
        emit_event(
            db,
            EventType.subscription_canceled,
            {
                "subscription_id": str(subscription.id),
                "cancel_reason": cancel_reason,
                "reason": cancel_reason,
                "source": source,
                "from_status": prev_status.value if prev_status else None,
                "to_status": SubscriptionStatus.canceled.value,
                "offer_name": subscription.offer.name if subscription.offer else None,
            },
            subscription_id=subscription.id,
            account_id=subscription.subscriber_id,
        )
    logger.info(
        "Subscription %s canceled (reason=%s source=%s locks_resolved=%d)",
        subscription_id,
        cancel_reason,
        source,
        resolved_count,
    )


def disable_subscription(
    db: Session,
    subscription_id: str,
    *,
    reason: str,
    source: str,
    emit: bool = True,
    preserve_locks: bool = False,
) -> bool:
    """Pause billing and access while preserving service assignments.

    Returns ``False`` when the subscription is already disabled. Terminal states
    remain immutable and must not be rewritten as disabled.
    """
    subscription = db.execute(
        select(Subscription).where(Subscription.id == subscription_id).with_for_update()
    ).scalar_one_or_none()
    if subscription is None:
        raise ValueError(f"Subscription {subscription_id} not found")
    if subscription.status == SubscriptionStatus.disabled:
        return False
    if subscription.status in _TERMINAL:
        raise ValueError(
            f"Cannot disable subscription already in {subscription.status.value}"
        )

    resolved_count = (
        0 if preserve_locks else resolve_all_locks(db, subscription, "disabled")
    )
    previous_status = subscription.status
    subscription.status = SubscriptionStatus.disabled
    db.flush()
    compute_account_status(db, str(subscription.subscriber_id))

    if emit:
        emit_event(
            db,
            EventType.subscription_disabled,
            {
                "subscription_id": str(subscription.id),
                "reason": reason,
                "source": source,
                "from_status": previous_status.value,
                "to_status": SubscriptionStatus.disabled.value,
                "offer_name": subscription.offer.name if subscription.offer else None,
            },
            subscription_id=subscription.id,
            account_id=subscription.subscriber_id,
        )
    logger.info(
        "Subscription %s disabled (reason=%s source=%s locks_resolved=%d)",
        subscription_id,
        reason,
        source,
        resolved_count,
    )
    return True


def enable_subscription(
    db: Session,
    subscription_id: str,
    *,
    reason: str,
    source: str,
    emit: bool = True,
) -> bool:
    """Return a disabled subscription to active without rebuilding its service."""
    subscription = db.execute(
        select(Subscription).where(Subscription.id == subscription_id).with_for_update()
    ).scalar_one_or_none()
    if subscription is None:
        raise ValueError(f"Subscription {subscription_id} not found")
    if subscription.status != SubscriptionStatus.disabled:
        return False
    _require_billing_approval(
        db,
        subscriber_id=str(subscription.subscriber_id),
    )
    active_locks = get_active_locks(db, subscription_id=str(subscription.id))
    if active_locks:
        subscription.status = SubscriptionStatus.suspended
        db.flush()
        compute_account_status(db, str(subscription.subscriber_id))
        if emit:
            emit_event(
                db,
                EventType.subscription_suspended,
                {
                    "subscription_id": str(subscription.id),
                    "reason": "active_enforcement_lock",
                    "source": source,
                    "from_status": SubscriptionStatus.disabled.value,
                    "to_status": SubscriptionStatus.suspended.value,
                    "offer_name": (
                        subscription.offer.name if subscription.offer else None
                    ),
                },
                subscription_id=subscription.id,
                account_id=subscription.subscriber_id,
            )
        logger.info(
            "Subscription %s remains suspended after enable request because "
            "%d enforcement locks are active",
            subscription_id,
            len(active_locks),
        )
        return False
    if reactivation_blocked_by_active_login(db, subscription):
        compute_account_status(db, str(subscription.subscriber_id))
        return False

    from app.models.lifecycle import SubscriptionLifecycleEvent
    from app.services.catalog.subscriptions import (
        _compute_next_billing_at,
        _resolve_billing_cycle,
    )

    resumed_at = datetime.now(UTC)
    disabled_at = db.scalar(
        select(SubscriptionLifecycleEvent.created_at)
        .where(SubscriptionLifecycleEvent.subscription_id == subscription.id)
        .where(SubscriptionLifecycleEvent.to_status == SubscriptionStatus.disabled)
        .order_by(SubscriptionLifecycleEvent.created_at.desc())
        .limit(1)
    )
    pause_started_at = disabled_at or subscription.updated_at or resumed_at
    if pause_started_at.tzinfo is None:
        pause_started_at = pause_started_at.replace(tzinfo=UTC)
    paused_for = max(resumed_at - pause_started_at, timedelta(0))
    cycle = subscription.billing_cycle or _resolve_billing_cycle(
        db,
        str(subscription.offer_id),
        str(subscription.offer_version_id) if subscription.offer_version_id else None,
    )
    subscription.status = SubscriptionStatus.active
    subscription.start_at = subscription.start_at or resumed_at
    if subscription.next_billing_at:
        next_billing_at = subscription.next_billing_at
        if next_billing_at.tzinfo is None:
            next_billing_at = next_billing_at.replace(tzinfo=UTC)
        subscription.next_billing_at = next_billing_at + paused_for
    else:
        subscription.next_billing_at = _compute_next_billing_at(resumed_at, cycle)
    db.flush()
    compute_account_status(db, str(subscription.subscriber_id))
    if emit:
        emit_event(
            db,
            EventType.subscription_resumed,
            {
                "subscription_id": str(subscription.id),
                "reason": reason,
                "source": source,
                "from_status": SubscriptionStatus.disabled.value,
                "to_status": SubscriptionStatus.active.value,
                "offer_name": subscription.offer.name if subscription.offer else None,
            },
            subscription_id=subscription.id,
            account_id=subscription.subscriber_id,
        )
    logger.info("Subscription %s enabled by %s", subscription_id, source)
    return True


def transition_subscription_status(
    db: Session,
    subscription_id: str,
    target_status: SubscriptionStatus,
    *,
    reason: str,
    source: str,
    emit: bool = True,
) -> bool:
    """Route an administrative status request through a legal domain command."""
    subscription = db.get(Subscription, subscription_id)
    if subscription is None:
        raise ValueError(f"Subscription {subscription_id} not found")
    if subscription.status == target_status:
        return False
    assert_legal_subscription_transition(subscription.status, target_status)

    if target_status == SubscriptionStatus.active:
        if subscription.status == SubscriptionStatus.pending:
            activate_subscription(db, subscription_id, emit=emit)
            return True
        if subscription.status == SubscriptionStatus.disabled:
            return enable_subscription(
                db,
                subscription_id,
                reason=reason,
                source=source,
                emit=emit,
            )
        if subscription.status in SUSPENDED_EQUIVALENT:
            return restore_subscription(
                db,
                subscription_id,
                trigger="admin",
                resolved_by=source,
                notes=reason,
                emit=emit,
            )
    elif target_status == SubscriptionStatus.suspended:
        suspend_subscription(
            db,
            subscription_id,
            reason=EnforcementReason.admin,
            source=source,
            notes=reason,
            emit=emit,
        )
        return True
    elif target_status == SubscriptionStatus.canceled:
        cancel_subscription(db, subscription_id, reason, source, emit=emit)
        return True
    elif target_status == SubscriptionStatus.expired:
        expire_subscription(db, subscription_id, emit=emit)
        return True
    elif target_status == SubscriptionStatus.disabled:
        return disable_subscription(
            db,
            subscription_id,
            reason=reason,
            source=source,
            emit=emit,
        )
    raise ValueError(f"Unsupported lifecycle target status {target_status.value}")


def transition_account_status(
    db: Session,
    subscriber_id: str,
    target_status: SubscriberStatus,
    *,
    reason: str,
    source: str,
    emit: bool = True,
    preserve_locks: bool = False,
) -> SubscriberStatus:
    """Apply an account-level command and align all owned subscription facts."""
    subscriber = db.get(Subscriber, subscriber_id)
    if subscriber is None:
        raise ValueError(f"Subscriber {subscriber_id} not found")
    if target_status == SubscriberStatus.active:
        _require_billing_approval(db, subscriber_id=subscriber_id)
    subscriptions = list(
        db.scalars(
            select(Subscription).where(Subscription.subscriber_id == subscriber.id)
        ).all()
    )
    if target_status in {SubscriberStatus.active, SubscriberStatus.new}:
        clear_account_lifecycle_override(
            db, subscriber_id, reason=reason, source=source
        )
        if target_status == SubscriberStatus.active:
            for subscription in subscriptions:
                if subscription.status in SUSPENDED_EQUIVALENT | {
                    SubscriptionStatus.disabled
                }:
                    transition_subscription_status(
                        db,
                        str(subscription.id),
                        SubscriptionStatus.active,
                        reason=reason,
                        source=source,
                        emit=emit,
                    )
    else:
        set_account_lifecycle_override(
            db,
            subscriber_id,
            target_status,
            reason=reason,
            source=source,
        )

    if target_status in {SubscriberStatus.blocked, SubscriberStatus.suspended}:
        for subscription in subscriptions:
            if subscription.status in _SUSPENDABLE:
                suspend_subscription(
                    db,
                    str(subscription.id),
                    reason=EnforcementReason.admin,
                    source=source,
                    notes=reason,
                    emit=emit,
                )
    elif target_status == SubscriberStatus.disabled:
        for subscription in subscriptions:
            if subscription.status not in _TERMINAL:
                disable_subscription(
                    db,
                    str(subscription.id),
                    reason=reason,
                    source=source,
                    emit=emit,
                    preserve_locks=preserve_locks,
                )
    elif target_status == SubscriberStatus.canceled:
        for subscription in subscriptions:
            if subscription.status not in _TERMINAL:
                cancel_subscription(
                    db,
                    str(subscription.id),
                    reason,
                    source,
                    emit=emit,
                )
    return compute_account_status(db, subscriber_id)


def unsuspend_account_override(
    db: Session,
    subscriber_id: str,
    *,
    reason: str,
    source: str,
    emit: bool = True,
) -> AccountUnsuspendResult:
    """Reverse only the consequences owned by one account suspension.

    The broad ``active`` transition is intentionally not used here: it may
    enable disabled historical subscriptions and clear unrelated locks. This
    operation clears an explicit suspended account override, resolves only
    same-source administrative locks, and restores only services held by those
    exact locks when no independent blocker remains.
    """

    subscriber = db.get(Subscriber, subscriber_id)
    if subscriber is None:
        raise ValueError(f"Subscriber {subscriber_id} not found")
    if subscriber.lifecycle_override_status != SubscriberStatus.suspended:
        raise ValueError("Account does not have an explicit suspended override")
    _require_billing_approval(db, subscriber_id=subscriber_id)

    recorded_source = str(subscriber.lifecycle_override_source or "").strip()
    subscriptions = list(
        db.scalars(
            select(Subscription)
            .where(Subscription.subscriber_id == subscriber.id)
            .order_by(Subscription.id)
        ).all()
    )
    now = datetime.now(UTC)
    resolved_lock_ids: list[UUID] = []
    restored_subscription_ids: list[UUID] = []

    for subscription in subscriptions:
        active_locks = list(
            db.scalars(
                select(EnforcementLock)
                .where(
                    EnforcementLock.subscription_id == subscription.id,
                    EnforcementLock.is_active.is_(True),
                )
                .order_by(EnforcementLock.id)
            ).all()
        )
        matching = [
            lock
            for lock in active_locks
            if recorded_source
            and lock.reason == EnforcementReason.admin
            and lock.source == recorded_source
        ]
        if not matching:
            continue

        independent = [lock for lock in active_locks if lock not in matching]
        would_restore = subscription.status in SUSPENDED_EQUIVALENT and not independent
        if would_restore and reactivation_blocked_by_active_login(db, subscription):
            # Keep the owned lock active when restoring the corresponding
            # service would violate the one-active-service-per-login invariant.
            continue

        for lock in matching:
            lock.is_active = False
            lock.resolved_at = now
            lock.resolved_by = source
            existing_notes = lock.notes or ""
            resolution_note = f"Resolved: {reason}"
            lock.notes = (
                f"{existing_notes}\n{resolution_note}"
                if existing_notes
                else resolution_note
            )
            resolved_lock_ids.append(lock.id)
            if emit:
                emit_event(
                    db,
                    EventType.enforcement_lock_resolved,
                    {
                        "lock_id": str(lock.id),
                        "subscription_id": str(subscription.id),
                        "reason": lock.reason.value,
                        "trigger": "account_unsuspend",
                        "resolved_by": source,
                    },
                    subscription_id=subscription.id,
                    account_id=subscription.subscriber_id,
                )

        if would_restore:
            previous = subscription.status
            subscription.status = SubscriptionStatus.active
            restored_subscription_ids.append(subscription.id)
            if emit:
                emit_event(
                    db,
                    EventType.subscription_resumed,
                    {
                        "subscription_id": str(subscription.id),
                        "trigger": "account_unsuspend",
                        "resolved_by": source,
                        "from_status": previous.value,
                        "to_status": SubscriptionStatus.active.value,
                        "offer_name": (
                            subscription.offer.name if subscription.offer else None
                        ),
                    },
                    subscription_id=subscription.id,
                    account_id=subscription.subscriber_id,
                )

    db.flush()
    status = clear_account_lifecycle_override(
        db,
        subscriber_id,
        reason=reason,
        source=source,
    )
    return AccountUnsuspendResult(
        account_id=subscriber.id,
        status=status,
        resolved_lock_ids=tuple(resolved_lock_ids),
        restored_subscription_ids=tuple(restored_subscription_ids),
    )


# ---------------------------------------------------------------------------
# Derived account status
# ---------------------------------------------------------------------------


def _has_open_dunning_case(db: Session, subscriber_id: str) -> bool:
    """True if the subscriber has an open dunning case.

    An open ``DunningCase`` is the durable, explicit "past due" signal (the
    dunning runner opens one when an account goes overdue and resolves it on
    payment). It is the source of truth for the derived ``delinquent`` status
    so that signal survives re-derivation instead of being a volatile
    side-channel that the next lifecycle op erases.
    """
    from app.models.collections import DunningCase, DunningCaseStatus

    return (
        db.scalars(
            select(DunningCase.id).where(
                DunningCase.account_id == subscriber_id,
                DunningCase.status == DunningCaseStatus.open,
            )
        ).first()
        is not None
    )


def _credit_covers_open_ar(db: Session, subscriber_id: str) -> bool:
    """True when the account's available balance covers its open AR.

    A prepaid customer whose financial position meets the canonical access
    threshold is not "past due" even if an individual invoice's
    ``balance_due`` is still open: enforcement already treats them as covered
    and never suspends them. Deriving ``delinquent`` from that same decision
    keeps status/display from diverging from enforcement. Postpaid legacy
    handling retains its non-negative net-credit check. Deferred imports avoid
    a collections<->lifecycle cycle.
    """
    from app.services.access_resolution import resolve_prepaid_funding
    from app.services.billing_profile import resolve_billing_profile
    from app.services.collections._core import get_available_balance
    from app.services.customer_financial_ledger import list_customer_financial_events

    try:
        events = list_customer_financial_events(db, subscriber_id, currency=None)
        if not any(event.signed_amount > 0 for event in events):
            return False
        account = db.get(Subscriber, subscriber_id)
        if account is not None:
            profile = resolve_billing_profile(db, account)
            if profile.effective_mode == BillingMode.prepaid:
                return (
                    profile.automation_safe
                    and resolve_prepaid_funding(db, account).funded
                )
        return get_available_balance(db, subscriber_id) >= 0
    except Exception:  # never let a balance-calc hiccup wedge status derivation
        logger.warning(
            "available-balance check failed for %s; treating as uncovered",
            subscriber_id,
            exc_info=True,
        )
        return False


def derive_account_status_without_override(
    db: Session, subscriber_id: str
) -> SubscriberStatus:
    """Return the account status derived only from subscription lifecycle facts."""
    subscriber = db.get(Subscriber, subscriber_id)
    if not subscriber:
        raise ValueError(f"Subscriber {subscriber_id} not found")

    subs = list(
        db.scalars(
            select(Subscription).where(Subscription.subscriber_id == subscriber.id)
        ).all()
    )

    if not subs:
        return SubscriberStatus.new
    if any(s.status == SubscriptionStatus.active for s in subs):
        return (
            SubscriberStatus.delinquent
            if _has_open_dunning_case(db, str(subscriber.id))
            and not _credit_covers_open_ar(db, str(subscriber.id))
            else SubscriberStatus.active
        )
    if any(s.status == SubscriptionStatus.suspended for s in subs):
        return SubscriberStatus.suspended
    if any(
        s.status in {SubscriptionStatus.blocked, SubscriptionStatus.stopped}
        for s in subs
    ):
        return SubscriberStatus.blocked
    if any(s.status == SubscriptionStatus.pending for s in subs):
        return SubscriberStatus.new
    if all(s.status == SubscriptionStatus.disabled for s in subs):
        return SubscriberStatus.disabled
    return SubscriberStatus.canceled


def derive_account_status(db: Session, subscriber_id: str) -> SubscriberStatus:
    """Return the subscriber status implied by authoritative lifecycle facts.

    This is the read-only half of ``compute_account_status``. Audits and
    enforcement planners must be able to report projection drift without
    temporarily mutating an ORM object or relying on a transaction rollback.
    An explicit account override outranks the derived service projection.
    """
    subscriber = db.get(Subscriber, subscriber_id)
    if not subscriber:
        raise ValueError(f"Subscriber {subscriber_id} not found")

    if subscriber.lifecycle_override_status is not None:
        return subscriber.lifecycle_override_status

    return derive_account_status_without_override(db, subscriber_id)


def derive_account_active_projection(
    subscriber: Subscriber,
    *,
    account_status: SubscriberStatus | None = None,
) -> bool:
    """Return the portal/account-active projection for canonical account state."""
    projected_status = account_status or subscriber.status
    explicitly_suspended = (
        subscriber.lifecycle_override_status == SubscriberStatus.suspended
    )
    return projected_status in {
        SubscriberStatus.active,
        SubscriberStatus.new,
        SubscriberStatus.blocked,
        SubscriberStatus.delinquent,
    } or (projected_status == SubscriberStatus.suspended and not explicitly_suspended)


def derive_subscription_access_projection(
    db: Session,
    subscriber: Subscriber,
    *,
    account_status: SubscriberStatus | None = None,
) -> dict[UUID, str | None]:
    """Return every child service's canonical desired access-state value."""
    from app.services.access_resolution import resolve_customer_access
    from app.services.walled_garden_policy import resolve_subscription_restriction

    projected_status = account_status or subscriber.status
    projected_is_active = derive_account_active_projection(
        subscriber,
        account_status=projected_status,
    )
    account_input = SimpleNamespace(
        id=subscriber.id,
        status=projected_status,
        billing_mode=subscriber.billing_mode,
        is_active=projected_is_active,
        billing_enabled=subscriber.billing_enabled,
        captive_redirect_enabled=subscriber.captive_redirect_enabled,
        user_type=subscriber.user_type,
        metadata_=subscriber.metadata_,
        reseller=subscriber.reseller,
        reseller_id=subscriber.reseller_id,
    )
    projected: dict[UUID, str | None] = {}
    subscriptions = list(
        db.scalars(
            select(Subscription).where(Subscription.subscriber_id == subscriber.id)
        ).all()
    )
    for subscription in subscriptions:
        restriction = resolve_subscription_restriction(
            db,
            subscription,
            account=account_input,  # type: ignore[arg-type]
        )
        decision = resolve_customer_access(
            subscription,
            subscriber=account_input,
            access_restriction_mode=(
                restriction.effective_mode if restriction is not None else None
            ),
        )
        state = decision.radius_access_state
        projected[subscription.id] = state.value if state is not None else None
    return projected


def _stage_subscription_access_projection(
    db: Session,
    subscriber: Subscriber,
) -> int:
    """Stage every child service's canonical desired access state.

    ``Subscriber.status`` is a projection of lifecycle facts, so it must not be
    persisted independently from the access decisions that consume it.  Keeping
    both projections in this owner transaction prevents a stale parent status
    from being fed back into RADIUS policy after a payment or restoration.
    External RADIUS remains eventual and is reconciled only after commit.
    """
    # The application session runs with autoflush disabled. Persist newly
    # staged lock transitions before deriving access so this transaction cannot
    # project yesterday's lock set.
    db.flush()
    changed = 0
    subscriptions = {
        subscription.id: subscription
        for subscription in db.scalars(
            select(Subscription).where(Subscription.subscriber_id == subscriber.id)
        ).all()
    }
    for subscription_id, projected in derive_subscription_access_projection(
        db, subscriber
    ).items():
        subscription = subscriptions[subscription_id]
        if subscription.access_state == projected:
            continue
        subscription.access_state = projected
        changed += 1
    if changed:
        db.flush()
        logger.info(
            "Account access projection staged: subscriber=%s subscriptions_changed=%s",
            subscriber.id,
            changed,
        )
    return changed


def compute_account_status(db: Session, subscriber_id: str) -> SubscriberStatus:
    """Derive and persist subscriber status from subscription states.

    Priority order:
      1. Any subscription active →
            ``delinquent`` if an open dunning case exists AND available credit
            does not cover open AR (past due, service still running,
            pre-suspension), else ``active``. Netting credit here mirrors the
            enforcement suspend gate so status can't say "delinquent" while
            enforcement (correctly) treats the account as covered.
      2. Any subscription suspended → suspended
      3. Any subscription blocked/stopped → blocked
      4. Any subscription pending → new
      5. All remaining subscriptions disabled → disabled
      6. All terminal (canceled/expired/hidden/archived) → canceled
      7. No subscriptions → new

    ``delinquent`` is only reachable from the active branch by design: once a
    subscription is suspended/blocked the stronger status wins, and once all
    are terminal the account is canceled. This makes the derivation
    the single producer of ``delinquent`` (it was previously written
    out-of-band and silently clobbered here).

    Updates ``subscriber.status`` and flushes.
    """
    subscriber = db.get(Subscriber, subscriber_id)
    if not subscriber:
        raise ValueError(f"Subscriber {subscriber_id} not found")

    new_status = derive_account_status(db, subscriber_id)

    if subscriber.status != new_status:
        logger.info(
            "Account status derived: subscriber=%s %s → %s",
            subscriber_id,
            subscriber.status.value,
            new_status.value,
        )
        subscriber.status = new_status

    # Sync is_active with the account projection. A service-derived suspension
    # keeps portal access so the customer can view invoices and pay. An explicit
    # account suspension is an administrative access decision and disables the
    # account until that override is cleared.
    should_be_active = derive_account_active_projection(
        subscriber,
        account_status=new_status,
    )
    if subscriber.is_active != should_be_active:
        subscriber.is_active = should_be_active

    if new_status in {SubscriberStatus.disabled, SubscriberStatus.canceled}:
        clear_prepaid_enforcement_timers(db, subscriber_id)

    _stage_subscription_access_projection(db, subscriber)
    db.flush()

    return new_status


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


def get_active_locks(
    db: Session,
    *,
    subscription_id: str | None = None,
    subscriber_id: str | None = None,
) -> list[EnforcementLock]:
    """Query active enforcement locks for a subscription or subscriber."""
    stmt = select(EnforcementLock).where(EnforcementLock.is_active.is_(True))
    if subscription_id:
        stmt = stmt.where(EnforcementLock.subscription_id == subscription_id)
    if subscriber_id:
        stmt = stmt.where(EnforcementLock.subscriber_id == subscriber_id)
    return list(db.scalars(stmt).all())


def has_active_lock(
    db: Session,
    subscription_id: str,
    reason: EnforcementReason | None = None,
) -> bool:
    """Check if a subscription has any (or specific reason) active lock."""
    stmt = select(EnforcementLock.id).where(
        EnforcementLock.subscription_id == subscription_id,
        EnforcementLock.is_active.is_(True),
    )
    if reason is not None:
        stmt = stmt.where(EnforcementLock.reason == reason)
    return db.scalars(stmt).first() is not None


def reactivation_blocked_by_active_login(db: Session, subscription) -> bool:
    """True if reactivating ``subscription`` would collide with an existing
    active service on the same login.

    Enforces the ``uniq_subs_login_active_per_subscriber`` partial unique index
    (login, subscriber_id WHERE status='active' AND login IS NOT NULL): a
    suspended sub whose subscriber already holds another active sub on the same
    login is a superseded duplicate and must NOT be flipped back to active.
    Used by the lock-repair tools to skip such rows instead of hitting an
    IntegrityError mid-restore.
    """
    from app.services.subscription_lifecycle import (
        reactivation_blocked_by_active_login as lifecycle_restore_is_blocked,
    )

    return lifecycle_restore_is_blocked(db, subscription)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def resolve_locks_for_trigger(
    db: Session,
    subscription: Subscription,
    *,
    trigger: str,
    resolved_by: str,
    reason: EnforcementReason | None = None,
    notes: str | None = None,
    emit: bool = True,
) -> tuple[int, EnforcementLock | None]:
    """Resolve the active locks a trigger is authorized to clear.

    Returns a tuple of ``(resolved_count, remaining_active_lock)`` after the
    resolution attempt.
    """
    stmt = select(EnforcementLock).where(
        EnforcementLock.subscription_id == subscription.id,
        EnforcementLock.is_active.is_(True),
    )
    if reason is not None:
        stmt = stmt.where(EnforcementLock.reason == reason)

    active_locks = list(db.scalars(stmt).all())
    now = datetime.now(UTC)
    resolved_count = 0

    for lock in active_locks:
        allowed = ALLOWED_RESTORERS.get(lock.reason, set())
        if trigger not in allowed:
            logger.info(
                "Trigger %r not allowed to resolve %s lock (lock=%s)",
                trigger,
                lock.reason.value,
                lock.id,
            )
            continue

        lock.is_active = False
        lock.resolved_at = now
        lock.resolved_by = resolved_by
        if notes:
            existing_notes = lock.notes or ""
            lock.notes = (
                f"{existing_notes}\nResolved: {notes}" if existing_notes else notes
            )
        resolved_count += 1

        if emit:
            emit_event(
                db,
                EventType.enforcement_lock_resolved,
                {
                    "lock_id": str(lock.id),
                    "subscription_id": str(subscription.id),
                    "reason": lock.reason.value,
                    "trigger": trigger,
                    "resolved_by": resolved_by,
                },
                subscription_id=subscription.id,
                account_id=subscription.subscriber_id,
            )

    if resolved_count:
        db.flush()

    remaining = db.scalars(
        select(EnforcementLock).where(
            EnforcementLock.subscription_id == subscription.id,
            EnforcementLock.is_active.is_(True),
        )
    ).first()
    return resolved_count, remaining


def resolve_all_locks(db: Session, subscription: Subscription, resolved_by: str) -> int:
    """Resolve all active locks on a subscription (for terminal transitions).

    Used internally by ``expire_subscription`` and ``cancel_subscription``,
    and also by the catalog module when handling status transitions that
    have already been committed.
    """
    locks = list(
        db.scalars(
            select(EnforcementLock).where(
                EnforcementLock.subscription_id == subscription.id,
                EnforcementLock.is_active.is_(True),
            )
        ).all()
    )
    now = datetime.now(UTC)
    for lock in locks:
        lock.is_active = False
        lock.resolved_at = now
        lock.resolved_by = resolved_by
    if locks:
        db.flush()
    return len(locks)
