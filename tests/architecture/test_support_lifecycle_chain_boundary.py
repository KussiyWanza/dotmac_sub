"""Boundary guards for the Team Inbox → Ticket → WorkOrder owner chain.

The chain contract (docs/designs/TICKET_WORK_ORDER_HANDOFF_SOT.md): each
owner's committed transition stages its typed output atomically; the support
lifecycle projection handler delivers outputs and fired durable-timer
triggers to receipted consumers; field completion never changes ticket
status; snooze and resolution-grace timing are durable per-entity timers,
not business sweeps.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_field_owner_emits_output_instead_of_inline_ticket_projection():
    src = _source("app/services/field/transitions.py")
    assert "EventType.work_order_field_outcome_recorded" in src
    # The ticket-timeline projection is applied only by the receipted
    # consumer, never inline in the field owner's transaction.
    assert "stage_field_outcome" not in src


def test_field_outcome_consumer_is_receipted_and_never_closes_ticket():
    src = _source("app/services/ticket_work_order_handoff.py")
    assert "def consume_field_outcome" in src
    assert "consume_owner_output" in src
    assert 'concern="committed field outcome consumption"' in src
    # Field evidence never transitions the ticket.
    assert "ticket.status =" not in src
    assert "transition_ticket_status" not in src


def test_resolution_grace_is_a_durable_timer_with_receipted_consumer():
    src = _source("app/services/support.py")
    assert 'purpose="resolution_confirmation_due"' in src
    assert 'output_event_type="support.resolution_confirmation_due"' in src
    assert "def consume_resolution_confirmation_due" in src
    assert "consume_owner_output" in src


def test_conversation_snooze_is_a_durable_timer_with_receipted_consumer():
    commands = _source("app/services/team_inbox_commands.py")
    assert 'purpose="snooze_wake"' in commands
    assert 'output_event_type="team_inbox.snooze_wake"' in commands
    assert "def consume_snooze_wake" in commands
    assert "consume_owner_output" in commands


def test_timer_dispatch_is_permanent_infrastructure():
    scheduler = _source("app/services/scheduler_config.py")
    block_start = scheduler.index('name="durable_timer_dispatch_runner"')
    block = scheduler[block_start - 400 : block_start + 400]
    assert "enabled=True" in block
    tasks = _source("app/tasks/durable_timers.py")
    assert "fire_due_timers" in tasks


def test_projection_handler_routes_outputs_and_timer_triggers():
    src = _source("app/services/events/handlers/support_lifecycle_projection.py")
    assert "EventType.work_order_field_outcome_recorded" in src
    assert '"support.resolution_confirmation_due"' in src
    assert '"team_inbox.snooze_wake"' in src
    assert "_owner_session(" in src
    assert "except Exception" not in src
    dispatcher = _source("app/services/events/dispatcher.py")
    assert "SupportLifecycleProjectionHandler()" in dispatcher
    controls = _source("app/services/control_relationships.py")
    assert '"SupportLifecycleProjectionHandler"' in controls
