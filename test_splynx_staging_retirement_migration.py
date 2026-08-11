"""Migration 517 drops splynx_staging only when nothing outside it depends on it."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "517_retire_splynx_staging_schema.py"
)
_SPEC = importlib.util.spec_from_file_location(
    "splynx_staging_retirement_migration", _MIGRATION_PATH
)
assert _SPEC and _SPEC.loader
retirement = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(retirement)


def _bind(*, dialect: str = "postgresql", present: bool = True, dependents=()):
    """A bind whose two queries answer schema-presence then external dependents."""

    conn = MagicMock()
    conn.dialect.name = dialect

    presence_result = MagicMock()
    presence_result.scalar.return_value = 1 if present else None

    dependents_result = MagicMock()
    dependents_result.scalars.return_value.all.return_value = list(dependents)

    conn.execute.side_effect = [presence_result, dependents_result]
    return conn


def test_chains_onto_the_current_head() -> None:
    assert retirement.revision == "517_retire_splynx_staging_schema"
    assert retirement.down_revision == "516_material_request_erp_submission"


def test_drops_the_schema_when_nothing_external_depends_on_it(monkeypatch) -> None:
    conn = _bind()
    executed: list[str] = []
    monkeypatch.setattr(retirement.op, "get_bind", lambda: conn)
    monkeypatch.setattr(retirement.op, "execute", executed.append)

    retirement.upgrade()

    assert executed == ['DROP SCHEMA IF EXISTS "splynx_staging" CASCADE']


def test_refuses_and_names_an_external_dependent(monkeypatch) -> None:
    conn = _bind(dependents=("public.some_live_view",))
    executed: list[str] = []
    monkeypatch.setattr(retirement.op, "get_bind", lambda: conn)
    monkeypatch.setattr(retirement.op, "execute", executed.append)

    with pytest.raises(RuntimeError) as exc:
        retirement.upgrade()

    # Naming the dependent is the point: CASCADE would have destroyed it.
    assert "public.some_live_view" in str(exc.value)
    assert executed == []


def test_absent_schema_is_a_no_op_so_a_fresh_chain_run_survives(monkeypatch) -> None:
    conn = _bind(present=False)
    executed: list[str] = []
    monkeypatch.setattr(retirement.op, "get_bind", lambda: conn)
    monkeypatch.setattr(retirement.op, "execute", executed.append)

    retirement.upgrade()

    assert executed == []


def test_non_postgres_is_a_no_op(monkeypatch) -> None:
    conn = _bind(dialect="sqlite")
    executed: list[str] = []
    monkeypatch.setattr(retirement.op, "get_bind", lambda: conn)
    monkeypatch.setattr(retirement.op, "execute", executed.append)

    retirement.upgrade()

    assert executed == []
    conn.execute.assert_not_called()


def test_there_is_no_row_count_gate() -> None:
    """The predecessor refused on any row, believing the schema was empty.

    It has not been empty since the evidence import. Rows are the expected
    condition; the verified archive is what makes dropping them safe, so a
    row-count assertion here would only reinstate a false premise.
    """

    source = _MIGRATION_PATH.read_text(encoding="utf-8")
    assert "count(*)" not in source
    assert "reltuples" not in source


def test_downgrade_refuses_and_points_at_the_archive() -> None:
    with pytest.raises(RuntimeError) as exc:
        retirement.downgrade()

    message = str(exc.value)
    assert "db-archives/splynx_staging_2026-08-11.dump" in message
    assert "pg_restore" in message
