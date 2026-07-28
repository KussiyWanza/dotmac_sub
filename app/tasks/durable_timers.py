"""Thin driver for `runtime.durable_timers` due-timer dispatch (ADR 0007 §7).

The task makes no business decision: it enters the timer owner's fire
command, which emits each due timer's declared trigger event and records the
fired transition atomically. Consumers reject stale generations themselves.
Due-timer dispatch is permanent lifecycle infrastructure (ADR 0007
invariant 23): its cadence may be configured, but it cannot be disabled.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.durable_timers.fire_due_durable_timers")
def fire_due_durable_timers(*, batch_limit: int = 200) -> dict[str, int]:
    from app.services.owner_commands import CommandContext
    from app.services.runtime_durable_timers import fire_due_timers

    now = datetime.now(UTC)
    with db_session_adapter.owner_command_session() as session:
        fired = fire_due_timers(
            session,
            now=now,
            context=CommandContext.system(
                actor="task:durable-timer-dispatch",
                scope="runtime.durable_timers:dispatch",
                reason="emit due durable-timer triggers",
                idempotency_key=f"fire-due-timers:{now.isoformat()}",
            ),
            batch_limit=batch_limit,
        )
    result = {"fired": len(fired)}
    logger.info(
        "durable timer dispatch complete",
        extra={"event": "durable_timer_dispatch", **result},
    )
    return result
