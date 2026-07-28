"""Durable-timer behavior for the support chain (ADR 0007 §7).

The owning transition stages the timer atomically; ``fire_due_timers`` emits
the decision-free trigger; the support lifecycle projection handler routes
it to the receipted consumer, whose effect commits atomically with its
unique ``(consumer, event_id)`` receipt.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.models.durable_timer import DurableTimer
from app.models.owner_output import OwnerOutputReceipt
from app.models.subscriber import Subscriber
from app.models.support import Ticket, TicketStatus
from app.models.team_inbox import InboxConversation
from app.services import team_inbox_commands
from app.services.owner_commands import CommandContext
from app.services.runtime_durable_timers import fire_due_timers
from app.services.support import Tickets


def _fire(db, now):
    # End any read transaction: the fire command requires a
    # transaction-free session at entry. Never rollback here — unit-test
    # sessions join the fixture connection in rollback_only mode, and a
    # bare rollback erases all committed test state.
    db.commit()
    fired = fire_due_timers(
        db,
        now=now,
        context=CommandContext.system(
            actor="pytest",
            scope="runtime.durable_timers:dispatch",
            reason="test fire",
            idempotency_key=f"test-fire:{uuid.uuid4()}",
        ),
    )
    db.commit()
    return fired


def _subscriber(db) -> Subscriber:
    subscriber = Subscriber(
        first_name="Timer",
        last_name="Case",
        email=f"timer-{uuid.uuid4().hex}@example.com",
    )
    db.add(subscriber)
    db.commit()
    return subscriber


def test_resolution_grace_timer_auto_confirms_through_receipt(db_session):
    subscriber = _subscriber(db_session)
    ticket = Ticket(subscriber_id=subscriber.id, title="No internet")
    db_session.add(ticket)
    db_session.commit()

    Tickets.request_resolution_confirmation(
        db_session, str(ticket.id), actor_id=None, grace_hours=0
    )
    db_session.commit()

    timer = db_session.execute(
        select(DurableTimer).where(
            DurableTimer.purpose == "resolution_confirmation_due"
        )
    ).scalar_one()
    assert str(timer.entity_id) == str(ticket.id)

    fired = _fire(db_session, datetime.now(UTC) + timedelta(seconds=1))
    assert len(fired) == 1

    db_session.refresh(ticket)
    assert ticket.status == TicketStatus.closed.value
    receipt = db_session.execute(
        select(OwnerOutputReceipt).where(
            OwnerOutputReceipt.consumer == "support.ticket_lifecycle"
        )
    ).scalar_one()
    assert receipt.outcome.value == "succeeded"

    # A stale second firing (same due scan later) is impossible — the timer
    # is fired — and replaying the consumer is receipt-guarded upstream.
    assert _fire(db_session, datetime.now(UTC) + timedelta(hours=3)) == ()


def test_snooze_timer_wakes_conversation_through_receipt(db_session):
    conversation = InboxConversation(
        subject="Snoozed thread",
        status="open",
        channel_type="email",
    )
    db_session.add(conversation)
    db_session.commit()

    wake_at = datetime.now(UTC) + timedelta(seconds=2)
    team_inbox_commands.update_workflow(
        db_session,
        conversation_id=str(conversation.id),
        snooze_until=wake_at,
    )
    db_session.commit()
    db_session.refresh(conversation)
    assert conversation.status == "snoozed"
    timer = db_session.execute(
        select(DurableTimer).where(DurableTimer.purpose == "snooze_wake")
    ).scalar_one()
    assert str(timer.entity_id) == str(conversation.id)

    time.sleep(3)
    fired = _fire(db_session, datetime.now(UTC))
    assert len(fired) == 1

    db_session.refresh(conversation)
    assert conversation.status == "open"
    assert conversation.snoozed_until is None
    receipt = db_session.execute(
        select(OwnerOutputReceipt).where(
            OwnerOutputReceipt.consumer == "communications.team_inbox_commands"
        )
    ).scalar_one()
    assert receipt.outcome.value == "succeeded"


def test_sla_clock_breach_fires_through_durable_timer(db_session):
    from app.models.ticket_workflow import (
        SlaBreach,
        SlaClock,
        SlaClockStatus,
        SlaPolicy,
        WorkflowEntityType,
    )

    db_session.add(
        SlaPolicy(
            name="Ticket Resolution SLA",
            entity_type=WorkflowEntityType.ticket.value,
            description="Default ticket SLA policy",
            is_active=True,
        )
    )
    db_session.commit()

    subscriber = _subscriber(db_session)
    from app.models.ticket_workflow import SlaClock as _SlaClock
    from app.schemas.support import TicketCreate

    # Create through the owner command so the clock stages its timer
    # atomically with the ticket transition.
    ticket = Tickets.create(
        db_session,
        TicketCreate(
            subscriber_id=subscriber.id,
            title="Slow repair",
            priority="urgent",
            due_at=datetime.now(UTC) - timedelta(minutes=5),
        ),
    )
    db_session.commit()
    clock = db_session.query(_SlaClock).filter(_SlaClock.entity_id == ticket.id).one()

    timer = db_session.execute(
        select(DurableTimer).where(DurableTimer.purpose == "sla_breach_due")
    ).scalar_one()
    assert str(timer.entity_id) == str(clock.id)

    fired = _fire(db_session, datetime.now(UTC) + timedelta(seconds=1))
    assert len(fired) == 1

    db_session.expire_all()
    clock = db_session.get(SlaClock, clock.id)
    assert clock.status == SlaClockStatus.breached.value
    assert db_session.query(SlaBreach).count() == 1
    receipt = (
        db_session.query(OwnerOutputReceipt)
        .filter(
            OwnerOutputReceipt.consumer == "support.ticket_lifecycle",
            OwnerOutputReceipt.event_type == "support.ticket_sla_breach_due",
        )
        .one()
    )
    assert receipt.outcome.value == "succeeded"
