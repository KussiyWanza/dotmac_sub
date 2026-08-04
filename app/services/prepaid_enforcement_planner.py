"""Side-effect-free planning for prepaid balance enforcement.

The production sweep and the operator dry-run consume the same account decision
function.  Planning never writes timers, queues notices, changes service state,
or sends network commands.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session, aliased

from app.models.catalog import BillingMode, Subscription, SubscriptionBundle
from app.models.domain_settings import SettingDomain
from app.models.enforcement_lock import EnforcementLock, EnforcementReason
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services import enforcement_window, settings_spec
from app.services.access_resolution import (
    PrepaidFundingDecision,
    prepaid_enforcement_filters,
    resolve_prepaid_funding,
)
from app.services.billing_communication_policy import (
    billing_communication_decisions,
)
from app.services.billing_profile import resolve_billing_profile
from app.services.billing_settings import COLLECTIBLE_SERVICE_STATUSES
from app.services.billing_statuses import BILLABLE_SUBSCRIBER_STATUSES
from app.services.collections._core import _bulk_dunning_shield_reasons
from app.services.collections.grace_policy import (
    GracePolicySource,
    resolve_grace_decision,
)
from app.services.common import coerce_uuid
from app.services.domain_errors import DomainError
from app.services.prepaid_currency import resolve_prepaid_enforcement_currency


class PrepaidEnforcementAction(StrEnum):
    not_applicable = "not_applicable"
    billing_profile_invalid = "billing_profile_invalid"
    coverage_unresolved = "coverage_unresolved"
    renewal_terms_unresolved = "renewal_terms_unresolved"
    clear_stale_timers = "clear_stale_timers"
    repair_stale_locks = "repair_stale_locks"
    restore = "restore"
    warn = "warn"
    waiting = "waiting"
    deferred = "deferred"
    suspend = "suspend"
    shielded = "shielded"
    state_drift = "state_drift"
    already_suspended = "already_suspended"
    ok = "ok"


class PrepaidEnforcementReasonSource(StrEnum):
    OWNER = "owner"
    BILLING_PROFILE = "billing_profile"
    COVERAGE = "prepaid_service_coverage"
    RENEWAL = "prepaid_service_renewals"
    WINDOW = "enforcement_window"
    SHIELD = "financial_shield"


class PrepaidEnforcementError(DomainError):
    """Stable failure at the prepaid enforcement planning boundary."""


@dataclass(frozen=True, slots=True)
class PrepaidEnforcementPolicy:
    warning_subject: str
    warning_body: str
    deactivation_subject: str
    deactivation_body: str


@dataclass(frozen=True, slots=True)
class PrepaidEnforcementPlanItem:
    account_id: str
    action: PrepaidEnforcementAction
    reason: str
    reason_source: PrepaidEnforcementReasonSource
    account_status: SubscriberStatus
    derived_account_status: SubscriberStatus
    account_status_drift: bool
    billing_mode: BillingMode | None
    currency: str
    available_balance: Decimal
    required_balance: Decimal
    active_subscription_count: int
    suspended_subscription_count: int
    active_prepaid_lock_count: int
    covered_subscription_ids: tuple[UUID, ...]
    non_billable_subscription_ids: tuple[UUID, ...]
    actionable_uncovered_subscription_ids: tuple[UUID, ...]
    unresolved_projection_subscription_ids: tuple[UUID, ...]
    unresolved_renewal_subscription_ids: tuple[UUID, ...]
    prepaid_low_balance_at: datetime | None
    grace_days: int
    grace_source: GracePolicySource
    grace_policy_set_id: str | None
    deactivation_due_at: datetime | None
    prepaid_deactivation_at: datetime | None
    notice_suppression_reason: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "action": self.action.value,
            "reason": self.reason,
            "reason_source": self.reason_source.value,
            "account_status": self.account_status.value,
            "derived_account_status": self.derived_account_status.value,
            "account_status_drift": self.account_status_drift,
            "billing_mode": self.billing_mode.value if self.billing_mode else None,
            "currency": self.currency,
            "available_balance": self.available_balance,
            "required_balance": self.required_balance,
            "active_subscription_count": self.active_subscription_count,
            "suspended_subscription_count": self.suspended_subscription_count,
            "active_prepaid_lock_count": self.active_prepaid_lock_count,
            "covered_subscription_ids": [
                str(value) for value in self.covered_subscription_ids
            ],
            "non_billable_subscription_ids": [
                str(value) for value in self.non_billable_subscription_ids
            ],
            "actionable_uncovered_subscription_ids": [
                str(value) for value in self.actionable_uncovered_subscription_ids
            ],
            "unresolved_projection_subscription_ids": [
                str(value) for value in self.unresolved_projection_subscription_ids
            ],
            "unresolved_renewal_subscription_ids": [
                str(value) for value in self.unresolved_renewal_subscription_ids
            ],
            "prepaid_low_balance_at": self.prepaid_low_balance_at,
            "grace_days": self.grace_days,
            "grace_source": self.grace_source.value,
            "grace_policy_set_id": self.grace_policy_set_id,
            "deactivation_due_at": self.deactivation_due_at,
            "prepaid_deactivation_at": self.prepaid_deactivation_at,
            "notice_suppression_reason": self.notice_suppression_reason,
        }


@dataclass(frozen=True, slots=True)
class PrepaidEnforcementPlan:
    generated_at: datetime
    policy: PrepaidEnforcementPolicy
    funding_owner: str
    funding_observed_at: datetime
    items: tuple[PrepaidEnforcementPlanItem, ...]

    @property
    def action_counts(self) -> dict[str, int]:
        counts = Counter(item.action.value for item in self.items)
        return dict(sorted(counts.items()))

    def to_dict(self, *, include_items: bool = True) -> dict[str, object]:
        result: dict[str, object] = {
            "generated_at": self.generated_at,
            "funding_owner": self.funding_owner,
            "funding_observed_at": self.funding_observed_at,
            "accounts": len(self.items),
            "action_counts": self.action_counts,
            "account_status_drift": sum(
                1 for item in self.items if item.account_status_drift
            ),
            "notice_suppressed": sum(
                1 for item in self.items if item.notice_suppression_reason
            ),
        }
        if include_items:
            result["items"] = [item.to_dict() for item in self.items]
        return result


def resolve_prepaid_enforcement_policy(db: Session) -> PrepaidEnforcementPolicy:
    def _string(key: str) -> str:
        value = settings_spec.resolve_value(db, SettingDomain.collections, key)
        text = str(value).strip() if value is not None else ""
        if not text:
            raise PrepaidEnforcementError(
                code="financial.prepaid_enforcement.missing_policy_text",
                message="A prepaid enforcement communication policy value is missing.",
                details={"setting": f"collections.{key}"},
            )
        return text

    return PrepaidEnforcementPolicy(
        warning_subject=_string("prepaid_warning_subject"),
        warning_body=_string("prepaid_warning_body"),
        deactivation_subject=_string("prepaid_deactivation_subject"),
        deactivation_body=_string("prepaid_deactivation_body"),
    )


def candidate_prepaid_account_ids(db: Session) -> set[UUID]:
    """Canonical enforcement, repair, and restoration cohort.

    The shared access predicates own normal eligibility. Timers and active
    prepaid locks are unconditional repair inputs so a later billing-mode or
    status change cannot strand enforcement state outside the sweep.
    """
    ids: set[UUID] = {
        row[0]
        for row in (
            db.query(Subscriber.id)
            .join(Subscription, Subscription.subscriber_id == Subscriber.id)
            .filter(Subscription.status.in_(COLLECTIBLE_SERVICE_STATUSES))
            .filter(Subscriber.status.in_(BILLABLE_SUBSCRIBER_STATUSES))
            .filter(Subscriber.is_active.is_(True))
            .filter(Subscriber.billing_enabled.is_(True))
            .filter(
                or_(
                    Subscriber.billing_mode == BillingMode.prepaid,
                    Subscription.billing_mode == BillingMode.prepaid,
                )
            )
            .distinct()
            .all()
        )
    }
    ids.update(
        row[0]
        for row in (
            db.query(Subscriber.id)
            .filter(
                or_(
                    Subscriber.prepaid_low_balance_at.is_not(None),
                    Subscriber.prepaid_deactivation_at.is_not(None),
                )
            )
            .all()
        )
    )
    ids.update(
        row[0]
        for row in db.execute(
            select(Subscription.subscriber_id)
            .join(
                EnforcementLock,
                EnforcementLock.subscription_id == Subscription.id,
            )
            .where(
                EnforcementLock.reason == EnforcementReason.prepaid,
                EnforcementLock.is_active.is_(True),
            )
            .distinct()
        ).all()
    )
    return ids


def candidate_prepaid_funding_account_ids(db: Session) -> set[UUID]:
    """Return only accounts that may consume prepaid funding authority.

    ``candidate_prepaid_account_ids`` is intentionally broader because it also
    carries stale timer and lock repair inputs. Those rows must remain visible
    to the sweep, but a postpaid or service-less account must never receive a
    prepaid opening balance merely to clear stale enforcement state.
    """
    other_subscription = aliased(Subscription)
    other_collectible_mode = exists().where(
        other_subscription.subscriber_id == Subscriber.id,
        other_subscription.status.in_(COLLECTIBLE_SERVICE_STATUSES),
        other_subscription.billing_mode != BillingMode.prepaid,
    )
    return {
        row[0]
        for row in (
            db.query(Subscriber.id)
            .join(Subscription, Subscription.subscriber_id == Subscriber.id)
            .filter(*prepaid_enforcement_filters(Subscription, Subscriber))
            .filter(Subscriber.billing_mode == BillingMode.prepaid)
            .filter(~other_collectible_mode)
            .distinct()
            .all()
        )
    }


def prepaid_notice_suppression_reasons(
    db: Session, account_ids: Collection[UUID]
) -> dict[UUID, str]:
    """Map accounts whose billing notices are fault-suppressed to the reason."""
    ids = set(account_ids)
    if not ids:
        return {}
    subscriptions = list(
        db.scalars(
            select(Subscription).where(Subscription.subscriber_id.in_(ids))
        ).all()
    )
    decisions = billing_communication_decisions(db, subscriptions)
    reasons: dict[UUID, str] = {}
    for subscription in subscriptions:
        decision = decisions.get(subscription.id)
        if decision is None or not decision.suppress_suspension_notice:
            continue
        if decision.reason:
            reasons.setdefault(subscription.subscriber_id, decision.reason)
    return reasons


def _dedicated_bundle_account_ids(
    db: Session, account_ids: Sequence[UUID]
) -> set[UUID]:
    if not account_ids:
        return set()
    return {
        row[0]
        for row in db.execute(
            select(Subscription.subscriber_id)
            .join(
                SubscriptionBundle,
                Subscription.bundle_id == SubscriptionBundle.id,
            )
            .where(
                Subscription.subscriber_id.in_(account_ids),
                SubscriptionBundle.is_dedicated.is_(True),
            )
            .distinct()
        ).all()
    }


def _prepaid_lock_counts(db: Session, account_ids: Sequence[UUID]) -> dict[UUID, int]:
    if not account_ids:
        return {}
    return {
        account_id: int(count)
        for account_id, count in db.execute(
            select(Subscription.subscriber_id, func.count(EnforcementLock.id))
            .join(
                EnforcementLock,
                EnforcementLock.subscription_id == Subscription.id,
            )
            .where(
                Subscription.subscriber_id.in_(account_ids),
                EnforcementLock.reason == EnforcementReason.prepaid,
                EnforcementLock.is_active.is_(True),
            )
            .group_by(Subscription.subscriber_id)
        ).all()
    }


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _window_block_reason(
    db: Session,
    *,
    now: datetime,
) -> str | None:
    return enforcement_window.resolve_enforcement_window_decision(db, now).block_reason


def plan_prepaid_account(
    db: Session,
    account: Subscriber,
    *,
    now: datetime,
    policy: PrepaidEnforcementPolicy,
    subscriptions: list[Subscription] | None = None,
    funding: PrepaidFundingDecision | None = None,
    active_prepaid_lock_count: int | None = None,
    dedicated_bundle: bool | None = None,
    shield_reason: str | None = None,
    shield_evaluated: bool = False,
    notice_suppression_reason: str | None = None,
    window_block_reason: str | None = None,
    window_evaluated: bool = False,
    include_derived_status: bool = True,
) -> PrepaidEnforcementPlanItem:
    """Classify one account without mutating it.

    ``window_block_reason``/``window_evaluated`` let a batched caller resolve
    the run-constant enforcement window once. ``include_derived_status``
    disables the report-only derived-status comparison (it never gates an
    action) for callers that cannot afford its financial-event replay per
    account.
    """
    from app.services.account_lifecycle import derive_account_status

    account_subscriptions = subscriptions
    if account_subscriptions is None:
        account_subscriptions = list(
            db.scalars(
                select(Subscription).where(Subscription.subscriber_id == account.id)
            ).all()
        )
    active_count = sum(
        1 for sub in account_subscriptions if sub.status.value == "active"
    )
    suspended_count = sum(
        1
        for sub in account_subscriptions
        if sub.status.value in {"blocked", "suspended"}
    )
    if active_prepaid_lock_count is None:
        active_prepaid_lock_count = int(
            db.scalar(
                select(func.count(EnforcementLock.id))
                .join(
                    Subscription,
                    Subscription.id == EnforcementLock.subscription_id,
                )
                .where(
                    Subscription.subscriber_id == account.id,
                    EnforcementLock.reason == EnforcementReason.prepaid,
                    EnforcementLock.is_active.is_(True),
                )
            )
            or 0
        )

    profile = resolve_billing_profile(db, account)
    funding_required = (
        account.status != SubscriberStatus.canceled
        and account.is_active
        and account.billing_enabled
        and profile.automation_safe
        and profile.effective_mode == BillingMode.prepaid
        and profile.has_collectible_subscriptions
    )
    if funding is None:
        if funding_required:
            funding = resolve_prepaid_funding(db, account, now=now)
        else:
            # Repair-only rows never consume funding authority. A neutral
            # decision preserves the report shape and fails funded if a future
            # branch accidentally reaches a money comparison.
            funding = PrepaidFundingDecision(
                account_id=str(account.id),
                available_balance=Decimal("0.00"),
                required_balance=Decimal("0.00"),
                currency=resolve_prepaid_enforcement_currency(db),
            )
    balance = funding.available_balance
    threshold = funding.required_balance
    current_status = account.status or SubscriberStatus.new
    derived_status = (
        derive_account_status(db, str(account.id))
        if include_derived_status
        else current_status
    )
    low_at = (
        _as_utc(account.prepaid_low_balance_at)
        if account.prepaid_low_balance_at is not None
        else None
    )
    grace = resolve_grace_decision(
        db,
        account,
        starts_at=low_at,
        as_of=now,
    )
    zero_grace = grace.policy.days == 0
    due_at = (low_at or now) if zero_grace else grace.ends_at

    action = PrepaidEnforcementAction.ok
    reason = "funded_and_aligned"
    reason_source = PrepaidEnforcementReasonSource.OWNER
    has_timers = (
        account.prepaid_low_balance_at is not None
        or account.prepaid_deactivation_at is not None
    )
    if (
        has_timers
        and (
            account.status == SubscriberStatus.canceled
            or not account.is_active
            or not account.billing_enabled
        )
        and active_prepaid_lock_count == 0
    ):
        action = PrepaidEnforcementAction.clear_stale_timers
        reason = "ineligible_account_has_prepaid_timers"
    elif account.status == SubscriberStatus.canceled:
        action = PrepaidEnforcementAction.not_applicable
        reason = "account_canceled"
    elif not account.is_active:
        action = PrepaidEnforcementAction.not_applicable
        reason = "account_inactive"
    elif not account.billing_enabled:
        action = PrepaidEnforcementAction.not_applicable
        reason = "account_billing_disabled"
    elif not profile.has_collectible_subscriptions:
        if active_prepaid_lock_count > 0:
            action = PrepaidEnforcementAction.repair_stale_locks
            reason = "prepaid_lock_without_collectible_service"
        elif has_timers:
            action = PrepaidEnforcementAction.clear_stale_timers
            reason = "account_without_collectible_service_has_prepaid_timers"
        else:
            action = PrepaidEnforcementAction.not_applicable
            reason = "account_without_collectible_service"
    elif not profile.automation_safe and profile.has_collectible_subscriptions:
        action = PrepaidEnforcementAction.billing_profile_invalid
        reason = (
            profile.invalid_reason.value
            if profile.invalid_reason
            else "billing_profile_not_automation_safe"
        )
        reason_source = PrepaidEnforcementReasonSource.BILLING_PROFILE
    elif profile.effective_mode != BillingMode.prepaid:
        if active_prepaid_lock_count > 0:
            action = PrepaidEnforcementAction.repair_stale_locks
            reason = "non_prepaid_account_has_prepaid_lock"
        elif (
            account.prepaid_low_balance_at is not None
            or account.prepaid_deactivation_at is not None
        ):
            action = PrepaidEnforcementAction.clear_stale_timers
            reason = "non_prepaid_account_has_prepaid_timers"
        else:
            action = PrepaidEnforcementAction.not_applicable
            reason = "effective_billing_mode_not_prepaid"
    elif funding.unresolved_projection_subscription_ids:
        action = PrepaidEnforcementAction.coverage_unresolved
        reason = "prepaid_coverage_evidence_requires_reconciliation"
        reason_source = PrepaidEnforcementReasonSource.COVERAGE
    elif funding.unresolved_renewal_subscription_ids:
        action = PrepaidEnforcementAction.renewal_terms_unresolved
        reason = "contracted_prepaid_renewal_terms_unavailable"
        reason_source = PrepaidEnforcementReasonSource.RENEWAL
    elif funding.funded:
        if (
            account.prepaid_low_balance_at is not None
            or account.prepaid_deactivation_at is not None
            or active_prepaid_lock_count > 0
        ):
            action = PrepaidEnforcementAction.restore
            reason = "funding_threshold_reached"
    elif active_prepaid_lock_count > 0 and active_count > 0:
        action = PrepaidEnforcementAction.state_drift
        reason = "active_subscription_has_prepaid_lock"
    elif active_prepaid_lock_count > 0 and account.prepaid_deactivation_at is None:
        action = PrepaidEnforcementAction.state_drift
        reason = "prepaid_lock_missing_deactivation_marker"
    elif account.prepaid_deactivation_at is not None and active_prepaid_lock_count == 0:
        action = PrepaidEnforcementAction.state_drift
        reason = "deactivation_marker_missing_prepaid_lock"
    elif active_prepaid_lock_count > 0:
        action = PrepaidEnforcementAction.already_suspended
        reason = "prepaid_lock_and_deactivation_aligned"
    elif account.prepaid_low_balance_at is None and not zero_grace:
        action = PrepaidEnforcementAction.warn
        reason = "low_balance_timer_not_armed"
    elif not zero_grace and grace.phase != "actionable":
        action = PrepaidEnforcementAction.waiting
        reason = "deactivation_grace_not_elapsed"
    elif window_reason := (
        window_block_reason if window_evaluated else _window_block_reason(db, now=now)
    ):
        action = PrepaidEnforcementAction.deferred
        reason = window_reason
        reason_source = PrepaidEnforcementReasonSource.WINDOW
    else:
        if dedicated_bundle is None:
            dedicated_bundle = account.id in _dedicated_bundle_account_ids(
                db, [account.id]
            )
        if dedicated_bundle:
            action = PrepaidEnforcementAction.shielded
            reason = "dedicated_bundle"
        else:
            if not shield_evaluated:
                shield_reason = _bulk_dunning_shield_reasons(db, {account.id}).get(
                    account.id
                )
            if shield_reason:
                action = PrepaidEnforcementAction.shielded
                reason = shield_reason
                reason_source = PrepaidEnforcementReasonSource.SHIELD
            else:
                action = PrepaidEnforcementAction.suspend
                reason = "low_balance_deactivation_due"

    return PrepaidEnforcementPlanItem(
        account_id=str(account.id),
        action=action,
        reason=reason,
        reason_source=reason_source,
        account_status=current_status,
        derived_account_status=derived_status,
        account_status_drift=current_status != derived_status,
        billing_mode=profile.effective_mode,
        currency=funding.currency,
        available_balance=balance,
        required_balance=threshold,
        active_subscription_count=active_count,
        suspended_subscription_count=suspended_count,
        active_prepaid_lock_count=active_prepaid_lock_count,
        covered_subscription_ids=funding.covered_subscription_ids,
        non_billable_subscription_ids=funding.non_billable_subscription_ids,
        actionable_uncovered_subscription_ids=(
            funding.actionable_uncovered_subscription_ids
        ),
        unresolved_projection_subscription_ids=(
            funding.unresolved_projection_subscription_ids
        ),
        unresolved_renewal_subscription_ids=(
            funding.unresolved_renewal_subscription_ids
        ),
        prepaid_low_balance_at=account.prepaid_low_balance_at,
        grace_days=grace.policy.days,
        grace_source=grace.policy.source,
        grace_policy_set_id=(
            str(grace.policy.policy_set_id) if grace.policy.policy_set_id else None
        ),
        deactivation_due_at=due_at,
        prepaid_deactivation_at=account.prepaid_deactivation_at,
        notice_suppression_reason=notice_suppression_reason,
    )


def plan_prepaid_enforcement(
    db: Session,
    *,
    now: datetime | None = None,
    account_ids: Sequence[UUID | str] | None = None,
    limit: int | None = None,
) -> PrepaidEnforcementPlan:
    """Build a deterministic, side-effect-free enforcement report."""
    generated_at = _as_utc(now) if now is not None else datetime.now(UTC)
    raw_ids = (
        list(account_ids)
        if account_ids is not None
        else list(candidate_prepaid_account_ids(db))
    )
    try:
        ids = sorted({coerce_uuid(str(value)) for value in raw_ids}, key=str)
    except (TypeError, ValueError) as exc:
        raise PrepaidEnforcementError(
            code="financial.prepaid_enforcement.invalid_account_id",
            message="A selected prepaid enforcement account identifier is invalid.",
        ) from exc
    from app.services.prepaid_funding_reconstruction import (
        prepaid_funding_incomplete_source_account_ids,
    )

    funding_candidate_ids = candidate_prepaid_funding_account_ids(db)
    incomplete_source_ids = prepaid_funding_incomplete_source_account_ids(
        db,
        [account_id for account_id in ids if account_id in funding_candidate_ids],
    )
    ids = [account_id for account_id in ids if account_id not in incomplete_source_ids]
    if limit is not None:
        ids = ids[: max(0, limit)]
    accounts = list(
        db.scalars(
            select(Subscriber).where(Subscriber.id.in_(ids)).order_by(Subscriber.id)
        ).all()
    )
    resolved_account_ids = {account.id for account in accounts}
    unresolved = [
        str(account_id) for account_id in ids if account_id not in resolved_account_ids
    ]
    if unresolved:
        raise PrepaidEnforcementError(
            code="financial.prepaid_enforcement.account_not_found",
            message="A selected prepaid enforcement account was not found.",
            details={"account_ids": unresolved},
        )
    resolved_ids = [account.id for account in accounts]
    subscriptions = list(
        db.scalars(
            select(Subscription).where(Subscription.subscriber_id.in_(resolved_ids))
        ).all()
    )
    subscriptions_by_account: dict[UUID, list[Subscription]] = defaultdict(list)
    for subscription in subscriptions:
        subscriptions_by_account[subscription.subscriber_id].append(subscription)

    policy = resolve_prepaid_enforcement_policy(db)
    funding_by_account = {
        account.id: resolve_prepaid_funding(db, account, now=generated_at)
        for account in accounts
        if account.id in funding_candidate_ids
    }
    lock_counts = _prepaid_lock_counts(db, resolved_ids)
    dedicated_accounts = _dedicated_bundle_account_ids(db, resolved_ids)
    shield_reasons = _bulk_dunning_shield_reasons(db, set(resolved_ids))
    notice_reasons = prepaid_notice_suppression_reasons(db, resolved_ids)
    window_reason = _window_block_reason(db, now=generated_at)

    items = tuple(
        plan_prepaid_account(
            db,
            account,
            now=generated_at,
            policy=policy,
            subscriptions=subscriptions_by_account.get(account.id, []),
            funding=funding_by_account.get(account.id),
            active_prepaid_lock_count=lock_counts.get(account.id, 0),
            dedicated_bundle=account.id in dedicated_accounts,
            shield_reason=shield_reasons.get(account.id),
            shield_evaluated=True,
            notice_suppression_reason=notice_reasons.get(account.id),
            window_block_reason=window_reason,
            window_evaluated=True,
        )
        for account in accounts
    )
    return PrepaidEnforcementPlan(
        generated_at=generated_at,
        policy=policy,
        funding_owner="financial.prepaid_funding_reconstruction",
        funding_observed_at=generated_at,
        items=items,
    )
