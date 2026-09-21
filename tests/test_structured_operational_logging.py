import logging

from app.services.operational_logging import (
    OperationalEventName,
    OperationalLogEvent,
    OperationalOutcome,
    log_operational_event,
)


def test_operational_summary_is_one_structured_info_event(caplog) -> None:
    with caplog.at_level(logging.INFO):
        log_operational_event(
            logging.getLogger("test.operational"),
            OperationalLogEvent(
                name=OperationalEventName.NOTIFICATION_QUEUE_PROCESSED,
                outcome=OperationalOutcome.COMPLETED_WITH_RETRIES,
                component="notifications",
                counters={"delivered": 4, "retried": 1, "failed": 0},
            ),
        )

    record = caplog.records[-1]
    assert record.getMessage() == "operational_task_outcome"
    assert record.event_name == "notification_queue_processed"
    assert record.outcome == "completed_with_retries"
    assert record.counters == {"delivered": 4, "retried": 1, "failed": 0}
