import json
import logging

from app.logging import JsonLogFormatter, SensitiveQueryFilter


def test_json_log_formatter_includes_dynamic_extra_fields():
    formatter = JsonLogFormatter()
    record = logging.LogRecord(
        name="app.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=10,
        msg="critical_path_event",
        args=(),
        exc_info=None,
    )
    record.operation_id = "op-123"
    record.operation_type = "ont_authorize"
    record.details = {"fsp": "0/1/3", "serial": "UBNT-F9AA7344"}

    payload = json.loads(formatter.format(record))

    assert payload["message"] == "critical_path_event"
    assert payload["operation_id"] == "op-123"
    assert payload["operation_type"] == "ont_authorize"
    assert payload["details"] == {"fsp": "0/1/3", "serial": "UBNT-F9AA7344"}


def test_json_log_formatter_keeps_safe_classifier_evidence():
    formatter = JsonLogFormatter()
    record = logging.LogRecord(
        name="app.services.ai_intake",
        level=logging.WARNING,
        pathname=__file__,
        lineno=30,
        msg="ai intake classifier unavailable",
        args=(),
        exc_info=None,
    )
    record.event = "ai_intake_classifier_unavailable"
    record.classifier_attempt_status = "invalid_output"
    record.classifier_failure_reason = "classifier_invalid_output"
    record.classifier_failure_kind = "schema_validation_failure"
    record.classifier_retry_count = 1
    record.classifier_retry_limit = 2
    record.provider = "configured-provider"
    record.model = "configured-model"

    payload = json.loads(formatter.format(record))

    assert payload["event"] == "ai_intake_classifier_unavailable"
    assert payload["classifier_attempt_status"] == "invalid_output"
    assert payload["classifier_failure_reason"] == "classifier_invalid_output"
    assert payload["classifier_failure_kind"] == "schema_validation_failure"
    assert payload["classifier_retry_count"] == 1
    assert payload["classifier_retry_limit"] == 2
    assert payload["provider"] == "configured-provider"
    assert payload["model"] == "configured-model"


def test_sensitive_query_filter_redacts_formatted_uvicorn_request_line():
    record = logging.LogRecord(
        name="uvicorn.error",
        level=logging.INFO,
        pathname=__file__,
        lineno=30,
        msg='%s - "WebSocket %s" [accepted]',
        args=("127.0.0.1", "/ws/inbox?token=header.payload.signature&keep=1"),
        exc_info=None,
    )

    assert SensitiveQueryFilter().filter(record) is True
    rendered = record.getMessage()
    assert "header.payload.signature" not in rendered
    assert "token=<redacted>" in rendered
    assert "keep=1" in rendered
