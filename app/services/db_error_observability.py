"""Redacted correlation for schema drift and transaction-timeout failures.

The handler never logs SQL text or bound parameters. It records a stable
statement fingerprint, the request correlation ID, and the nearest application
caller so an unidentified production query can be mapped back to its owner.
"""

from __future__ import annotations

import hashlib
import logging
import re
import traceback
from collections.abc import MutableMapping
from dataclasses import dataclass
from time import monotonic
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Connection, Engine

logger = logging.getLogger(__name__)

_SPACE_RE = re.compile(r"\s+")
_IDENTIFIER_RE = re.compile(
    r"(?:column|relation)\s+[\"'](?P<identifier>[A-Za-z0-9_.-]+)[\"']",
    re.IGNORECASE,
)
_INSTALLED_KEY = "_dotmac_db_error_observability_installed"
_CONNECTION_OWNER_KEY = "_dotmac_database_transaction_owner"


@dataclass(frozen=True, slots=True)
class DatabaseTransactionOwner:
    started_at: float
    request_id: str | None
    task_name: str | None
    task_id: str | None


def set_database_transaction_owner(
    connection: Connection,
    owner: DatabaseTransactionOwner,
) -> None:
    connection.info[_CONNECTION_OWNER_KEY] = owner


def clear_database_transaction_owner(
    connection_info: MutableMapping[str, object],
) -> None:
    connection_info.pop(_CONNECTION_OWNER_KEY, None)


def statement_fingerprint(statement: object) -> str | None:
    if not isinstance(statement, str) or not statement.strip():
        return None
    normalized = _SPACE_RE.sub(" ", statement.strip()).lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]


def _caller() -> str:
    for frame in reversed(traceback.extract_stack(limit=40)):
        path = frame.filename.replace("\\", "/")
        if "/app/" not in path:
            continue
        if path.endswith("/services/db_error_observability.py") or path.endswith(
            "/app/db.py"
        ):
            continue
        relative = path.split("/app/", 1)[1]
        return f"app/{relative}:{frame.lineno}:{frame.name}"
    return "unknown"


def _request_id() -> str | None:
    try:
        from app.observability import get_request_id

        return get_request_id() or None
    except Exception:
        return None


def _error_code(exc: BaseException) -> str | None:
    return (
        str(getattr(exc, "sqlstate", None) or getattr(exc, "pgcode", None) or "")
        or None
    )


def _category(exc: BaseException, code: str | None) -> str | None:
    message = str(exc).casefold()
    if (
        code == "42703"
        or "undefined column" in message
        or ("column" in message and "does not exist" in message)
    ):
        return "undefined_column"
    if (
        code == "42P01"
        or "undefined table" in message
        or ("relation" in message and "does not exist" in message)
    ):
        return "undefined_relation"
    if "idle-in-transaction" in message or "idle in transaction" in message:
        return "idle_transaction_timeout"
    return None


def _handle_error(context: Any) -> None:
    original = getattr(context, "original_exception", None)
    if not isinstance(original, BaseException):
        return
    code = _error_code(original)
    category = _category(original, code)
    if category is None:
        return
    message = str(original)
    identifier_match = _IDENTIFIER_RE.search(message)
    logger.error(
        "database_operational_error",
        extra={
            "category": category,
            "db_error_code": code,
            "identifier": (
                identifier_match.group("identifier")
                if identifier_match is not None
                else None
            ),
            "statement_fingerprint": statement_fingerprint(
                getattr(context, "statement", None)
            ),
            "request_id": _request_id(),
            "caller": _caller(),
        },
    )


def _handle_pool_invalidate(
    _dbapi_connection: Any,
    connection_record: Any,
    exception: BaseException | None,
) -> None:
    if exception is None:
        return
    code = _error_code(exception)
    category = _category(exception, code)
    if category != "idle_transaction_timeout":
        return
    owner = getattr(connection_record, "info", {}).get(_CONNECTION_OWNER_KEY)
    if not isinstance(owner, DatabaseTransactionOwner):
        owner = DatabaseTransactionOwner(
            started_at=monotonic(),
            request_id=None,
            task_name=None,
            task_id=None,
        )
    logger.error(
        "database_pool_connection_invalidated",
        extra={
            "category": category,
            "db_error_code": code,
            "request_id": owner.request_id,
            "task_name": owner.task_name,
            "task_id": owner.task_id,
            "transaction_age_seconds": round(
                max(0.0, monotonic() - owner.started_at),
                3,
            ),
        },
    )


def install_db_error_observability(engine: Engine) -> None:
    if getattr(engine, _INSTALLED_KEY, False):
        return
    event.listen(engine, "handle_error", _handle_error)
    event.listen(engine.pool, "invalidate", _handle_pool_invalidate)
    setattr(engine, _INSTALLED_KEY, True)
