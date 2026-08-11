"""Migration 518 drops splynx_staging only when nothing outside it depends on it."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "518_retire_splynx_staging_schema.py"
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
    assert retirement.revision == "518_retire_splynx_staging_schema"
    assert retirement.down_revision == "517_close_legacy_resolved_tickets"


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


def test_downgrade_is_a_no_op_so_the_chain_stays_unwindable(monkeypatch) -> None:
    """Revisions above this one are downgraded through it in chain tests.

    Raising would block unwinding migrations unrelated to this schema, and
    recreating an empty `splynx_staging` would be worse -- the next upgrade
    would drop the empty shell and report success, so the chain would look
    reversible while the rows were long gone.
    """

    executed: list[str] = []
    monkeypatch.setattr(retirement.op, "execute", executed.append)

    retirement.downgrade()

    assert executed == []


def test_the_archive_location_is_recorded_for_recovery() -> None:
    source = _MIGRATION_PATH.read_text(encoding="utf-8")
    assert "db-archives/splynx_staging_2026-08-11.dump" in source
    assert "20b2e815e0da4006d3b501a7fea4c36b0645fdae1fb0e3c81fd7e18c0980e2fd" in source
    assert "pg_restore -n splynx_staging" in source
