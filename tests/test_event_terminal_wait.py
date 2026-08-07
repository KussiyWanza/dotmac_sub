"""Exact durable-event terminal verification for operator commands."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from app.models.event_store import EventStatus, EventStore
from app.services.event_store import (
    EventTerminalDisposition,
    WaitForEventTerminalQuery,
    wait_for_event_terminal,
)
from app.services.events.types import EventType


def test_terminal_wait_never_substitutes_a_newer_event(db_session) -> None:
    expected_event_id = uuid4()
    db_session.add_all(
        [
            EventStore(
                event_id=expected_event_id,
                event_type=EventType.ip_assignment_served_projection_repaired.value,
                payload={"preview_fingerprint": "expected"},
                status=EventStatus.failed,
                retry_count=0,
                failed_handlers=[{"handler": "ip_assignment_projection"}],
            ),
            EventStore(
                event_id=uuid4(),
                event_type=EventType.ip_assignment_served_projection_repaired.value,
                payload={"preview_fingerprint": "newer"},
                status=EventStatus.completed,
                retry_count=0,
            ),
        ]
    )
    db_session.commit()

    outcome = wait_for_event_terminal(
        db_session,
        WaitForEventTerminalQuery(
            event_id=expected_event_id,
            event_type=EventType.ip_assignment_served_projection_repaired,
            timeout=timedelta(seconds=1),
        ),
    )

    assert outcome.event_id == expected_event_id
    assert outcome.disposition is EventTerminalDisposition.failed
    assert outcome.failed_handlers == ("ip_assignment_projection",)
