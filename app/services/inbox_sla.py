"""Typed Inbox SLA policy selection, calendar arithmetic, and evaluation.

Public configuration and sweep commands own their transactions. Lifecycle
participants called from Inbox owners only flush within the caller's command.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Final
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.inbox_sla import (
    InboxSlaClock,
    InboxSlaEvent,
    InboxSlaPolicy,
    InboxSlaRule,
)
from app.models.team_inbox import (
    InboxConversation,
    InboxConversationStatus,
    InboxMessage,
    InboxMessageDirection,
)
from app.services.audit_adapter import AuditActor, stage_audit_event
from app.services.domain_errors import DomainError
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

_POLICY_COMMAND = OwnerCommandDefinition(
    owner="communications.inbox_sla",
    concern="Inbox SLA policy selection and clock state",
    name="execute_inbox_sla_policy_command",
)

STATUS_RUNNING: Final = "running"
STATUS_WARNING: Final = "warning"
STATUS_BREACHED: Final = "breached"
STATUS_PAUSED: Final = "paused"
STATUS_COMPLETED: Final = "completed"


class InboxSlaError(DomainError, ValueError):
    """A rejected Inbox SLA command or invalid configuration."""


@dataclass(frozen=True, slots=True)
class SlaRuleInput:
    first_response_minutes: int
    resolution_minutes: int
    warning_minutes: int = 0
    next_response_minutes: int | None = None
    service_team_id: UUID | None = None
    channel_type: str | None = None
    priority: int | None = None
    source_reference: str | None = None


@dataclass(frozen=True, slots=True)
class SlaPolicyInput:
    name: str
    description: str | None
    rules: tuple[SlaRuleInput, ...]
    timezone: str = "Africa/Lagos"
    working_days: tuple[int, ...] = (0, 1, 2, 3, 4)
    workday_start: time = time(9)
    workday_end: time = time(17)
    holidays: tuple[date, ...] = ()
    is_default: bool = False
    source_reference: str | None = None


def _error(code: str, message: str, **details: object) -> InboxSlaError:
    return InboxSlaError(
        code=f"communications.inbox_sla.{code}", message=message, details=details
    )


def validate_policy(command: SlaPolicyInput) -> None:
    if not command.name.strip() or not command.rules:
        raise _error(
            "incomplete_policy",
            "An Inbox SLA policy needs a name and at least one rule.",
        )
    if command.timezone not in {"Africa/Lagos", "UTC"}:
        try:
            ZoneInfo(command.timezone)
        except Exception as exc:
            raise _error(
                "invalid_timezone", "The policy timezone is not supported."
            ) from exc
    if not set(command.working_days) <= set(range(7)) or not command.working_days:
        raise _error(
            "invalid_working_days", "Working days must contain weekdays 0 through 6."
        )
    if command.workday_start >= command.workday_end:
        raise _error(
            "invalid_working_hours", "Working hours must have a positive duration."
        )
    seen: set[tuple[UUID | None, str | None, int | None]] = set()
    for rule in command.rules:
        key = (rule.service_team_id, rule.channel_type, rule.priority)
        if key in seen:
            raise _error(
                "overlapping_rules",
                "Rules with the same team, channel, and priority overlap.",
                key=key,
            )
        seen.add(key)
        if rule.first_response_minutes <= 0 or rule.resolution_minutes <= 0:
            raise _error(
                "invalid_target", "Response and resolution targets must be positive."
            )
        if (
            rule.warning_minutes < 0
            or rule.warning_minutes >= rule.first_response_minutes
        ):
            raise _error(
                "invalid_warning",
                "Warning minutes must be non-negative and less than first response minutes.",
            )


def _create_policy(db: Session, command: SlaPolicyInput) -> InboxSlaPolicy:
    validate_policy(command)
    if (
        db.query(InboxSlaPolicy)
        .filter(InboxSlaPolicy.name == command.name.strip())
        .first()
    ):
        raise _error(
            "duplicate_policy", "An Inbox SLA policy with this name already exists."
        )
    if command.is_default:
        db.query(InboxSlaPolicy).filter(InboxSlaPolicy.is_default.is_(True)).update(
            {"is_default": False}, synchronize_session="fetch"
        )
    policy = InboxSlaPolicy(
        name=command.name.strip(),
        description=command.description,
        timezone=command.timezone,
        working_days=list(command.working_days),
        workday_start=command.workday_start,
        workday_end=command.workday_end,
        holidays=[item.isoformat() for item in command.holidays],
        is_default=command.is_default,
        source_reference=command.source_reference,
    )
    db.add(policy)
    db.flush()
    for item in command.rules:
        db.add(
            InboxSlaRule(
                policy_id=policy.id,
                first_response_minutes=item.first_response_minutes,
                next_response_minutes=item.next_response_minutes,
                resolution_minutes=item.resolution_minutes,
                warning_minutes=item.warning_minutes,
                service_team_id=item.service_team_id,
                channel_type=item.channel_type,
                priority=item.priority,
                source_reference=item.source_reference,
            )
        )
    db.flush()
    return policy


def select_policy_rule(
    db: Session, conversation: InboxConversation
) -> tuple[InboxSlaPolicy, InboxSlaRule] | None:
    policies = db.query(InboxSlaPolicy).filter(InboxSlaPolicy.is_active.is_(True)).all()
    candidates: list[tuple[int, InboxSlaPolicy, InboxSlaRule]] = []
    for policy in policies:
        for rule in policy.rules:
            if not rule.is_active:
                continue
            if (
                rule.service_team_id is not None
                and rule.service_team_id != conversation.primary_service_team_id
            ):
                continue
            if (
                rule.channel_type is not None
                and rule.channel_type != conversation.channel_type
            ):
                continue
            if rule.priority is not None and rule.priority != conversation.priority:
                continue
            specificity = sum(
                value is not None
                for value in (rule.service_team_id, rule.channel_type, rule.priority)
            )
            candidates.append((specificity, policy, rule))
    if not candidates:
        return None
    _, policy, rule = max(
        candidates,
        key=lambda item: (
            item[0],
            1 if item[1].is_default else 0,
            str(item[1].id),
            str(item[2].id),
        ),
    )
    return policy, rule


def _local(policy: InboxSlaPolicy, value: datetime) -> datetime:
    aware = value if value.tzinfo else value.replace(tzinfo=UTC)
    return aware.astimezone(ZoneInfo(policy.timezone))


def add_calendar_minutes(start: datetime, minutes: int) -> datetime:
    return start + timedelta(minutes=minutes)


def add_business_minutes(
    start: datetime, minutes: int, policy: InboxSlaPolicy
) -> datetime:
    """Add minutes in the policy's local working calendar, returning UTC."""
    if minutes <= 0:
        return start.astimezone(UTC)
    zone = ZoneInfo(policy.timezone)
    current = _local(policy, start)
    remaining = minutes * 60
    holidays = set(policy.holidays or [])
    while remaining > 0:
        if (
            current.weekday() in set(policy.working_days or [])
            and current.date().isoformat() not in holidays
        ):
            opening = current.replace(
                hour=policy.workday_start.hour,
                minute=policy.workday_start.minute,
                second=0,
                microsecond=0,
            )
            closing = current.replace(
                hour=policy.workday_end.hour,
                minute=policy.workday_end.minute,
                second=0,
                microsecond=0,
            )
            if current < opening:
                current = opening
            if opening <= current < closing:
                available = int((closing - current).total_seconds())
                if remaining <= available:
                    return (current + timedelta(seconds=remaining)).astimezone(UTC)
                remaining -= available
        current = (current + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    return current.astimezone(UTC)


def _due(start: datetime, minutes: int, policy: InboxSlaPolicy) -> datetime:
    return add_business_minutes(start, minutes, policy)


def _record(
    db: Session,
    clock: InboxSlaClock,
    event_type: str,
    event_key: str,
    occurred_at: datetime,
) -> None:
    if (
        db.query(InboxSlaEvent)
        .filter(
            InboxSlaEvent.clock_id == clock.id, InboxSlaEvent.event_key == event_key
        )
        .first()
    ):
        return
    db.add(
        InboxSlaEvent(
            clock_id=clock.id,
            conversation_id=clock.conversation_id,
            event_key=event_key,
            event_type=event_type,
            occurred_at=occurred_at,
        )
    )
    db.flush()
    from app.services.events import emit_event
    from app.services.events.types import EventType

    emit_event(
        db,
        EventType.inbox_sla_clock_changed,
        {
            "clock_id": str(clock.id),
            "conversation_id": str(clock.conversation_id),
            "event_key": event_key,
            "transition": event_type,
            "occurred_at": occurred_at.isoformat(),
        },
    )


def ensure_clock(
    db: Session, conversation: InboxConversation, *, started_at: datetime | None = None
) -> InboxSlaClock | None:
    existing = (
        db.query(InboxSlaClock)
        .filter(InboxSlaClock.conversation_id == conversation.id)
        .with_for_update()
        .first()
    )
    if existing:
        return existing
    selected = select_policy_rule(db, conversation)
    if selected is None:
        return None
    policy, rule = selected
    start = (
        started_at or conversation.first_message_at or conversation.created_at
    ).astimezone(UTC)
    clock = InboxSlaClock(
        conversation_id=conversation.id,
        policy_id=policy.id,
        rule_id=rule.id,
        started_at=start,
        first_response_due_at=_due(start, rule.first_response_minutes, policy),
        resolution_due_at=_due(start, rule.resolution_minutes, policy),
        status=STATUS_RUNNING,
    )
    db.add(clock)
    db.flush()
    _record(db, clock, "started", f"started:{start.isoformat()}", start)
    return clock


def record_inbound(
    db: Session, conversation: InboxConversation, *, occurred_at: datetime | None = None
) -> InboxSlaClock | None:
    clock = ensure_clock(db, conversation, started_at=occurred_at)
    if clock is None:
        return None
    when = (occurred_at or datetime.now(UTC)).astimezone(UTC)
    if (
        clock.first_response_at is not None
        and clock.next_response_due_at is not None
        and clock.next_response_at is None
    ):
        return clock
    if clock.first_response_at is not None:
        policy = db.get(InboxSlaPolicy, clock.policy_id)
        rule = db.get(InboxSlaRule, clock.rule_id)
        if policy and rule and rule.next_response_minutes:
            clock.next_response_due_at = _due(when, rule.next_response_minutes, policy)
            clock.next_response_at = None
            _record(
                db, clock, "next_response_started", f"next:{when.isoformat()}", when
            )
    db.flush()
    return clock


def is_eligible_human_response(message: InboxMessage) -> bool:
    if message.direction != InboxMessageDirection.outbound.value:
        return False
    metadata = message.metadata_ if isinstance(message.metadata_, dict) else {}
    sender_type = str(
        metadata.get("sender_type") or metadata.get("author_type") or "agent"
    ).lower()
    return metadata.get("sent_by_person_id") is not None or sender_type in {
        "agent",
        "human",
        "staff",
    }


def record_outbound(
    db: Session, message: InboxMessage, *, occurred_at: datetime | None = None
) -> InboxSlaClock | None:
    if not is_eligible_human_response(message):
        return None
    clock = (
        db.query(InboxSlaClock)
        .filter(InboxSlaClock.conversation_id == message.conversation_id)
        .with_for_update()
        .first()
    )
    if clock is None:
        conversation = db.get(InboxConversation, message.conversation_id)
        clock = ensure_clock(db, conversation) if conversation else None
    if clock is None:
        return None
    when = (occurred_at or message.sent_at or message.created_at).astimezone(UTC)
    if clock.first_response_at is None:
        clock.first_response_at = when
        _record(db, clock, "first_response", f"first_response:{message.id}", when)
    if clock.next_response_due_at is not None and clock.next_response_at is None:
        clock.next_response_at = when
        _record(db, clock, "next_response", f"next_response:{message.id}", when)
    db.flush()
    return clock


def update_status(
    db: Session,
    conversation: InboxConversation,
    new_status: str,
    *,
    occurred_at: datetime | None = None,
) -> InboxSlaClock | None:
    clock = (
        db.query(InboxSlaClock)
        .filter(InboxSlaClock.conversation_id == conversation.id)
        .with_for_update()
        .first()
    )
    if clock is None:
        return None
    when = (occurred_at or datetime.now(UTC)).astimezone(UTC)
    if new_status == InboxConversationStatus.resolved.value:
        clock.resolved_at = when
        clock.status = STATUS_COMPLETED
        _record(db, clock, "resolved", f"resolved:{when.isoformat()}", when)
    elif (
        new_status != InboxConversationStatus.resolved.value
        and clock.resolved_at is not None
    ):
        clock.resolved_at = None
        clock.status = STATUS_RUNNING
        _record(db, clock, "reopened", f"reopened:{when.isoformat()}", when)
    db.flush()
    return clock


def evaluate_clock(
    db: Session, clock_id: UUID, *, now: datetime | None = None
) -> str | None:
    clock = (
        db.query(InboxSlaClock)
        .filter(InboxSlaClock.id == clock_id)
        .with_for_update()
        .first()
    )
    if clock is None:
        return None
    current = (now or datetime.now(UTC)).astimezone(UTC)
    clock.last_evaluated_at = current
    if clock.resolved_at is not None:
        clock.status = STATUS_COMPLETED
        db.flush()
        return clock.status
    if clock.paused_at is not None:
        clock.status = STATUS_PAUSED
        db.flush()
        return clock.status

    def utc(value: datetime) -> datetime:
        return (
            value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        )

    overdue = (
        clock.first_response_at is None
        and current >= utc(clock.first_response_due_at)
        or clock.next_response_due_at is not None
        and clock.next_response_at is None
        and current >= utc(clock.next_response_due_at)
        or current >= utc(clock.resolution_due_at)
    )
    rule = db.get(InboxSlaRule, clock.rule_id)
    warning = bool(
        rule
        and clock.first_response_at is None
        and current
        >= utc(clock.first_response_due_at) - timedelta(minutes=rule.warning_minutes)
    )
    if overdue:
        if clock.status != STATUS_BREACHED:
            clock.status = STATUS_BREACHED
            _record(
                db, clock, "breached", f"breached:{current.date().isoformat()}", current
            )
    elif warning:
        clock.status = STATUS_WARNING
        if clock.warning_sent_at is None:
            clock.warning_sent_at = current
            _record(
                db, clock, "warning", f"warning:{current.date().isoformat()}", current
            )
    else:
        clock.status = STATUS_RUNNING
    db.flush()
    return clock.status


@dataclass(frozen=True, slots=True)
class SlaRuleView:
    id: UUID
    service_team_id: UUID | None
    channel_type: str | None
    priority: int | None
    first_response_minutes: int
    next_response_minutes: int | None
    resolution_minutes: int
    warning_minutes: int
    is_active: bool


@dataclass(frozen=True, slots=True)
class SlaPolicyView:
    id: UUID
    name: str
    description: str | None
    timezone: str
    working_days: tuple[int, ...]
    workday_start: time
    workday_end: time
    holidays: tuple[date, ...]
    is_active: bool
    is_default: bool
    rules: tuple[SlaRuleView, ...]


def _policy_view(policy: InboxSlaPolicy) -> SlaPolicyView:
    return SlaPolicyView(
        id=policy.id,
        name=policy.name,
        description=policy.description,
        timezone=policy.timezone,
        working_days=tuple(policy.working_days),
        workday_start=policy.workday_start,
        workday_end=policy.workday_end,
        holidays=tuple(date.fromisoformat(value) for value in policy.holidays or ()),
        is_active=policy.is_active,
        is_default=policy.is_default,
        rules=tuple(
            SlaRuleView(
                id=rule.id,
                service_team_id=rule.service_team_id,
                channel_type=rule.channel_type,
                priority=rule.priority,
                first_response_minutes=rule.first_response_minutes,
                next_response_minutes=rule.next_response_minutes,
                resolution_minutes=rule.resolution_minutes,
                warning_minutes=rule.warning_minutes,
                is_active=rule.is_active,
            )
            for rule in policy.rules
        ),
    )


@dataclass(frozen=True, slots=True)
class SlaPolicyQuery:
    policy_id: UUID | None = None


def query_policies(db: Session, query: SlaPolicyQuery) -> tuple[SlaPolicyView, ...]:
    policies = db.query(InboxSlaPolicy).order_by(InboxSlaPolicy.name)
    if query.policy_id is not None:
        policies = policies.filter(InboxSlaPolicy.id == query.policy_id)
    result = tuple(_policy_view(policy) for policy in policies.all())
    if query.policy_id is not None and not result:
        raise _error("not_found", "Inbox SLA policy not found")
    return result


@dataclass(frozen=True, slots=True)
class SaveSlaPolicyCommand:
    context: CommandContext
    actor: AuditActor
    policy: SlaPolicyInput


@dataclass(frozen=True, slots=True)
class ActivateSlaPolicyCommand:
    context: CommandContext
    actor: AuditActor
    policy_id: UUID
    active: bool


def _lock_policy_configuration(db: Session) -> None:
    # Serializes default selection even when there are no policy rows yet.
    # SQLite is only the non-authoritative unit lane.
    if db.get_bind().dialect.name == "postgresql":
        db.execute(select(func.pg_advisory_xact_lock(783241906)))


def _record_policy_change(
    db: Session,
    *,
    policy: InboxSlaPolicy,
    context: CommandContext,
    actor: AuditActor,
) -> None:
    from app.services.events import emit_event
    from app.services.events.types import EventType

    stage_audit_event(
        db,
        action="inbox.sla.policy_changed",
        entity_type="inbox_sla_policy",
        entity_id=str(policy.id),
        actor=actor,
        request_id=str(context.command_id),
        metadata={"is_active": policy.is_active, "is_default": policy.is_default},
    )
    emit_event(
        db,
        EventType.inbox_sla_policy_changed,
        {
            "policy_id": str(policy.id),
            "is_active": policy.is_active,
            "is_default": policy.is_default,
            "command_id": str(context.command_id),
        },
        actor=context.actor,
    )


def _authorize_policy_change(context: CommandContext, actor: AuditActor) -> None:
    if context.scope != "support:ticket:update" or context.actor != actor.actor_id:
        raise _error(
            "forbidden", "SLA policy changes require an authorized administrator"
        )


def save_policy(db: Session, command: SaveSlaPolicyCommand) -> SlaPolicyView:
    def operation() -> SlaPolicyView:
        _authorize_policy_change(command.context, command.actor)
        _lock_policy_configuration(db)
        policy = _create_policy(db, command=command.policy)
        _record_policy_change(
            db, policy=policy, context=command.context, actor=command.actor
        )
        return _policy_view(policy)

    return execute_owner_command(
        db,
        definition=_POLICY_COMMAND,
        context=command.context,
        operation=operation,
    )


def activate_policy(db: Session, command: ActivateSlaPolicyCommand) -> SlaPolicyView:
    def operation() -> SlaPolicyView:
        _authorize_policy_change(command.context, command.actor)
        _lock_policy_configuration(db)
        policy = (
            db.query(InboxSlaPolicy)
            .filter(
                InboxSlaPolicy.id == command.policy_id,
            )
            .with_for_update()
            .one_or_none()
        )
        if policy is None:
            raise _error("not_found", "Inbox SLA policy not found")
        if policy.is_active != command.active:
            policy.is_active = command.active
            if not command.active:
                policy.is_default = False
            db.flush()
            _record_policy_change(
                db, policy=policy, context=command.context, actor=command.actor
            )
        return _policy_view(policy)

    return execute_owner_command(
        db,
        definition=_POLICY_COMMAND,
        context=command.context,
        operation=operation,
    )


@dataclass(frozen=True, slots=True)
class EvaluateSlaCommand:
    context: CommandContext
    now: datetime
    limit: int = 500


@dataclass(frozen=True, slots=True)
class SlaEvaluationOutcome:
    checked: int
    warning: int
    breached: int
    completed: int
    paused: int


def evaluate_due_clocks(
    db: Session, command: EvaluateSlaCommand
) -> SlaEvaluationOutcome:
    def operation() -> SlaEvaluationOutcome:
        clocks = (
            db.query(InboxSlaClock)
            .filter(
                InboxSlaClock.status.in_(
                    (STATUS_RUNNING, STATUS_WARNING, STATUS_PAUSED)
                )
            )
            .order_by(
                InboxSlaClock.last_evaluated_at.asc().nullsfirst(), InboxSlaClock.id
            )
            .with_for_update(skip_locked=True)
            .limit(max(1, min(command.limit, 5000)))
            .all()
        )
        # Include pre-deadline clocks: their warning window may already be open.
        statuses = tuple(
            evaluate_clock(db, clock_id=clock.id, now=command.now) for clock in clocks
        )
        return SlaEvaluationOutcome(
            checked=len(statuses),
            warning=statuses.count(STATUS_WARNING),
            breached=statuses.count(STATUS_BREACHED),
            completed=statuses.count(STATUS_COMPLETED),
            paused=statuses.count(STATUS_PAUSED),
        )

    return execute_owner_command(
        db,
        definition=_POLICY_COMMAND,
        context=command.context,
        operation=operation,
    )
