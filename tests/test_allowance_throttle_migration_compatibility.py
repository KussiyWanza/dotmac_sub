from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
import sqlalchemy as sa

from app.services.sot_registry.registry import service_relationship

ROOT = Path(__file__).resolve().parents[1]
MIGRATION_501 = ROOT / "alembic/versions/501_retire_allowance_throttle_rate.py"
MIGRATION_502 = ROOT / "alembic/versions/502_restore_deferred_allowance_throttle.py"


def _load_migration(path: Path, module_name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Inspector:
    def __init__(self, columns: tuple[str, ...]) -> None:
        self._columns = columns

    def get_columns(self, table_name: str) -> list[dict[str, str]]:
        assert table_name == "usage_allowances"
        return [{"name": name} for name in self._columns]


def test_compatibility_revisions_form_the_single_forward_chain() -> None:
    migration_501 = _load_migration(MIGRATION_501, "migration_501_compat")
    migration_502 = _load_migration(MIGRATION_502, "migration_502_repair")

    assert migration_501.revision == "501_retire_allowance_throttle_rate"
    assert migration_501.down_revision == "500_reconcile_staff_notification_inbox"
    assert migration_502.revision == "502_restore_deferred_allowance_throttle"
    assert migration_502.down_revision == "501_retire_allowance_throttle_rate"


def test_501_is_a_no_ddl_compatibility_marker() -> None:
    migration = _load_migration(MIGRATION_501, "migration_501_no_ddl")

    migration.upgrade()
    migration.downgrade()

    source = MIGRATION_501.read_text(encoding="utf-8")
    assert "op.drop_column" not in source
    assert "op.add_column" not in source


def test_502_preserves_an_existing_column(monkeypatch: pytest.MonkeyPatch) -> None:
    migration = _load_migration(MIGRATION_502, "migration_502_existing")
    executed: list[str] = []
    added: list[tuple[str, sa.Column[object]]] = []

    monkeypatch.setattr(
        migration.op,
        "execute",
        lambda statement: executed.append(str(statement)),
    )
    monkeypatch.setattr(migration.op, "get_bind", lambda: object())
    monkeypatch.setattr(
        migration.sa,
        "inspect",
        lambda _bind: _Inspector(("id", "throttle_rate_mbps")),
    )
    monkeypatch.setattr(
        migration.op,
        "add_column",
        lambda table, column: added.append((table, column)),
    )

    migration.upgrade()

    assert executed == [
        "SET LOCAL lock_timeout = '5s'",
        "SET LOCAL statement_timeout = '60s'",
    ]
    assert added == []


def test_502_restores_only_the_missing_nullable_integer_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration(MIGRATION_502, "migration_502_missing")
    added: list[tuple[str, sa.Column[object]]] = []

    monkeypatch.setattr(migration.op, "execute", lambda _statement: None)
    monkeypatch.setattr(migration.op, "get_bind", lambda: object())
    monkeypatch.setattr(
        migration.sa,
        "inspect",
        lambda _bind: _Inspector(("id",)),
    )
    monkeypatch.setattr(
        migration.op,
        "add_column",
        lambda table, column: added.append((table, column)),
    )

    migration.upgrade()

    assert len(added) == 1
    table_name, column = added[0]
    assert table_name == "usage_allowances"
    assert column.name == "throttle_rate_mbps"
    assert isinstance(column.type, sa.Integer)
    assert column.nullable is True


def test_502_refuses_a_destructive_downgrade() -> None:
    migration = _load_migration(MIGRATION_502, "migration_502_downgrade")

    with pytest.raises(RuntimeError, match="forward-fix only"):
        migration.downgrade()


def test_executable_owner_contract_records_the_deferred_schema_retirement() -> None:
    owner = service_relationship("access.fup_throttle_rate")
    assert owner.contract is not None
    assert owner.contract.migration is not None
    retirement = owner.contract.migration.fallback_retirement

    assert retirement is not None
    assert "is unmapped" in retirement
    assert "separate reviewed change" in retirement
