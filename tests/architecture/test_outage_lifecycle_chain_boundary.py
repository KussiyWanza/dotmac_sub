"""Boundary guards for the network outage owner-output chain.

The chain contract (docs/designs/NETWORK_OUTAGE_RESPONSE_LIFECYCLE.md): every
incident transition stages its typed outage event atomically with the status
write; the registered ``OutageLifecycleProjectionHandler`` applies
cross-owner consequences after commit with durable retry; outage resolution
never closes support Tickets or WorkOrders.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_lifecycle_outputs_are_atomic_not_swallowed():
    src = _source("app/services/topology/outage.py")
    # The pre-commit, swallowed fan-out is retired: outputs stage with the
    # transition and dispatch after commit.
    assert "defer_until_commit=False" not in src
    assert "except Exception" not in src
    assert "outage_event_emit_failed" not in src
    # Both the typed lifecycle output and the legacy webhook fan-out stage.
    assert "emit_event(session, EventType(kind), payload, actor=actor)" in src
    assert "EventType.network_alert" in src


def test_transitions_never_apply_cross_owner_consequences_inline():
    src = _source("app/services/topology/outage.py")
    marker = "receipted lifecycle-output consumption"
    assert marker in src
    transitions = src.split(marker, 1)[0]
    consumers = src.split(marker, 1)[1]
    for consequence_call in (
        "ensure_outage_operations(",
        "ensure_outage_customer_watchers(",
        "plan_outage_escalations(",
        "cancel_entity_events(",
    ):
        # Consequences run only through the receipted consumer commands,
        # never inline inside a transition.
        assert consequence_call not in transitions
    for receipt_marker in (
        "consume_owner_output",
        "execute_owner_command",
        "def consume_outage_activation",
        "def consume_outage_termination",
    ):
        assert receipt_marker in consumers


def test_projection_handler_consumes_the_lifecycle_outputs():
    src = _source("app/services/events/handlers/outage_lifecycle_projection.py")
    for event_name in (
        "outage_created",
        "outage_confirmed",
        "outage_discarded",
        "outage_resolved",
    ):
        assert f"EventType.{event_name}" in src
    # Consequences must not be wrapped in a swallow: failures propagate so
    # the delivery stays failed and retryable.
    assert "except Exception" not in src
    # The handler is a delivery adapter over the receipted consumer
    # commands, each on a fresh owner-command session.
    assert "_owner_session(" in src
    assert "consume_outage_activation" in src
    assert "consume_outage_termination" in src


def test_handler_is_registered_and_scoped():
    dispatcher = _source("app/services/events/dispatcher.py")
    assert "OutageLifecycleProjectionHandler()" in dispatcher
    controls = _source("app/services/control_relationships.py")
    assert '"OutageLifecycleProjectionHandler"' in controls
    assert "outage_lifecycle_projection" in controls


def test_outage_resolution_never_touches_tickets_or_work_orders():
    # Recovery evidence is emitted; Support and Field owners transition
    # their own cases. Neither the lifecycle owner nor its projection
    # handler may import or reference the ticket/work-order domains.
    for relative in (
        "app/services/topology/outage.py",
        "app/services/events/handlers/outage_lifecycle_projection.py",
    ):
        src = _source(relative)
        assert "from app.models.work_order" not in src
        assert "from app.models.support" not in src
        assert "from app.services import support" not in src
        assert "from app.services.support" not in src
        assert "ticket_work_order_handoff" not in src


def test_typed_outage_event_types_are_declared():
    src = _source("app/services/events/types.py")
    for value in (
        'outage_created = "outage.created"',
        'outage_suspected = "outage.suspected"',
        'outage_confirmed = "outage.confirmed"',
        'outage_clearing = "outage.clearing"',
        'outage_reopened = "outage.reopened"',
        'outage_rerooted = "outage.rerooted"',
        'outage_discarded = "outage.discarded"',
        'outage_resolved = "outage.resolved"',
    ):
        assert value in src
