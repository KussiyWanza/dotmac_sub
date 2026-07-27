"""Behavior coverage for `runtime.durable_timers` (ADR 0007 Phase 5)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.durable_timer import DurableTimer, TimerStatus
from app.models.event_store import EventStore
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.runtime_durable_timers import (
    DurableTimerError,
    ScheduleTimerCommand,
    cancel_timer,
    current_timer,
    fire_due_timers,
    schedule_timer,
)

NOW = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)

# A real contracted owner hosts the participant staging, exactly as an owning
# business transition would.
_HOST_COMMAND = OwnerCommandDefinition(
    owner="billing.contracts",
    concern="versioned billing contract terms",
    name="record_billing_contract_version",
)


def _context() -> CommandContext:
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor="user:pytest",
        scope="durable-timer:test",
        reason="pytest durable timers",
        idempotency_key=f"pytest:{command_id}",
    )


def _in_owner_command(db, operation):
    return execute_owner_command(
        db, definition=_HOST_COMMAND, context=_context(), operation=operation
    )


def _schedule(db, *, entity_id, due_at, purpose="renewal"):
    context = _context()
    return _in_owner_command(
        db,
        lambda: schedule_timer(
            db,
            ScheduleTimerCommand(
                owner="billing.obligations",
                entity_kind="billing_obligation",
                entity_id=entity_id,
                purpose=purpose,
                due_at=due_at,
                output_event_type="billing.obligation_timer_due",
            ),
            context=context,
        ),
    )


def test_scheduling_outside_an_owner_command_fails_closed(db_session):
    with pytest.raises(DurableTimerError) as excinfo:
        schedule_timer(
            db_session,
            ScheduleTimerCommand(
                owner="billing.obligations",
                entity_kind="billing_obligation",
                entity_id=uuid4(),
                purpose="renewal",
                due_at=NOW,
                output_event_type="billing.obligation_timer_due",
            ),
            context=_context(),
        )

    assert excinfo.value.code == ("runtime.durable_timers.timer_requires_owner_command")


def test_rescheduling_supersedes_and_bumps_the_generation(db_session):
    entity_id = uuid4()
    first = _schedule(db_session, entity_id=entity_id, due_at=NOW)
    first_id = first.id
    db_session.commit()

    second = _schedule(db_session, entity_id=entity_id, due_at=NOW + timedelta(days=1))

    assert second.generation == 2
    old = db_session.get(DurableTimer, first_id)
    assert old.status is TimerStatus.superseded
    live = current_timer(
        db_session,
        owner="billing.obligations",
        entity_kind="billing_obligation",
        entity_id=entity_id,
        purpose="renewal",
    )
    assert live.id == second.id


def test_cancel_is_idempotent(db_session):
    entity_id = uuid4()
    _schedule(db_session, entity_id=entity_id, due_at=NOW)
    db_session.commit()

    context = _context()

    def cancel_twice():
        first = cancel_timer(
            db_session,
            owner="billing.obligations",
            entity_kind="billing_obligation",
            entity_id=entity_id,
            purpose="renewal",
        )
        second = cancel_timer(
            db_session,
            owner="billing.obligations",
            entity_kind="billing_obligation",
            entity_id=entity_id,
            purpose="renewal",
        )
        return first, second

    first, second = _in_owner_command(db_session, cancel_twice)

    assert first is True
    assert second is False


def test_firing_emits_only_the_declared_trigger_and_marks_the_timer(db_session):
    entity_id = uuid4()
    timer = _schedule(db_session, entity_id=entity_id, due_at=NOW)
    timer_id = timer.id
    db_session.commit()

    fired = fire_due_timers(
        db_session, now=NOW + timedelta(minutes=1), context=_context()
    )

    assert len(fired) == 1
    assert fired[0].timer_id == timer_id
    assert fired[0].output_event_type == "billing.obligation_timer_due"

    record = db_session.get(DurableTimer, timer_id)
    assert record.status is TimerStatus.fired
    assert record.fired_event_id == fired[0].event_id

    event = db_session.execute(
        select(EventStore).where(EventStore.event_id == fired[0].event_id)
    ).scalar_one()
    payload = event.payload
    # The trigger names the declared output and generation; it carries no
    # customer, invoice, funding, or access decision.
    assert payload["trigger"] == "billing.obligation_timer_due"
    assert payload["generation"] == 1
    assert payload["entity_id"] == str(entity_id)


def test_a_timer_that_is_not_due_does_not_fire(db_session):
    entity_id = uuid4()
    _schedule(db_session, entity_id=entity_id, due_at=NOW + timedelta(days=2))
    db_session.commit()

    fired = fire_due_timers(db_session, now=NOW, context=_context())

    assert fired == ()


def test_a_superseded_timer_never_fires(db_session):
    """Stale generations are inert: only the current generation is due."""

    entity_id = uuid4()
    _schedule(db_session, entity_id=entity_id, due_at=NOW)
    db_session.commit()
    _schedule(db_session, entity_id=entity_id, due_at=NOW + timedelta(days=30))
    db_session.commit()

    fired = fire_due_timers(
        db_session, now=NOW + timedelta(minutes=1), context=_context()
    )

    assert fired == ()


def test_the_due_scan_is_bounded(db_session):
    for _ in range(3):
        _schedule(db_session, entity_id=uuid4(), due_at=NOW)
        db_session.commit()

    fired = fire_due_timers(
        db_session,
        now=NOW + timedelta(minutes=1),
        context=_context(),
        batch_limit=2,
    )

    assert len(fired) == 2
    remaining = fire_due_timers(
        db_session,
        now=NOW + timedelta(minutes=1),
        context=_context(),
        batch_limit=2,
    )
    assert len(remaining) == 1
