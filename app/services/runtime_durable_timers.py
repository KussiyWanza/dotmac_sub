"""`runtime.durable_timers` — exact time triggers (ADR 0007 Phase 5).

Two halves, matching the ADR:

**Staging** is a required flush-only participant. The owning business
transition calls :func:`schedule_timer` / :func:`cancel_timer` inside its own
``execute_owner_command`` transaction, so a transition that requires a future
action cannot commit without its timer. Scheduling replaces the previous
generation; a bumped generation makes any in-flight stale delivery harmless.

**Firing** is this service's own owner command. :func:`fire_due_timers` scans
the indexed timer table for ``due_at <= now`` with a bounded batch, marks each
timer fired, and emits only the declared trigger event carrying the timer's
identity and generation. It performs no customer, invoice, funding, or access
decision — the consumer that declared the timer revalidates its own state.

This replaces business-wide sweeps that reconstruct which transition should
have been scheduled.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.durable_timer import DurableTimer, TimerStatus
from app.services.domain_errors import DomainError
from app.services.events.dispatcher import emit_event
from app.services.events.types import EventType
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
    owner_command_active,
)

OWNER = "runtime.durable_timers"

_FIRE_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="due-timer trigger emission",
    name="fire_due_timers",
)

# Timer triggers ride the generic event type until the billing event
# vocabulary lands in EventType; the payload names the exact output.
_TRIGGER_EVENT = EventType.custom


class DurableTimerError(DomainError):
    """Fail-closed durable-timer error."""


def _error(suffix: str, message: str, **details: object) -> DurableTimerError:
    return DurableTimerError(
        code=f"{OWNER}.{suffix}", message=message, details=dict(details)
    )


@dataclass(frozen=True)
class ScheduleTimerCommand:
    """Typed request to create or replace one owner's timer."""

    owner: str
    entity_kind: str
    entity_id: UUID
    purpose: str
    due_at: datetime
    output_event_type: str
    expected_source_version: int = 1


def _require_participant(db: Session, *, action: str) -> None:
    if not owner_command_active(db):
        raise _error(
            "timer_requires_owner_command",
            "Timers are staged only inside the owning transition's command.",
            action=action,
        )


def _current_timer(
    db: Session, *, owner: str, entity_kind: str, entity_id: UUID, purpose: str
) -> DurableTimer | None:
    return db.execute(
        select(DurableTimer)
        .where(
            DurableTimer.owner == owner,
            DurableTimer.entity_kind == entity_kind,
            DurableTimer.entity_id == entity_id,
            DurableTimer.purpose == purpose,
            DurableTimer.status == TimerStatus.scheduled,
        )
        .with_for_update()
    ).scalar_one_or_none()


def schedule_timer(
    db: Session,
    command: ScheduleTimerCommand,
    *,
    context: CommandContext,
) -> DurableTimer:
    """Create or replace the current timer for (owner, entity, purpose).

    Flush-only participant. Replacement supersedes the previous generation and
    bumps the new one, making stale deliveries idempotently rejectable.
    """

    _require_participant(db, action="schedule")
    if command.due_at.tzinfo is None:
        raise _error(
            "invalid_timer_due_at",
            "Timer due time must be timezone-aware.",
        )
    if not command.output_event_type.strip():
        raise _error(
            "invalid_timer_output",
            "A timer must declare its output event type.",
        )

    current = _current_timer(
        db,
        owner=command.owner,
        entity_kind=command.entity_kind,
        entity_id=command.entity_id,
        purpose=command.purpose,
    )
    generation = 1
    if current is not None:
        current.status = TimerStatus.superseded
        generation = current.generation + 1
        db.flush()

    timer = DurableTimer(
        owner=command.owner,
        entity_kind=command.entity_kind,
        entity_id=command.entity_id,
        purpose=command.purpose,
        generation=generation,
        due_at=command.due_at,
        expected_source_version=command.expected_source_version,
        output_event_type=command.output_event_type,
        command_id=context.command_id,
        correlation_id=context.correlation_id,
    )
    db.add(timer)
    db.flush()
    return timer


