"""Typed operational task outcome observations."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum


class OperationalEventName(StrEnum):
    """Closed event names for task summaries that are safe to aggregate."""

    BILLING_ENFORCEMENT_COMPLETED = "billing_enforcement_completed"
    NOTIFICATION_QUEUE_PROCESSED = "notification_queue_processed"
    ERP_SYNC_EVENTS_COMPLETED = "erp_sync_events_completed"
    ERP_EXPENSE_STATUS_REFRESH_COMPLETED = "erp_expense_status_refresh_completed"
    ERP_MATERIAL_STATUS_REFRESH_COMPLETED = "erp_material_status_refresh_completed"
    ERP_PURCHASE_INVOICE_STATUS_REFRESH_COMPLETED = (
        "erp_purchase_invoice_status_refresh_completed"
    )


class OperationalOutcome(StrEnum):
    COMPLETED = "completed"
    COMPLETED_WITH_RETRIES = "completed_with_retries"


@dataclass(frozen=True)
class OperationalLogEvent:
    """One compact task-summary event, without message-text classification."""

    name: OperationalEventName
    outcome: OperationalOutcome
    component: str
    counters: Mapping[str, int]


def log_operational_event(
    event_logger: logging.Logger, event: OperationalLogEvent
) -> None:
    """Emit one structured INFO record for a completed operational task.

    The event name and outcome are closed vocabulary.  Counters explain a
    completed run but do not turn expected retries or domain refusals into an
    ERROR signal.
    """

    event_logger.info(
        "operational_task_outcome",
        extra={
            "event_name": event.name.value,
            "outcome": event.outcome.value,
            "component": event.component,
            "counters": dict(event.counters),
        },
    )
