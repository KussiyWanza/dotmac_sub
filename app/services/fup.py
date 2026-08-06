"""Fair Usage Policy service for traffic-based speed reduction policies."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time
from typing import TypeVar
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models.catalog import CatalogOffer
from app.models.fup import (
    FupAction,
    FupConsumptionPeriod,
    FupDataUnit,
    FupDirection,
    FupPolicy,
    FupRule,
)
from app.services.common import coerce_uuid
from app.services.domain_errors import DomainError
from app.services.events import EventType, emit_event
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

ResultT = TypeVar("ResultT")


class FupRuleEngineError(DomainError):
    """Stable failures from the FUP policy and rule owner."""


def _error(suffix: str, message: str) -> FupRuleEngineError:
    return FupRuleEngineError(
        code=f"access.fup_rule_engine.{suffix}",
        message=message,
    )


def _definition(name: str) -> OwnerCommandDefinition:
    return OwnerCommandDefinition(
        owner="access.fup_rule_engine",
        concern="FUP policy and rule definitions (CRUD)",
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
class FupPolicySettings:
    traffic_accounting_start: time | None = None
    traffic_accounting_end: time | None = None
    traffic_inverse_interval: bool = False
    online_accounting_start: time | None = None
    online_accounting_end: time | None = None
    online_inverse_interval: bool = False
    traffic_days_of_week: list[int] | None = None
    online_days_of_week: list[int] | None = None
    is_active: bool = True
    notes: str | None = None


@dataclass(frozen=True, slots=True)
class FupRuleSpec:
    name: str
    consumption_period: str
    direction: str
    threshold_amount: float
    threshold_unit: str
    action: str
    speed_reduction_percent: float | None = None
    sort_order: int | None = None
    time_start: time | None = None
    time_end: time | None = None
    enabled_by_rule_id: str | None = None
    cooldown_minutes: int = 0
    days_of_week: list[int] | None = None
    is_active: bool = True


@dataclass(frozen=True, slots=True)
class FupRulePatch:
    updated_fields: frozenset[str]
    name: str | None = None
    consumption_period: str | None = None
    direction: str | None = None
    threshold_amount: float | None = None
    threshold_unit: str | None = None
    action: str | None = None
    speed_reduction_percent: float | None = None
    sort_order: int | None = None
    time_start: time | None = None
    time_end: time | None = None
    enabled_by_rule_id: str | None = None
    cooldown_minutes: int | None = None
    days_of_week: list[int] | None = None
    is_active: bool | None = None


@dataclass(frozen=True, slots=True)
class EnsureFupPolicyCommand:
    context: CommandContext
    offer_id: str


@dataclass(frozen=True, slots=True)
class UpdateFupPolicyCommand:
    context: CommandContext
    offer_id: str
    settings: FupPolicySettings


@dataclass(frozen=True, slots=True)
class AddFupRuleCommand:
    context: CommandContext
    offer_id: str
    spec: FupRuleSpec


@dataclass(frozen=True, slots=True)
class UpdateFupRuleCommand:
    context: CommandContext
    rule_id: str
    patch: FupRulePatch


@dataclass(frozen=True, slots=True)
class DeleteFupRuleCommand:
    context: CommandContext
    rule_id: str


@dataclass(frozen=True, slots=True)
class CloneFupRulesCommand:
    context: CommandContext
    source_offer_id: str
    target_offer_id: str


def _enum(value: str, enum_type, label: str):
    try:
        return enum_type(value)
    except ValueError as exc:
        raise _error("invalid_rule", f"Invalid {label}.") from exc


def _validate_days(days: list[int] | None) -> list[int] | None:
    if days is None:
        return None
    if any(day < 0 or day > 6 for day in days):
        raise _error("invalid_rule", "FUP days must be between 0 and 6.")
    return sorted(set(days))


def _validate_spec(spec: FupRuleSpec) -> None:
    if not spec.name.strip():
        raise _error("invalid_rule", "FUP rule name is required.")
    if spec.threshold_amount <= 0:
        raise _error("invalid_rule", "FUP threshold must be positive.")
    if spec.sort_order is not None and spec.sort_order < 0:
        raise _error("invalid_rule", "FUP sort order cannot be negative.")
    if spec.cooldown_minutes < 0:
        raise _error("invalid_rule", "FUP cooldown cannot be negative.")
    if (
        spec.speed_reduction_percent is not None
        and not 0 < spec.speed_reduction_percent < 100
    ):
        raise _error(
            "invalid_rule",
            "FUP speed reduction must be between 1 and 99 percent.",
        )
    _validate_days(spec.days_of_week)


def _offer(db: Session, offer_id: str, *, for_update: bool = False) -> CatalogOffer:
    query = db.query(CatalogOffer).filter(CatalogOffer.id == coerce_uuid(offer_id))
    if for_update:
        query = query.with_for_update(of=CatalogOffer)
    offer = query.one_or_none()
    if offer is None:
        raise _error("offer_not_found", "Catalog offer not found.")
    return offer


def _policy_by_offer(
    db: Session, offer_id: str, *, for_update: bool = False
) -> FupPolicy | None:
    query = (
        db.query(FupPolicy)
        .options(joinedload(FupPolicy.rules))
        .filter(FupPolicy.offer_id == coerce_uuid(offer_id))
    )
    if for_update:
        query = query.with_for_update(of=FupPolicy)
    return query.one_or_none()


def _ensure_policy(db: Session, offer_id: str) -> tuple[FupPolicy, bool]:
    offer = _offer(db, offer_id, for_update=True)
    policy = _policy_by_offer(db, str(offer.id), for_update=True)
    if policy is not None:
        return policy, False
    policy = FupPolicy(offer_id=offer.id, is_active=True)
    db.add(policy)
    db.flush()
    return policy, True


def _rule(db: Session, rule_id: str, *, for_update: bool = False) -> FupRule:
    query = db.query(FupRule).filter(FupRule.id == coerce_uuid(rule_id))
    if for_update:
        query = query.with_for_update(of=FupRule)
    rule = query.one_or_none()
    if rule is None:
        raise _error("rule_not_found", "FUP rule not found.")
    return rule


def _resolve_prerequisite(
    db: Session,
    *,
    policy_id: object,
    rule_id: str | None,
    excluding_rule_id: object | None = None,
):
    if not rule_id:
        return None
    prerequisite = _rule(db, rule_id, for_update=True)
    if prerequisite.policy_id != policy_id or prerequisite.id == excluding_rule_id:
        raise _error(
            "invalid_rule_chain",
            "FUP prerequisite must be another rule in the same policy.",
        )
    return prerequisite.id


def _emit_change(
    db: Session,
    context: CommandContext,
    *,
    action: str,
    policy_id: object,
    offer_id: object,
    rule_id: object | None = None,
) -> None:
    emit_event(
        db,
        EventType.fup_policy_changed,
        {
            "schema_version": 1,
            "action": action,
            "policy_id": str(policy_id),
            "offer_id": str(offer_id),
            "rule_id": str(rule_id) if rule_id is not None else None,
            "command_id": str(context.command_id),
            "correlation_id": str(context.correlation_id),
        },
        actor=context.actor,
    )


class FupPolicies:
    """Canonical owner for FUP policy/rule state and rule evaluation inputs."""

    @staticmethod
    def get_by_offer(db: Session, offer_id: str) -> FupPolicy | None:
        """Get the FUP policy for a catalog offer, or None if none exists.

        Args:
            db: Database session.
            offer_id: The catalog offer UUID.

        Returns:
            The FupPolicy or None.
        """
        stmt = (
            select(FupPolicy)
            .options(joinedload(FupPolicy.rules))
            .where(FupPolicy.offer_id == coerce_uuid(offer_id))
        )
        return db.scalars(stmt).unique().first()

    @staticmethod
    def ensure(db: Session, command: EnsureFupPolicyCommand) -> FupPolicy:
        def operation() -> FupPolicy:
            policy, created = _ensure_policy(db, command.offer_id)
            if created:
                _emit_change(
                    db,
                    command.context,
                    action="policy_created",
                    policy_id=policy.id,
                    offer_id=policy.offer_id,
                )
            return policy

        return _execute(
            db,
            context=command.context,
            name="ensure_fup_policy",
            operation=operation,
        )

    @staticmethod
    def update_policy(db: Session, command: UpdateFupPolicyCommand) -> FupPolicy:
        def operation() -> FupPolicy:
            policy, _created = _ensure_policy(db, command.offer_id)
            settings = command.settings
            policy.traffic_accounting_start = settings.traffic_accounting_start
            policy.traffic_accounting_end = settings.traffic_accounting_end
            policy.traffic_inverse_interval = settings.traffic_inverse_interval
            policy.online_accounting_start = settings.online_accounting_start
            policy.online_accounting_end = settings.online_accounting_end
            policy.online_inverse_interval = settings.online_inverse_interval
            policy.traffic_days_of_week = _validate_days(settings.traffic_days_of_week)
            policy.online_days_of_week = _validate_days(settings.online_days_of_week)
            policy.is_active = settings.is_active
            policy.notes = (settings.notes or "").strip() or None
            policy.updated_at = datetime.now(UTC)
            db.flush()
            _emit_change(
                db,
                command.context,
                action="policy_updated",
                policy_id=policy.id,
                offer_id=policy.offer_id,
            )
            return policy

        return _execute(
            db,
            context=command.context,
            name="update_fup_policy",
            operation=operation,
        )

    @staticmethod
    def add_rule(db: Session, command: AddFupRuleCommand) -> FupRule:
        def operation() -> FupRule:
            _validate_spec(command.spec)
            policy, _created = _ensure_policy(db, command.offer_id)
            max_order_stmt = (
                select(FupRule.sort_order)
                .where(FupRule.policy_id == policy.id)
                .order_by(FupRule.sort_order.desc())
                .limit(1)
            )
            max_order = db.scalars(max_order_stmt).first()
            spec = command.spec
            rule = FupRule(
                policy_id=policy.id,
                name=spec.name.strip(),
                sort_order=(
                    spec.sort_order
                    if spec.sort_order is not None
                    else (max_order or 0) + 1
                ),
                consumption_period=_enum(
                    spec.consumption_period,
                    FupConsumptionPeriod,
                    "consumption period",
                ),
                direction=_enum(spec.direction, FupDirection, "direction"),
                threshold_amount=spec.threshold_amount,
                threshold_unit=_enum(
                    spec.threshold_unit, FupDataUnit, "threshold unit"
                ),
                action=_enum(spec.action, FupAction, "action"),
                speed_reduction_percent=spec.speed_reduction_percent,
                time_start=spec.time_start,
                time_end=spec.time_end,
                enabled_by_rule_id=_resolve_prerequisite(
                    db,
                    policy_id=policy.id,
                    rule_id=spec.enabled_by_rule_id,
                ),
                cooldown_minutes=spec.cooldown_minutes,
                days_of_week=_validate_days(spec.days_of_week),
                is_active=spec.is_active,
            )
            db.add(rule)
            db.flush()
            _emit_change(
                db,
                command.context,
                action="rule_added",
                policy_id=policy.id,
                offer_id=policy.offer_id,
                rule_id=rule.id,
            )
            return rule

        return _execute(
            db,
            context=command.context,
            name="add_fup_rule",
            operation=operation,
        )

    @staticmethod
    def update_rule(db: Session, command: UpdateFupRuleCommand) -> FupRule:
        def operation() -> FupRule:
            rule = _rule(db, command.rule_id, for_update=True)
            patch = command.patch
            fields = patch.updated_fields
            allowed_fields = set(FupRulePatch.__dataclass_fields__) - {"updated_fields"}
            if not fields or not fields <= allowed_fields:
                raise _error("invalid_rule", "FUP rule update fields are invalid.")
            if "name" in fields:
                if not patch.name or not patch.name.strip():
                    raise _error("invalid_rule", "FUP rule name is required.")
                rule.name = patch.name.strip()
            if "sort_order" in fields:
                if patch.sort_order is None or patch.sort_order < 0:
                    raise _error("invalid_rule", "FUP sort order cannot be negative.")
                rule.sort_order = patch.sort_order
            if "consumption_period" in fields:
                if patch.consumption_period is None:
                    raise _error("invalid_rule", "FUP consumption period is required.")
                rule.consumption_period = _enum(
                    patch.consumption_period,
                    FupConsumptionPeriod,
                    "consumption period",
                )
            if "direction" in fields:
                if patch.direction is None:
                    raise _error("invalid_rule", "FUP direction is required.")
                rule.direction = _enum(patch.direction, FupDirection, "direction")
            if "threshold_amount" in fields:
                if patch.threshold_amount is None or patch.threshold_amount <= 0:
                    raise _error("invalid_rule", "FUP threshold must be positive.")
                rule.threshold_amount = patch.threshold_amount
            if "threshold_unit" in fields:
                if patch.threshold_unit is None:
                    raise _error("invalid_rule", "FUP threshold unit is required.")
                rule.threshold_unit = _enum(
                    patch.threshold_unit, FupDataUnit, "threshold unit"
                )
            if "action" in fields:
                if patch.action is None:
                    raise _error("invalid_rule", "FUP action is required.")
                rule.action = _enum(patch.action, FupAction, "action")
            if "speed_reduction_percent" in fields:
                if patch.speed_reduction_percent is not None and not (
                    0 < patch.speed_reduction_percent < 100
                ):
                    raise _error(
                        "invalid_rule",
                        "FUP speed reduction must be between 1 and 99 percent.",
                    )
                rule.speed_reduction_percent = patch.speed_reduction_percent
            if "time_start" in fields:
                rule.time_start = patch.time_start
            if "time_end" in fields:
                rule.time_end = patch.time_end
            if "enabled_by_rule_id" in fields:
                rule.enabled_by_rule_id = _resolve_prerequisite(
                    db,
                    policy_id=rule.policy_id,
                    rule_id=patch.enabled_by_rule_id,
                    excluding_rule_id=rule.id,
                )
            if "cooldown_minutes" in fields:
                if patch.cooldown_minutes is None or patch.cooldown_minutes < 0:
                    raise _error("invalid_rule", "FUP cooldown cannot be negative.")
                rule.cooldown_minutes = patch.cooldown_minutes
            if "days_of_week" in fields:
                rule.days_of_week = _validate_days(patch.days_of_week)
            if "is_active" in fields:
                if patch.is_active is None:
                    raise _error("invalid_rule", "FUP active state is required.")
                rule.is_active = patch.is_active
            rule.updated_at = datetime.now(UTC)
            db.flush()
            policy = _policy_by_offer(db, str(rule.policy.offer_id))
            if policy is None:
                raise _error("policy_not_found", "FUP policy not found.")
            _emit_change(
                db,
                command.context,
                action="rule_updated",
                policy_id=rule.policy_id,
                offer_id=policy.offer_id,
                rule_id=rule.id,
            )
            return rule

        return _execute(
            db,
            context=command.context,
            name="update_fup_rule",
            operation=operation,
        )

    @staticmethod
    def delete_rule(db: Session, command: DeleteFupRuleCommand) -> None:
        def operation() -> None:
            rule = _rule(db, command.rule_id, for_update=True)
            policy_id = rule.policy_id
            offer_id = rule.policy.offer_id
            rule_id = rule.id
            db.delete(rule)
            db.flush()
            _emit_change(
                db,
                command.context,
                action="rule_deleted",
                policy_id=policy_id,
                offer_id=offer_id,
                rule_id=rule_id,
            )

        return _execute(
            db,
            context=command.context,
            name="delete_fup_rule",
            operation=operation,
        )

    @staticmethod
    def list_rules(db: Session, policy_id: str) -> list[FupRule]:
        """List all rules for a given FUP policy, ordered by sort_order.

        Args:
            db: Database session.
            policy_id: The FUP policy UUID.

        Returns:
            List of FupRule objects.
        """
        stmt = (
            select(FupRule)
            .where(FupRule.policy_id == coerce_uuid(policy_id))
            .order_by(FupRule.sort_order.asc())
        )
        return list(db.scalars(stmt).all())

    @staticmethod
    def clone_rules(db: Session, command: CloneFupRulesCommand) -> list[FupRule]:
        def operation() -> list[FupRule]:
            _offer(db, command.source_offer_id, for_update=True)
            source_policy = _policy_by_offer(
                db, command.source_offer_id, for_update=True
            )
            if source_policy is None:
                raise _error(
                    "source_policy_not_found",
                    "Source offer has no FUP policy.",
                )
            target_policy, _created = _ensure_policy(db, command.target_offer_id)
            cloned: list[FupRule] = []
            rule_map: dict[str, FupRule] = {}
            for source_rule in source_policy.rules:
                rule = FupRule(
                    policy_id=target_policy.id,
                    name=source_rule.name,
                    sort_order=source_rule.sort_order,
                    consumption_period=source_rule.consumption_period,
                    direction=source_rule.direction,
                    threshold_amount=source_rule.threshold_amount,
                    threshold_unit=source_rule.threshold_unit,
                    action=source_rule.action,
                    speed_reduction_percent=source_rule.speed_reduction_percent,
                    cooldown_minutes=source_rule.cooldown_minutes,
                    time_start=source_rule.time_start,
                    time_end=source_rule.time_end,
                    days_of_week=(
                        list(source_rule.days_of_week)
                        if source_rule.days_of_week
                        else None
                    ),
                    is_active=source_rule.is_active,
                )
                db.add(rule)
                cloned.append(rule)
                rule_map[str(source_rule.id)] = rule
            db.flush()
            for source_rule in source_policy.rules:
                cloned_rule = rule_map[str(source_rule.id)]
                if source_rule.enabled_by_rule_id:
                    prerequisite = rule_map.get(str(source_rule.enabled_by_rule_id))
                    if prerequisite is None:
                        raise _error(
                            "invalid_rule_chain",
                            "Source FUP rule chain is inconsistent.",
                        )
                    cloned_rule.enabled_by_rule_id = prerequisite.id
            _emit_change(
                db,
                command.context,
                action="rules_cloned",
                policy_id=target_policy.id,
                offer_id=target_policy.offer_id,
            )
            return cloned

        return _execute(
            db,
            context=command.context,
            name="clone_fup_rules",
            operation=operation,
        )


fup_policies = FupPolicies()


# ═══════════════════════════════════════════════════════════════════
# FUP Rule Evaluation & Simulation Engine
# ═══════════════════════════════════════════════════════════════════


def _threshold_gb(rule: FupRule) -> float:
    """Convert threshold to GB for comparison."""
    amount = rule.threshold_amount or 0
    unit = rule.threshold_unit.value if rule.threshold_unit else "gb"
    return {"mb": amount / 1024, "gb": amount, "tb": amount * 1024}.get(unit, amount)


def _time_in_window(
    check_time: datetime | None,
    start: time | None,
    end: time | None,
    inverse: bool = False,
    tz: ZoneInfo | None = None,
) -> bool:
    """Check if a time falls within a start-end window.

    If inverse=True, returns True when OUTSIDE the window (e.g., night browsing
    is "free" = traffic inside window doesn't count, so inverse window is "counted").

    ``tz`` is the subscriber's timezone. A window is configured in *wall clock*
    terms — "the night is free from 22:00" means the customer's 22:00 — but
    ``check_time`` is a UTC instant, so comparing its raw ``.time()`` shifted
    every window by the UTC offset: a 22:00 window opened at 23:00 in Lagos.
    Daily consumption windows are already aligned to subscriber-local midnight
    by ``fup_usage.fup_window_bounds``; this makes the time-of-day gate agree
    with them instead of straddling two clocks.
    """
    if start is None or end is None:
        return True  # no window = always applies
    if check_time is None:
        return True

    t = check_time.astimezone(tz).time() if tz is not None else check_time.time()

    if start <= end:
        in_window = start <= t <= end
    else:
        # Overnight window (e.g., 22:00 → 06:00)
        in_window = t >= start or t <= end

    return not in_window if inverse else in_window


def _day_in_list(
    check_time: datetime | None,
    days: list[int] | None,
    tz: ZoneInfo | None = None,
) -> bool:
    """Check if a datetime's weekday is in the allowed days list (0=Mon..6=Sun).

    Resolved in ``tz`` for the same reason as ``_time_in_window``: a weekday is
    a wall-clock fact, and late-evening traffic east of UTC otherwise counts
    against the following day.
    """
    if not days:
        return True  # no filter = all days
    if check_time is None:
        return True
    local = check_time.astimezone(tz) if tz is not None else check_time
    return local.weekday() in days


def evaluate_rules(
    db: Session,
    offer_id: str,
    *,
    current_usage_gb: float,
    current_time: datetime | None = None,
    fired_rule_ids: set | None = None,
    usage_by_period: dict | None = None,
    tz: ZoneInfo | None = None,
) -> list[dict]:
    """Evaluate FUP rules for an offer against current usage.

    ``tz`` is the subscriber's timezone, used only for time-of-day and
    day-of-week gating. Enforcement supplies it; the simulation/preview path
    may omit it, in which case windows are read in UTC as before.

    ``usage_by_period`` (optional) maps a consumption_period -> FupUsageWindow so
    each rule is compared against usage over ITS own window (daily/weekly/
    monthly). When omitted, every rule uses ``current_usage_gb`` (the legacy
    monthly figure) — preserving the simulation/preview path unchanged.

    Returns a list of rule evaluation results (in sort_order):
    - rule_id, name, sort_order
    - threshold_gb, triggered (bool)
    - action, speed_reduction_percent
    - reason: why triggered or skipped
    - blocked_by: rule that needs to fire first (if chained)

    This is the core engine used by both simulation and real enforcement.
    """
    policy = FupPolicies.get_by_offer(db, offer_id)
    if not policy or not policy.is_active:
        return []

    current_time = current_time or datetime.now(UTC)
    fired_rule_ids = fired_rule_ids or set()

    results: list[dict] = []

    for rule in policy.rules:
        if not rule.is_active:
            results.append(
                {
                    "rule_id": str(rule.id),
                    "name": rule.name,
                    "sort_order": rule.sort_order,
                    "threshold_gb": _threshold_gb(rule),
                    "triggered": False,
                    "action": rule.action.value,
                    "speed_reduction_percent": rule.speed_reduction_percent,
                    "reason": "Rule is disabled",
                    "status": "disabled",
                }
            )
            continue

        threshold = _threshold_gb(rule)

        # Per-period usage: each rule is measured over its own consumption
        # window when usage_by_period is supplied; else the legacy monthly value.
        from app.services.fup_usage import period_value

        usage_window = (
            usage_by_period.get(period_value(rule.consumption_period))
            if usage_by_period
            else None
        )
        rule_usage_gb = (
            usage_window.used_gb if usage_window is not None else current_usage_gb
        )

        # Check chaining: does this rule require a prior rule to have fired?
        if (
            rule.enabled_by_rule_id
            and str(rule.enabled_by_rule_id) not in fired_rule_ids
        ):
            results.append(
                {
                    "rule_id": str(rule.id),
                    "name": rule.name,
                    "sort_order": rule.sort_order,
                    "threshold_gb": threshold,
                    "triggered": False,
                    "action": rule.action.value,
                    "speed_reduction_percent": rule.speed_reduction_percent,
                    "reason": "Waiting for prerequisite rule to fire",
                    "blocked_by": str(rule.enabled_by_rule_id),
                    "status": "waiting",
                }
            )
            continue

        # Time-of-day gating is the RULE's alone. The policy's
        # traffic_accounting_* window is a different decision — which traffic
        # ACCRUES, applied in fup_usage — and letting it fall through to here
        # made one field silently govern both, so a "night is free" accounting
        # window also stopped every rule from releasing at night. Each field
        # now owns exactly one thing (PLAN_FAMILY_ARCHITECTURE §12).
        #
        # No policy in production sets traffic_accounting_start, so dropping
        # the fallback is a no-op on current data.
        time_start = rule.time_start
        time_end = rule.time_end

        if not _time_in_window(current_time, time_start, time_end, tz=tz):
            results.append(
                {
                    "rule_id": str(rule.id),
                    "name": rule.name,
                    "sort_order": rule.sort_order,
                    "threshold_gb": threshold,
                    "triggered": False,
                    "action": rule.action.value,
                    "speed_reduction_percent": rule.speed_reduction_percent,
                    "reason": f"Outside time window ({time_start}-{time_end})",
                    "status": "time_skip",
                }
            )
            continue

        # Check day-of-week
        days = rule.days_of_week or policy.traffic_days_of_week
        if not _day_in_list(current_time, days, tz=tz):
            day_names = {
                0: "Mon",
                1: "Tue",
                2: "Wed",
                3: "Thu",
                4: "Fri",
                5: "Sat",
                6: "Sun",
            }
            allowed = ", ".join(day_names.get(d, str(d)) for d in (days or []))
            results.append(
                {
                    "rule_id": str(rule.id),
                    "name": rule.name,
                    "sort_order": rule.sort_order,
                    "threshold_gb": threshold,
                    "triggered": False,
                    "action": rule.action.value,
                    "speed_reduction_percent": rule.speed_reduction_percent,
                    "reason": f"Not active today (active: {allowed})",
                    "status": "day_skip",
                }
            )
            continue

        # Check threshold (against this rule's own consumption window)
        triggered = rule_usage_gb >= threshold

        if triggered:
            fired_rule_ids.add(str(rule.id))

        results.append(
            {
                "rule_id": str(rule.id),
                "name": rule.name,
                "sort_order": rule.sort_order,
                "threshold_gb": threshold,
                "current_usage_gb": round(rule_usage_gb, 2),
                "triggered": triggered,
                "action": rule.action.value if triggered else None,
                "speed_reduction_percent": rule.speed_reduction_percent
                if triggered
                else None,
                "reason": (
                    f"Usage {rule_usage_gb:.1f} GB >= threshold {threshold:.1f} GB"
                    if triggered
                    else f"Usage {rule_usage_gb:.1f} GB < threshold {threshold:.1f} GB"
                ),
                "status": "triggered" if triggered else "ok",
                "usage_percent": round(rule_usage_gb / threshold * 100, 1)
                if threshold > 0
                else 0,
                "consumption_period": (
                    rule.consumption_period.value
                    if rule.consumption_period
                    else "monthly"
                ),
                "window_start": (
                    usage_window.window.start.isoformat() if usage_window else None
                ),
                "window_end": (
                    usage_window.window.end.isoformat() if usage_window else None
                ),
                "usage_source": usage_window.source if usage_window else None,
                "is_authoritative": (
                    usage_window.is_authoritative if usage_window else None
                ),
                "cooldown_minutes": rule.cooldown_minutes or 0,
            }
        )

    return results


def simulate_fup(
    db: Session,
    offer_id: str,
    *,
    current_usage_gb: float = 0,
    current_time: datetime | None = None,
    current_day: int | None = None,
    billing_day_elapsed: int = 15,
    billing_cycle_days: int = 30,
) -> dict:
    """Simulate FUP rule evaluation for a given scenario.

    Returns a complete simulation result with:
    - rules: list of rule evaluation results
    - summary: overall status (ok/warning/throttled/blocked)
    - projection: estimated usage at end of cycle
    """
    if current_time is None:
        current_time = datetime.now(UTC)
    if current_day is not None:
        # Override the day-of-week for simulation
        # Find next date with the target weekday
        while current_time.weekday() != current_day:
            from datetime import timedelta

            current_time += timedelta(days=1)

    rules = evaluate_rules(
        db,
        offer_id,
        current_usage_gb=current_usage_gb,
        current_time=current_time,
    )

    # Calculate projection
    daily_avg = current_usage_gb / max(billing_day_elapsed, 1)
    projected_total = daily_avg * billing_cycle_days
    days_remaining = max(0, billing_cycle_days - billing_day_elapsed)

    # Determine overall status
    triggered_rules = [r for r in rules if r.get("triggered")]
    if any(r["action"] == "block" for r in triggered_rules):
        status = "blocked"
    elif any(r["action"] == "reduce_speed" for r in triggered_rules):
        status = "throttled"
    elif any(r["action"] == "notify" for r in triggered_rules):
        status = "warning"
    else:
        status = "ok"

    # Find the highest threshold for progress calculation
    max_threshold = max((r["threshold_gb"] for r in rules), default=0)

    return {
        "rules": rules,
        "summary": {
            "status": status,
            "triggered_count": len(triggered_rules),
            "total_rules": len(rules),
            "current_usage_gb": round(current_usage_gb, 2),
            "max_threshold_gb": round(max_threshold, 2),
            "usage_percent": round(current_usage_gb / max_threshold * 100, 1)
            if max_threshold > 0
            else 0,
        },
        "projection": {
            "daily_average_gb": round(daily_avg, 2),
            "projected_total_gb": round(projected_total, 2),
            "days_remaining": days_remaining,
            "will_exceed": projected_total > max_threshold
            if max_threshold > 0
            else False,
            "days_until_cap": round((max_threshold - current_usage_gb) / daily_avg, 1)
            if daily_avg > 0 and max_threshold > current_usage_gb
            else 0,
        },
    }
