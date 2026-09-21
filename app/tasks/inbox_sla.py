"""Idempotent Inbox SLA evaluator."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from app.celery_app import celery_app
from app.services import inbox_sla
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.inbox_sla.evaluate_inbox_sla")
def evaluate_inbox_sla(*, limit: int = 500) -> dict[str, int]:
    command = inbox_sla.EvaluateSlaCommand(
        context=CommandContext.system(
            actor="inbox-sla-evaluator",
            scope="inbox-sla:evaluate",
            reason="Scheduled Inbox SLA evaluation",
        ),
        now=datetime.now(UTC),
        limit=limit,
    )
    with db_session_adapter.owner_command_session() as db:
        outcome = inbox_sla.evaluate_due_clocks(db, command=command)
    counts = {
        "checked": outcome.checked,
        "warning": outcome.warning,
        "breached": outcome.breached,
        "completed": outcome.completed,
        "paused": outcome.paused,
    }
    logger.info("inbox_sla_evaluation_complete", extra=counts)
    return counts
