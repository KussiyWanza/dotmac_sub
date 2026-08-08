"""FUP enforcement decisions and per-subscription command coordination."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypeVar
from uuid import UUID

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.domain_settings import SettingDomain
from app.models.fup import FupPolicy
from app.models.fup_state import FupActionStatus, FupState
from app.models.usage import QuotaBucket
from app.services import control_registry, settings_spec
from app.services import fup_state as fup_state_service
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.events import EventType, emit_event
from app.services.fup_state import ApplyFupRuntimeState
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

logger = logging.getLogger(__name__)
ResultT = TypeVar("ResultT")


class FupEnforcementError(DomainError):
    """Stable failures from the FUP enforcement coordinator."""


def _error(suffix: str, message: str) -> FupEnforcementError:
    return FupEnforcementError(
        code=f"access.fup_enforcement_sweep.{suffix}",
        message=message,
    )


def _definition(name: str) -> OwnerCommandDefinition:
    return OwnerCommandDefinition(
        owner="access.fup_enforcement_sweep",
        concern="FUP sweep enforce/warn/reset decisions",
        name=name,
    )


def _execute(
    db: Session,
    *,
    context: CommandContext,
    name: str,
    operation: Callable[[], ResultT],
) -> ResultT:
    return execute_owner_command(
        db,
        definition=_definition(name),
        context=context,
        operation=operation,
    )


@dataclass(frozen=True, slots=True)
class RunFupSweepRequest:
    correlation_id: UUID
    source: str = "scheduled_full_sweep"
    subscription_ids: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class RunExpiredFupLiftRequest:
    correlation_id: UUID
    source: str = "scheduled_expired_lift"


@dataclass(frozen=True, slots=True)
class EvaluateFupSubscriptionCommand:
    context: CommandContext
    subscription_id: UUID
    evaluated_at: datetime
    warning_enabled: bool
    warning_ratio: float
    throttle_profile_configured: bool


@dataclass(frozen=True, slots=True)
class LiftExpiredFupSubscriptionCommand:
    context: CommandContext
    subscription_id: UUID
    evaluated_at: datetime


@dataclass(frozen=True, slots=True)
class FupSubscriptionOutcome:
    processed: int = 1
    enforced: int = 0
    reset: int = 0
    notified: int = 0
    submonthly_no_data: int = 0
    throttle_unconfigured: int = 0


def stage_fup_runtime_state(
    db: Session,
    command: ApplyFupRuntimeState,
) -> FupState:
    """Delegate one typed state projection inside the enforcement transaction."""
    return fup_state_service.fup_state.apply_action(db, command)


def _fup_should_enforce(
    *,
    prior_status: str,
    target_status: str,
    cooldown_minutes: int,
    state: FupState | None,
    now: datetime,
) -> bool:
    """Enforce on transition, or reassert only after a positive cooldown."""
    if prior_status != target_status:
        return True
    if cooldown_minutes and state is not None and state.last_evaluated_at is not None:
        last = state.last_evaluated_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        return (now - last).total_seconds() / 60 >= cooldown_minutes
    return False


def _validate_source(source: str) -> str:
    normalized = source.strip()
    if not normalized or len(normalized) > 120:
        raise _error("invalid_source", "FUP sweep source is invalid.")
    return normalized


def _command_context(
    *,
    correlation_id: UUID,
    subscription_id: UUID,
    source: str,
) -> CommandContext:
    return CommandContext.system(
        actor="system:fup_enforcement",
        scope=str(subscription_id),
        reason=source,
        correlation_id=correlation_id,
    )


def _release_read_transaction(db: Session) -> None:
    # Mock-only unit adapters are not SQLAlchemy sessions. Production always
    # closes the discovery read before entering the first owner command.
    if isinstance(db, Session):
        db_session_adapter.release_read_transaction(db)


def _candidate_subscription_ids(
    db: Session,
    subscription_ids: tuple[str, ...] | None,
    *,
    source: str,
) -> list[UUID]:
    subscription_uuid_filter: list[UUID] | None = None
    if subscription_ids is not None:
        subscription_uuid_filter = []
        for raw_id in subscription_ids:
            try:
                subscription_uuid_filter.append(UUID(str(raw_id)))
            except (TypeError, ValueError):
                logger.warning(
                    "Skipping invalid FUP subscription id %r from %s",
                    raw_id,
                    source,
                )
        if not subscription_uuid_filter:
            return []

    enforced_states = (
        FupActionStatus.notified,
        FupActionStatus.throttled,
        FupActionStatus.blocked,
    )
    query = (
        db.query(Subscription)
        .join(FupPolicy, FupPolicy.offer_id == Subscription.offer_id)
        .outerjoin(FupState, FupState.subscription_id == Subscription.id)
        .filter(
            or_(
                Subscription.status == SubscriptionStatus.active,
                FupState.action_status.in_(enforced_states),
            )
        )
        .filter(FupPolicy.is_active.is_(True))
    )
    if subscription_uuid_filter is not None:
        query = query.filter(Subscription.id.in_(subscription_uuid_filter))
    return [row.id for row in query.all()]


def _subscription_for_evaluation(db: Session, subscription_id: UUID) -> Subscription:
    subscription = (
        db.query(Subscription)
        .filter(Subscription.id == subscription_id)
        .with_for_update(of=Subscription)
        .one_or_none()
    )
    if subscription is None:
        raise _error("subscription_not_found", "Subscription was not found.")
    return subscription


def _current_quota_bucket(
    db: Session,
    subscription_id: UUID,
    evaluated_at: datetime,
) -> QuotaBucket | None:
    """Read the metering-owned bucket; enforcement never creates usage facts."""
    return (
        db.query(QuotaBucket)
        .filter(QuotaBucket.subscription_id == subscription_id)
        .filter(QuotaBucket.period_start <= evaluated_at)
        .filter(QuotaBucket.period_end > evaluated_at)
        .order_by(QuotaBucket.period_start.desc())
        .first()
    )


def _requires_quota_bucket(db: Session, offer_id) -> bool:
    """Does this offer have any rule that measures against the billing bucket?

    Only ``monthly`` rules do. Treating the bucket as universally required
    conflated "we cannot measure this rule" with "this subscription is not
    metered for billing".
    """
    from app.services.fup import FupPolicies
    from app.services.fup_usage import period_value

    policy = FupPolicies.get_by_offer(db, str(offer_id))
    if policy is None:
        return True
    active = [rule for rule in policy.rules if rule.is_active]
    if not active:
        return True
    return any(period_value(rule.consumption_period) == "monthly" for rule in active)


def _sweep_policy(db: Session) -> tuple[bool, float, bool]:
    from app.services.usage import _parse_warning_thresholds

    warning_enabled = control_registry.is_enabled(db, "usage.warnings")
    raw_thresholds = settings_spec.resolve_value(
        db, SettingDomain.usage, "usage_warning_thresholds"
    )
    parsed = _parse_warning_thresholds(
        str(raw_thresholds) if raw_thresholds is not None else None
    )
    warning_ratio = float(parsed[0]) if parsed else 0.8
    throttle_profile_configured = bool(
        settings_spec.resolve_value(
            db,
            SettingDomain.usage,
            "fup_throttle_radius_profile_id",
        )
    )
    return warning_enabled, warning_ratio, throttle_profile_configured


def _emit_enforcement_event(
    db: Session,
    subscription: Subscription,
    rule_result: dict,
    *,
    current_usage: float,
    cap_resets_at: object,
) -> None:
    emit_event(
        db,
        EventType.usage_exhausted,
        {
            "schema_version": 1,
            "subscription_id": str(subscription.id),
            "offer_id": str(subscription.offer_id),
            "rule_id": rule_result.get("rule_id"),
            "action": rule_result.get("action"),
            "current_usage_gb": current_usage,
            "threshold_gb": rule_result.get("threshold_gb"),
            "cap_resets_at": cap_resets_at,
        },
        subscription_id=subscription.id,
        account_id=subscription.subscriber_id,
    )


def _evaluate_subscription(
    db: Session,
    command: EvaluateFupSubscriptionCommand,
) -> FupSubscriptionOutcome:
    from app.services.fup import evaluate_rules
    from app.services.fup_usage import build_usage_by_period

    subscription = _subscription_for_evaluation(db, command.subscription_id)
    state = fup_state_service.fup_state.get_for_update(db, subscription.id)
    if state and state.cap_resets_at and command.evaluated_at >= state.cap_resets_at:
        from app.services.enforcement import lift_fup_enforcement

        result = lift_fup_enforcement(
            db,
            str(subscription.id),
            evaluated_at=command.evaluated_at,
        )
        return FupSubscriptionOutcome(reset=int(bool(result.get("lifted"))))

    bucket = _current_quota_bucket(db, subscription.id, command.evaluated_at)
    if bucket is None and _requires_quota_bucket(db, subscription.offer_id):
        # A monthly rule measures against the billing bucket, so without one
        # there is nothing to compare and skipping is correct.
        return FupSubscriptionOutcome()
    if bucket is None:
        # Sub-monthly rules read the windowed usage reader instead, and quota
        # buckets only exist for offers carrying a usage allowance
        # (usage.meter_active_subscriptions). home_flex deliberately has none —
        # the allowance is a BILLING object and FUP is enforcement (§1) — so
        # requiring a bucket here silently disabled every daily ladder on the
        # family that most needs one, however carefully it was configured.
        logger.debug(
            "FUP evaluating sub %s with no quota bucket; sub-monthly rules only",
            subscription.id,
        )
    current_usage = float(bucket.used_gb or 0) if bucket is not None else 0.0
    usage_by_period = build_usage_by_period(
        db,
        subscription,
        str(subscription.offer_id),
        command.evaluated_at,
        current_usage,
    )
    prior_status = state.action_status.value if state else "none"
    # Time-of-day windows are wall-clock facts about the customer's night, and
    # the daily consumption windows above are already aligned to this same
    # local midnight. Passing it keeps both halves of a "free night" on one
    # clock.
    from app.services.usage_summary import _subscriber_tz

    subscriber_tz = _subscriber_tz(db, str(subscription.subscriber_id))
    results = evaluate_rules(
        db,
        str(subscription.offer_id),
        current_usage_gb=current_usage,
        current_time=command.evaluated_at,
        usage_by_period=usage_by_period,
        tz=subscriber_tz,
    )
    # A free overnight window has to LIFT the throttle, not merely stop
    # re-applying it. cap_resets_at (above) is the period boundary — for a
    # daily rule, local midnight — so without this a rule whose window closes
    # at 22:00 would leave the subscriber throttled for the first two hours of
    # the night it was told were free.
    #
    # Deliberately narrow: it releases only when the rule that CAUSED the
    # current enforcement is itself outside its time window right now. It does
    # not release because a threshold stopped being met (that is what
    # cap_resets_at owns) and it cannot release on a blind usage reading,
    # because a time_skip is decided from the clock alone.
    if state is not None and state.active_rule_id is not None:
        active_rule_id = str(state.active_rule_id)
        outside_window = any(
            row.get("rule_id") == active_rule_id and row.get("status") == "time_skip"
            for row in results
        )
        if outside_window and prior_status in {"throttled", "blocked"}:
            from app.services.enforcement import lift_fup_enforcement

            result = lift_fup_enforcement(
                db,
                str(subscription.id),
                evaluated_at=command.evaluated_at,
            )
            logger.info(
                "FUP enforcement lifted for sub %s: rule %s is outside its time window",
                subscription.id,
                active_rule_id,
            )
            return FupSubscriptionOutcome(reset=int(bool(result.get("lifted"))))

    no_data_count = sum(1 for row in results if row.get("usage_source") == "no_data")
    for row in results:
        if row.get("usage_source") == "no_data":
            logger.warning(
                "FUP %s window for sub %s rule %s had no usage data; not enforced",
                row.get("consumption_period"),
                subscription.id,
                row.get("rule_id"),
            )

    pending_notifications: list[dict] = []
    enforced = 0
    throttle_unconfigured = 0
    triggered = [row for row in results if row.get("triggered")]
    if triggered:
        for rule_result in reversed(triggered):
            # A sub-monthly rule carries its own window_end; the billing period
            # is only the fallback, and there may be no bucket at all.
            cap_resets_at = rule_result.get("window_end") or (
                bucket.period_end.isoformat()
                if bucket is not None and bucket.period_end
                else None
            )
            # Report the usage of the window this rule was MEASURED against,
            # not the monthly bucket. A daily rule breaches on its own window,
            # and `current_usage` is the monthly figure — 0.0 for the families
            # that carry no quota bucket at all (home_flex). Using it here made
            # a real daily breach tell the customer, the event stream, and the
            # audit trail "0 GB exhausted". evaluate_rules already resolved the
            # right number per rule and falls back to the monthly value for
            # monthly rules, so this is correct for every consumption period.
            #
            # A measured 0.0 is valid evidence; a MISSING measurement is not.
            # `or 0.0` conflated them, which is the same substitution of a
            # plausible zero for an unknown that this block exists to fix.
            # evaluate_rules sets the key on every triggered row, so absence is
            # a contract breach — fail closed rather than enforce blind.
            raw_rule_usage = rule_result.get("current_usage_gb")
            if raw_rule_usage is None:
                logger.error(
                    "FUP rule %s triggered for sub %s with no measured usage; "
                    "not enforcing on unknown evidence",
                    rule_result.get("rule_id"),
                    subscription.id,
                )
                continue
            rule_usage = float(raw_rule_usage)
            if rule_result.get("usage_source") == "no_data":
                continue
            action = rule_result.get("action")
            if action == "block":
                if _fup_should_enforce(
                    prior_status=prior_status,
                    target_status="blocked",
                    cooldown_minutes=rule_result.get("cooldown_minutes") or 0,
                    state=state,
                    now=command.evaluated_at,
                ):
                    _emit_enforcement_event(
                        db,
                        subscription,
                        rule_result,
                        current_usage=rule_usage,
                        cap_resets_at=cap_resets_at,
                    )
                    enforced += 1
                if prior_status != "blocked":
                    pending_notifications.append(
                        {
                            "subscriber_id": subscription.subscriber_id,
                            "kind": "blocked",
                            "rule_name": rule_result.get("name"),
                            "threshold_gb": rule_result.get("threshold_gb"),
                            "used_gb": rule_usage,
                            "cap_resets_at": cap_resets_at,
                        }
                    )
                _maybe_queue_repeat_upsell(
                    db,
                    subscription,
                    bucket,
                    rule_result,
                    pending_notifications,
                )
                break
            if action == "reduce_speed":
                if not command.throttle_profile_configured:
                    # Observability only. The throttle a subscriber gets is
                    # DERIVED from their own rate, so a missing global profile
                    # is a missing fallback, not a missing throttle. Refusing
                    # here meant a deployment that never set the global setting
                    # enforced nothing at all, however carefully its ladders
                    # were configured; the handler now raises if derivation
                    # fails and there is no fallback either.
                    throttle_unconfigured += 1
                    logger.info(
                        "FUP reduce_speed for sub %s rule %s has no global "
                        "fallback profile; relying on the derived profile",
                        subscription.id,
                        rule_result.get("rule_id"),
                    )
                if _fup_should_enforce(
                    prior_status=prior_status,
                    target_status="throttled",
                    cooldown_minutes=rule_result.get("cooldown_minutes") or 0,
                    state=state,
                    now=command.evaluated_at,
                ):
                    _emit_enforcement_event(
                        db,
                        subscription,
                        rule_result,
                        current_usage=rule_usage,
                        cap_resets_at=cap_resets_at,
                    )
                    enforced += 1
                if prior_status != "throttled":
                    pending_notifications.append(
                        {
                            "subscriber_id": subscription.subscriber_id,
                            "kind": "throttled",
                            "rule_name": rule_result.get("name"),
                            "threshold_gb": rule_result.get("threshold_gb"),
                            "used_gb": rule_usage,
                            "cap_resets_at": cap_resets_at,
                        }
                    )
                _maybe_queue_repeat_upsell(
                    db,
                    subscription,
                    bucket,
                    rule_result,
                    pending_notifications,
                )
                break
            if action == "notify" and prior_status == "none":
                rule_id = rule_result.get("rule_id")
                stage_fup_runtime_state(
                    db,
                    ApplyFupRuntimeState(
                        subscription_id=subscription.id,
                        offer_id=subscription.offer_id,
                        rule_id=UUID(str(rule_id)) if rule_id else None,
                        action_status=FupActionStatus.notified,
                        evaluated_at=command.evaluated_at,
                        notes="FUP notification threshold reached",
                    ),
                )
                pending_notifications.append(
                    {
                        "subscriber_id": subscription.subscriber_id,
                        "kind": "notified",
                        "rule_name": rule_result.get("name"),
                        "threshold_gb": rule_result.get("threshold_gb"),
                        "used_gb": rule_usage,
                    }
                )
                break
    elif prior_status == "none" and command.warning_enabled:
        # Each rule's own window usage, not the monthly bucket. evaluate_rules
        # already divides rule_usage_gb by the threshold for exactly this
        # comparison; using current_usage instead measured a daily rule against
        # a month of traffic, so a daily ladder either never warned or warned
        # on the wrong day. Rows with no measured window (usage_percent None)
        # are skipped rather than treated as zero.
        ratios = [
            (row["usage_percent"] / 100.0, row)
            for row in results
            if row.get("threshold_gb") and row.get("usage_percent") is not None
        ]
        if ratios:
            ratio, nearest_rule = max(ratios, key=lambda item: item[0])
            if command.warning_ratio <= ratio < 1.0:
                rule_id = nearest_rule.get("rule_id")
                stage_fup_runtime_state(
                    db,
                    ApplyFupRuntimeState(
                        subscription_id=subscription.id,
                        offer_id=subscription.offer_id,
                        rule_id=UUID(str(rule_id)) if rule_id else None,
                        action_status=FupActionStatus.notified,
                        evaluated_at=command.evaluated_at,
                        notes="approaching fup limit",
                    ),
                )
                pending_notifications.append(
                    {
                        "subscriber_id": subscription.subscriber_id,
                        "kind": "approaching",
                        "rule_name": nearest_rule.get("name"),
                        "threshold_gb": nearest_rule.get("threshold_gb"),
                        # The ratio above is measured on this rule's own window,
                        # so the GB figure quoted to the customer has to come
                        # from the same window or the warning contradicts
                        # itself. The row was selected BY usage_percent, so a
                        # measured value is guaranteed present here.
                        "used_gb": float(nearest_rule["current_usage_gb"]),
                    }
                )

    notified = _emit_fup_notifications(db, pending_notifications)
    return FupSubscriptionOutcome(
        enforced=enforced,
        notified=notified,
        submonthly_no_data=no_data_count,
        throttle_unconfigured=throttle_unconfigured,
    )


def evaluate_fup_subscription(
    db: Session,
    command: EvaluateFupSubscriptionCommand,
) -> FupSubscriptionOutcome:
    return _execute(
        db,
        context=command.context,
        name="evaluate_fup_subscription",
        operation=lambda: _evaluate_subscription(db, command),
    )


def run_fup_evaluation(
    db: Session,
    request: RunFupSweepRequest,
) -> dict[str, int]:
    """Discover candidates, then commit one complete owner command per subscription."""
    source = _validate_source(request.source)
    totals = FupSubscriptionOutcome(processed=0)
    now = datetime.now(UTC)
    warning_enabled, warning_ratio, throttle_configured = _sweep_policy(db)
    candidate_ids = _candidate_subscription_ids(
        db,
        request.subscription_ids,
        source=source,
    )
    _release_read_transaction(db)
    for subscription_id in candidate_ids:
        outcome = evaluate_fup_subscription(
            db,
            EvaluateFupSubscriptionCommand(
                context=_command_context(
                    correlation_id=request.correlation_id,
                    subscription_id=subscription_id,
                    source=source,
                ),
                subscription_id=subscription_id,
                evaluated_at=now,
                warning_enabled=warning_enabled,
                warning_ratio=warning_ratio,
                throttle_profile_configured=throttle_configured,
            ),
        )
        totals = FupSubscriptionOutcome(
            processed=totals.processed + outcome.processed,
            enforced=totals.enforced + outcome.enforced,
            reset=totals.reset + outcome.reset,
            notified=totals.notified + outcome.notified,
            submonthly_no_data=totals.submonthly_no_data + outcome.submonthly_no_data,
            throttle_unconfigured=(
                totals.throttle_unconfigured + outcome.throttle_unconfigured
            ),
        )
    result = {
        "processed": totals.processed,
        "enforced": totals.enforced,
        "reset": totals.reset,
        "notified": totals.notified,
        "submonthly_no_data": totals.submonthly_no_data,
        "throttle_unconfigured": totals.throttle_unconfigured,
        "targeted": int(request.subscription_ids is not None),
    }
    logger.info("FUP evaluation complete source=%s result=%s", source, result)
    return result


def _hit_fup_in_window(
    session: Session,
    subscriber_id: object,
    start: datetime,
    end: datetime,
) -> bool:
    from app.models.notification import Notification

    return (
        session.query(Notification.id)
        .filter(Notification.subscriber_id == subscriber_id)
        .filter(Notification.event_type.in_(["fup_throttled", "fup_blocked"]))
        .filter(Notification.created_at >= start)
        .filter(Notification.created_at < end)
        .first()
        is not None
    )


def _maybe_queue_repeat_upsell(
    session: Session,
    subscription: Subscription,
    bucket: QuotaBucket | None,
    rule_result: dict,
    pending_notifications: list[dict],
) -> None:
    from app.models.notification import Notification

    # The upsell compares against prior billing cycles, so it is meaningless
    # without a bucket to take the cycle length from.
    if bucket is None or not bucket.period_start or not bucket.period_end:
        return
    period_len = bucket.period_end - bucket.period_start
    if period_len.total_seconds() <= 0:
        return
    already = (
        session.query(Notification.id)
        .filter(Notification.subscriber_id == subscription.subscriber_id)
        .filter(Notification.event_type == "fup_repeat_upsell")
        .filter(Notification.created_at >= bucket.period_start)
        .first()
    )
    if already is not None:
        return
    prior_hits = sum(
        1
        for cycle in (1, 2)
        if _hit_fup_in_window(
            session,
            subscription.subscriber_id,
            bucket.period_start - period_len * cycle,
            bucket.period_start - period_len * (cycle - 1),
        )
    )
    if prior_hits < 1:
        return
    pending_notifications.append(
        {
            "subscriber_id": subscription.subscriber_id,
            "kind": "repeat_upsell",
            "rule_name": rule_result.get("name"),
            "threshold_gb": rule_result.get("threshold_gb"),
            "used_gb": None,
            "cycles": prior_hits + 1,
        }
    )


def _fup_reset_phrase(cap_resets_at: Any) -> str:
    if not cap_resets_at:
        return ""
    try:
        value = (
            datetime.fromisoformat(cap_resets_at)
            if isinstance(cap_resets_at, str)
            else cap_resets_at
        )
        return f" on {value.date().isoformat()}"
    except (ValueError, TypeError, AttributeError):
        return ""


def _build_fup_notification(
    kind: str,
    rule_name: Any,
    threshold_gb: Any,
    used_gb: Any,
    cap_resets_at: Any = None,
) -> tuple[str, str]:
    plan = rule_name or "your plan"
    when = _fup_reset_phrase(cap_resets_at)
    if kind == "blocked":
        return (
            "Service paused",
            f"You've reached the fair-usage limit on {plan}. Service is paused "
            f"until your data allowance resets{when} — or top up data to restore "
            "it now.",
        )
    if kind == "throttled":
        return (
            "Speed reduced",
            f"You've reached the fair-usage limit on {plan}. Your speed is "
            f"reduced until your data allowance resets{when} — or top up data to "
            "restore full speed now.",
        )
    if kind == "repeat_upsell":
        return (
            "Hitting your limit every month?",
            f"You've reached the fair-usage limit on {plan} several months "
            "in a row. A bigger plan gives you more full-speed data every "
            "cycle — see your upgrade options in the app.",
        )
    if kind == "notified":
        return (
            "Data limit reached",
            f"You've reached the fair-usage notification threshold on {plan}.",
        )
    try:
        pct = int(round((used_gb / threshold_gb) * 100)) if threshold_gb else 80
    except (TypeError, ZeroDivisionError):
        pct = 80
    return (
        "Approaching your data limit",
        f"You've used about {pct}% of the fair-usage allowance on {plan}.",
    )


_FUP_NOTIFICATION_DEFAULT_CHANNELS = {
    "approaching": ("push",),
    "notified": ("push", "email"),
    "throttled": ("push", "email"),
    "blocked": ("push", "email"),
    "repeat_upsell": ("push", "email"),
}


def _emit_fup_notifications(session: Session, pending: list[dict]) -> int:
    """Stage policy-selected communication intents in the owner transaction."""
    if not pending:
        return 0
    from app.models.subscriber import Subscriber
    from app.services.notification import notifications as notifications_svc

    sent = 0
    for item in pending:
        subscriber = session.get(Subscriber, item["subscriber_id"])
        subject, body = _build_fup_notification(
            item["kind"],
            item.get("rule_name"),
            item.get("threshold_gb"),
            item.get("used_gb"),
            item.get("cap_resets_at"),
        )
        event_type = f"fup_{item['kind']}"
        created = notifications_svc.queue_customer_notifications_for_policy(
            session,
            subscriber=subscriber,
            template_code=event_type,
            event_type=event_type,
            category="fup",
            default_channels=_FUP_NOTIFICATION_DEFAULT_CHANNELS[item["kind"]],
            subject=subject,
            body=body,
            commit=False,
        )
        if created:
            sent += 1
    return sent


def _lift_expired_subscription(
    db: Session,
    command: LiftExpiredFupSubscriptionCommand,
) -> bool:
    from app.services.enforcement import lift_fup_enforcement

    result = lift_fup_enforcement(
        db,
        str(command.subscription_id),
        evaluated_at=command.evaluated_at,
    )
    return bool(result.get("lifted"))


def lift_expired_fup_subscription(
    db: Session,
    command: LiftExpiredFupSubscriptionCommand,
) -> bool:
    return _execute(
        db,
        context=command.context,
        name="lift_expired_fup_subscription",
        operation=lambda: _lift_expired_subscription(db, command),
    )


def run_expired_fup_lift(
    db: Session,
    request: RunExpiredFupLiftRequest,
) -> dict[str, int]:
    """Repair overdue enforcement with one isolated owner command per state."""
    source = _validate_source(request.source)
    now = datetime.now(UTC)
    pending_ids = [
        state.subscription_id
        for state in fup_state_service.fup_state.list_pending_reset(db, now)
    ]
    _release_read_transaction(db)
    lifted = 0
    errors = 0
    for subscription_id in pending_ids:
        try:
            if lift_expired_fup_subscription(
                db,
                LiftExpiredFupSubscriptionCommand(
                    context=_command_context(
                        correlation_id=request.correlation_id,
                        subscription_id=subscription_id,
                        source=source,
                    ),
                    subscription_id=subscription_id,
                    evaluated_at=now,
                ),
            ):
                lifted += 1
        except Exception as exc:
            errors += 1
            logger.error(
                "Failed FUP lift subscription=%s error=%s",
                subscription_id,
                exc,
            )
    return {"pending": len(pending_ids), "lifted": lifted, "errors": errors}


__all__ = [
    "EvaluateFupSubscriptionCommand",
    "FupEnforcementError",
    "FupSubscriptionOutcome",
    "LiftExpiredFupSubscriptionCommand",
    "RunExpiredFupLiftRequest",
    "RunFupSweepRequest",
    "evaluate_fup_subscription",
    "lift_expired_fup_subscription",
    "run_expired_fup_lift",
    "run_fup_evaluation",
    "stage_fup_runtime_state",
]
