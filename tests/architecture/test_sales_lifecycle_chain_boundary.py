"""Boundary guards for the sales → service delivery owner-output chain.

The chain contract (docs/designs/SALES_TO_SERVICE_LIFECYCLE_SOT.md): each
owner stages its output event atomically with its transition; the registered
``SalesLifecycleProjectionHandler`` applies the consequence after commit with
durable retry; a consequence that cannot be applied stays a failed retryable
delivery. These source guards keep the retired parallel paths out.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_funding_consequences_are_event_chained_not_swallowed():
    src = _source("app/services/sales_orders.py")
    # The swallowed post-commit fan-out is retired.
    assert "_push_sales_order_subscriptions" not in src
    assert "sales_order_subscription_sync_failed" not in src
    # The funding edge stages its durable output atomically.
    assert "EventType.sales_order_funding_satisfied" in src
    assert "def stage_funding_transition" in src
    assert "def apply_funding_consequences" in src
    # The consumer converts an unresolved offer into a typed failure instead
    # of silently skipping the service line.
    assert "funding_consequence_unresolved" in src


def test_producers_stage_funding_output_on_every_paid_edge():
    sales_orders = _source("app/services/sales_orders.py")
    # create, update, and line-recalculation paths all stage the edge.
    assert sales_orders.count("stage_funding_transition(") >= 4
    selfserve = _source("app/services/sales/selfserve.py")
    # The self-serve deposit path crosses the same edge without recording a
    # second order payment.
    assert "stage_funding_transition(" in selfserve
    assert "record_order_payment=False" in selfserve


def test_cx_owner_does_not_write_sales_state_inline():
    src = _source("app/services/customer_experience_handoffs.py")
    assert "fulfill_from_customer_experience" not in src
    assert "from app.services import sales_orders" not in src


def test_projection_handler_consumes_the_full_chain():
    src = _source("app/services/events/handlers/sales_lifecycle_projection.py")
    for event_name in (
        "sales_order_funding_satisfied",
        "vendor_project_verified",
        "service_order_released",
        "service_order_completed",
        "customer_experience_accepted",
    ):
        assert f"EventType.{event_name}" in src
    # Consequences must not be wrapped in a swallow: the handler module
    # delegates to owners and lets failures propagate to the dispatcher.
    assert "except Exception" not in src
    # Every sales.fulfillment hop runs through a receipted consumer command on
    # a fresh owner-command session.
    assert "_owner_session(" in src
    for consumer in (
        "consume_funding_satisfaction",
        "consume_verified_implementation",
        "consume_service_order_release",
        "consume_cx_acceptance",
    ):
        assert consumer in src


def test_fulfillment_consumers_are_receipted_owner_commands():
    src = _source("app/services/sales_fulfillment.py")
    assert "consume_owner_output" in src
    assert "execute_owner_command" in src
    assert 'concern="committed lifecycle output consumption"' in src or (
        '_CONSUME_CONCERN = "committed lifecycle output consumption"' in src
    )


def test_released_output_carries_sales_linkage():
    src = _source("app/services/service_order_lifecycle.py")
    released = src.split("EventType.service_order_released", 1)[1]
    assert '"sales_order_id"' in released.split("emit_event", 1)[0]


def test_provisioning_start_failures_stay_retryable():
    src = _source("app/services/events/handlers/provisioning.py")
    section = src.split("def _handle_service_order_assigned", 1)[1]
    # The provisioning-run start propagates failure so the event delivery
    # remains failed and retryable.
    tail = section.split("except Exception", 1)[1]
    assert "raise" in tail
