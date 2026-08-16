"""Scheduled reporting tasks."""

import logging
import time

from app.celery_app import celery_app
from app.services import ncc_report_email
from app.services.db_session_adapter import db_session_adapter
from app.services.observability import record_task_run

logger = logging.getLogger(__name__)

_NCC_EMAIL_TASK = "app.tasks.reports.send_scheduled_ncc_report"


@celery_app.task(name=_NCC_EMAIL_TASK)
def send_scheduled_ncc_report() -> dict[str, object]:
    """Thin adapter for the owner-arbitrated Tuesday NCC delivery."""
    started = time.monotonic()
    try:
        with db_session_adapter.owner_command_session() as session:
            outcome = ncc_report_email.run_scheduled_ncc_report_email(db=session)
    except Exception:
        logger.exception("ncc_report_email_task_failed")
        record_task_run(
            _NCC_EMAIL_TASK,
            status="error",
            counters={},
            duration_seconds=time.monotonic() - started,
        )
        raise

    result: dict[str, object] = {
        "queued": outcome.queued,
        "decision": outcome.decision.value,
        "scheduled_local_date": outcome.scheduled_local_date,
        "run_id": str(outcome.run_id) if outcome.run_id else None,
        "notification_id": (
            str(outcome.notification_id) if outcome.notification_id else None
        ),
        "rows": outcome.row_count,
        "not_filable": outcome.not_filable_count,
        "failure_code": outcome.failure_code,
    }

    record_task_run(
        _NCC_EMAIL_TASK,
        status="error" if outcome.failure_code else "success",
        counters={"queued": 1 if outcome.queued else 0},
        duration_seconds=time.monotonic() - started,
    )
    return result
