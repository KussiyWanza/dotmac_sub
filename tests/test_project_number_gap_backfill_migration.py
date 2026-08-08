from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import sqlalchemy as sa

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    REPO_ROOT / "alembic" / "versions" / "498_backfill_project_numbers_8_10.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "project_number_gap_backfill", MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_extends_current_single_head() -> None:
    migration = _load_migration()
    assert migration.revision == "498_backfill_project_numbers_8_10"
    assert migration.down_revision == "497_vendor_route_lengths"


def test_upgrade_allocates_after_numbers_available_at_execution(monkeypatch) -> None:
    migration = _load_migration()
    statements: list[str] = []

    def record_execute(statement: sa.sql.elements.TextClause) -> None:
        statements.append(str(statement))

    monkeypatch.setattr(migration.op, "execute", record_execute)
    migration.upgrade()

    assert statements[:4] == [
        "SET LOCAL lock_timeout = '5s'",
        "SET LOCAL statement_timeout = '60s'",
        "LOCK TABLE document_sequences IN SHARE ROW EXCLUSIVE MODE",
        "LOCK TABLE projects IN SHARE ROW EXCLUSIVE MODE",
    ]
    backfill = statements[4]
    assert "MAX(substring(number FROM 6)::integer)" in backfill
    assert "SELECT next_value - 1" in backfill
    assert "GREATEST(" in backfill
    assert "row_number() OVER (ORDER BY number::integer)" in backfill
    assert "WHERE number IN ('8', '9', '10')" in backfill
    assert "project_number_floor.value + rollout_window_projects.offset" in backfill
    sequence_repair = statements[5]
    assert "MAX(substring(number FROM 6)::integer) + 1" in sequence_repair
    assert "GREATEST(" in sequence_repair