def cancel_timer(
    db: Session,
    *,
    owner: str,
    entity_kind: str,
    entity_id: UUID,
    purpose: str,
) -> bool:
    """Cancel the current timer, if any. Flush-only participant, idempotent."""

    _require_participant(db, action="cancel")
    current = _current_timer(
        db, owner=owner, entity_kind=entity_kind, entity_id=entity_id, purpose=purpose
    )
    if current is None:
        return False
    current.status = TimerStatus.canceled
    db.flush()
    return True


def current_timer(
    db: Session,
    *,
    owner: str,
    entity_kind: str,
    entity_id: UUID,
    purpose: str,
) -> DurableTimer | None:
    """Read-only: the current scheduled timer for (owner, entity, purpose)."""

    return db.execute(
        select(DurableTimer).where(
            DurableTimer.owner == owner,
            DurableTimer.entity_kind == entity_kind,
            DurableTimer.entity_id == entity_id,
            DurableTimer.purpose == purpose,
            DurableTimer.status == TimerStatus.scheduled,
        )
    ).scalar_one_or_none()


@dataclass(frozen=True)
class FiredTimer:
    """One trigger this run emitted."""

    timer_id: UUID
    owner: str
    entity_id: UUID
    purpose: str
    generation: int
    output_event_type: str
    event_id: UUID


def fire_due_timers(
    db: Session,
    *,
    now: datetime,
    context: CommandContext,
    batch_limit: int = 200,
) -> tuple[FiredTimer, ...]:
    """Emit the declared trigger for every timer due at ``now``.

    Bounded, indexed, and decision-free: the only writes are the timers'
    ``fired`` transitions and the staged trigger events, which commit
    atomically. Consumers reject a stale generation themselves.
    """

    if now.tzinfo is None:
        raise _error(
            "invalid_timer_due_at",
            "The due scan requires a timezone-aware instant.",
        )

    return execute_owner_command(
        db,
        definition=_FIRE_COMMAND,
        context=context,
        operation=lambda: _fire_due(
            db, now=now, context=context, batch_limit=batch_limit
        ),
    )


def _fire_due(
    db: Session,
    *,
    now: datetime,
    context: CommandContext,
    batch_limit: int,
) -> tuple[FiredTimer, ...]:
    due = list(
        db.execute(
            select(DurableTimer)
            .where(
                DurableTimer.status == TimerStatus.scheduled,
                DurableTimer.due_at <= now,
            )
            .order_by(DurableTimer.due_at)
            .limit(batch_limit)
            .with_for_update(skip_locked=True)
        ).scalars()
    )

    fired: list[FiredTimer] = []
    for timer in due:
        event = emit_event(
            db,
            _TRIGGER_EVENT,
            {
                "trigger": timer.output_event_type,
                "timer_id": str(timer.id),
                "timer_owner": timer.owner,
                "entity_kind": timer.entity_kind,
                "entity_id": str(timer.entity_id),
                "purpose": timer.purpose,
                "generation": timer.generation,
                "expected_source_version": timer.expected_source_version,
                "due_at": timer.due_at.isoformat(),
            },
            actor=context.actor,
        )
        timer.status = TimerStatus.fired
        timer.fired_at = now
        timer.fired_event_id = event.event_id
        fired.append(
            FiredTimer(
                timer_id=timer.id,
                owner=timer.owner,
                entity_id=timer.entity_id,
                purpose=timer.purpose,
                generation=timer.generation,
                output_event_type=timer.output_event_type,
                event_id=event.event_id,
            )
        )
    db.flush()
    return tuple(fired)


__all__ = [
    "DurableTimerError",
    "FiredTimer",
    "ScheduleTimerCommand",
    "cancel_timer",
    "current_timer",
    "fire_due_timers",
    "schedule_timer",
]
