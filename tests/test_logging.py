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
