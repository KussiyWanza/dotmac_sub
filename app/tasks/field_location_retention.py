"""Scheduled adapter for detailed field-location history retention."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Protocol
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy.exc import OperationalError

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter
from app.services.field.location_retention import (
    PruneFieldLocationHistoryCommand,
    prune_field_location_history,
)
from app.services.owner_commands import CommandContext
from app.services.observability import (
    StateObservation,
    publish_state_snapshot,
    record_task_run,
)

logger = logging.getLogger(__name__)


class _TaskRequest(Protocol):
    id: str | None


class _BoundTask(Protocol):
    request: _TaskRequest


@celery_app.task(
    bind=True,
    name="app.tasks.field_location_retention.prune_field_location_history",
    autoretry_for=(OperationalError,),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    retry_kwargs={"max_retries": 3},
)
def prune_field_location_history_task(
    self: _BoundTask,
) -> dict[str, int | str | bool]:
    request_id = str(self.request.id or uuid4())
    command_id = uuid5(
        NAMESPACE_URL,
        f"dotmac:field-location-retention:{request_id}",
    )
    command = PruneFieldLocationHistoryCommand(
        context=CommandContext.system(
            actor="celery:field_location_retention",
            scope="field:global",
            reason="scheduled 30-day detailed GPS retention",
            command_id=command_id,
            idempotency_key=f"celery:{request_id}",
        ),
        as_of=datetime.now(UTC),
    )
    try:
        with db_session_adapter.owner_command_session() as db:
            outcome = prune_field_location_history(db, command)
    except Exception:
        publish_state_snapshot(
            "field_location_retention",
            (StateObservation(signal="failure", scope="global", value=1),),
            status="error",
        )
        record_task_run(
            "app.tasks.field_location_retention.prune_field_location_history",
            status="error",
            counters={"failure": 1},
        )
        logger.exception(
            "field_location_history_retention_failed",
            extra={
                "event": "field_location_history_retention_failed",
                "command_id": str(command.context.command_id),
            },
        )
        raise
    publish_state_snapshot(
        "field_location_retention",
        (
            StateObservation(
                signal="deleted_rows",
                scope="global",
                value=outcome.deleted_count,
            ),
            StateObservation(
                signal="batch_limit_reached",
                scope="global",
                value=1 if outcome.batch_limit_reached else 0,
            ),
        ),
        status="degraded" if outcome.batch_limit_reached else "ok",
    )
    record_task_run(
        "app.tasks.field_location_retention.prune_field_location_history",
        status="success",
        counters={
            "deleted_count": outcome.deleted_count,
            "batch_limit_reached": outcome.batch_limit_reached,
        },
    )
    if outcome.batch_limit_reached:
        logger.warning(
            "field_location_history_retention_backlog_detected",
            extra={
                "event": "field_location_history_retention_backlog_detected",
                "command_id": str(outcome.command_id),
                "cutoff": outcome.cutoff.isoformat(),
                "deleted_count": outcome.deleted_count,
            },
        )
    logger.info(
        "field_location_history_retention_completed",
        extra={
            "event": "field_location_history_retention_completed",
            "command_id": str(outcome.command_id),
            "cutoff": outcome.cutoff.isoformat(),
            "deleted_count": outcome.deleted_count,
            "batch_limit_reached": outcome.batch_limit_reached,
        },
    )
    return {
        "command_id": str(outcome.command_id),
        "cutoff": outcome.cutoff.isoformat(),
        "deleted_count": outcome.deleted_count,
        "batch_limit_reached": outcome.batch_limit_reached,
    }
