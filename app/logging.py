import json
import logging
import logging.config
import sys
from datetime import UTC, datetime

_BASE_LOG_RECORD_FIELDS = set(logging.makeLogRecord({}).__dict__.keys())


class StderrStreamHandler(logging.StreamHandler):
    """A StreamHandler that resolves ``sys.stderr`` lazily on every emit.

    The default ``logging.StreamHandler`` captures whatever ``sys.stderr``
    points at when the handler is *constructed* and holds that reference for
    life. ``configure_logging()`` runs at import time of ``app.main``; under
    pytest the first test that imports the app binds the root handler to that
    test's captured stderr. When pytest later tears that capture down (closing
    the stream), the still-bound handler raises
    ``ValueError: I/O operation on closed file`` for every subsequent test that
    logs through the root logger — a broad, ordering-dependent test-isolation
    cascade ("--- Logging error ---" in CI). Resolving ``sys.stderr`` on each
    access keeps the handler bound to the live stream, fixing the leak at its
    source (and making the handler robust to any stderr swap at runtime).
    """

    def __init__(self) -> None:
        super().__init__(stream=sys.stderr)

    @property
    def stream(self):
        return sys.stderr

    @stream.setter
    def stream(self, value) -> None:
        # logging.StreamHandler.__init__ assigns to self.stream; ignore the
        # snapshot it tries to store so stream resolution stays dynamic.
        pass


def _json_safe(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in (
            "request_id",
            "actor_id",
            "path",
            "method",
            "status",
            "duration_ms",
        ):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        for key, value in record.__dict__.items():
            if key in payload or key in _BASE_LOG_RECORD_FIELDS or key.startswith("_"):
                continue
            payload[key] = _json_safe(value)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging() -> None:
    logging_config = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "json": {
                "()": JsonLogFormatter,
            }
        },
        "handlers": {
            "default": {
                # Lazily-resolving stderr handler (see StderrStreamHandler):
                # avoids binding to a stale/closed stream snapshot, which under
                # pytest caused an "I/O operation on closed file" cascade.
                "()": StderrStreamHandler,
                "formatter": "json",
            }
        },
        "root": {"handlers": ["default"], "level": "INFO"},
        "loggers": {
            # Paramiko logs expected network/banner failures from remote devices
            # as ERROR tracebacks before application code can handle them. Keep
            # those details in our OLT SSH result messages instead of flooding
            # app logs with transport internals.
            "paramiko.transport": {"level": "CRITICAL", "propagate": False},
        },
    }
    logging.config.dictConfig(logging_config)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
