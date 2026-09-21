"""The migration idle_in_transaction_session_timeout is bounded and sanitized.

Guards the 2026-09-09 failed-deploy fix: ``context.run_migrations()`` loads,
imports and topologically sorts every file under ``alembic/versions/`` INSIDE
the already-open migration transaction, with zero DB activity, before the
first real migration statement runs. Production's
``idle_in_transaction_session_timeout`` killed the connection during that
setup. The raw value is owned by
``settings.alembic_idle_transaction_timeout`` (the config owner) and
interpolated into a Postgres ``SET``, so malformed input must not reach SQL.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.db import (
    apply_migration_idle_transaction_timeout,
    resolve_migration_idle_transaction_timeout,
)


class _FakeDialect:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeConnection:
    """Captures executed SQL without touching a real database."""

    def __init__(self, dialect_name: str) -> None:
        self.dialect = _FakeDialect(dialect_name)
        self.executed: list[str] = []

    def exec_driver_sql(self, sql: str) -> None:
        self.executed.append(sql)


def test_defaults_to_10min(monkeypatch):
    monkeypatch.setattr(
        "app.db.settings",
        SimpleNamespace(alembic_idle_transaction_timeout="10min"),
    )
    assert resolve_migration_idle_transaction_timeout() == "10min"


@pytest.mark.parametrize("value", ["10min", "600s", "600000ms", "0"])
def test_valid_settings_value_is_used(monkeypatch, value):
    monkeypatch.setattr(
        "app.db.settings",
        SimpleNamespace(alembic_idle_transaction_timeout=value),
    )
    assert resolve_migration_idle_transaction_timeout() == value


@pytest.mark.parametrize(
    "bad",
    [
        "'; DROP TABLE x; --",
        "10 minutes",
        "abc",
        "10min; SELECT 1",
        "",
        "-10min",
        "10MIN",
    ],
)
def test_malformed_input_falls_back_to_default(monkeypatch, bad):
    # No injection or malformed unit reaches the SET statement.
    monkeypatch.setattr(
        "app.db.settings",
        SimpleNamespace(alembic_idle_transaction_timeout=bad),
    )
    assert resolve_migration_idle_transaction_timeout() == "10min"


def test_explicit_arg_overrides_settings(monkeypatch):
    monkeypatch.setattr(
        "app.db.settings",
        SimpleNamespace(alembic_idle_transaction_timeout="99min"),
    )
    assert resolve_migration_idle_transaction_timeout("5min") == "5min"
    assert resolve_migration_idle_transaction_timeout("bad") == "10min"


def test_no_op_on_non_postgres_dialect(monkeypatch):
    monkeypatch.setattr(
        "app.db.settings",
        SimpleNamespace(alembic_idle_transaction_timeout="10min"),
    )
    connection = _FakeConnection("sqlite")
    apply_migration_idle_transaction_timeout(connection)
    assert connection.executed == []


def test_issues_set_with_resolved_value_on_postgres(monkeypatch):
    monkeypatch.setattr(
        "app.db.settings",
        SimpleNamespace(alembic_idle_transaction_timeout="10min"),
    )
    connection = _FakeConnection("postgresql")
    apply_migration_idle_transaction_timeout(connection)
    assert connection.executed == ["SET idle_in_transaction_session_timeout = '10min'"]


def test_malformed_settings_value_still_produces_a_safe_set(monkeypatch):
    monkeypatch.setattr(
        "app.db.settings",
        SimpleNamespace(alembic_idle_transaction_timeout="'; DROP TABLE x; --"),
    )
    connection = _FakeConnection("postgresql")
    apply_migration_idle_transaction_timeout(connection)
    assert connection.executed == ["SET idle_in_transaction_session_timeout = '10min'"]
