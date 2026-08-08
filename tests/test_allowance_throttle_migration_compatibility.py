from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from app.services.sot_registry.registry import service_relationship

ROOT = Path(__file__).resolve().parents[1]
MIGRATION_501 = ROOT / "alembic/versions/501_retire_allowance_throttle_rate.py"


def _load_migration(module_name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, MIGRATION_501)
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


def test_501_extends_the_recorded_single_head() -> None:
    migration = _load_migration("migration_501_identity")

    assert migration.revision == "501_retire_allowance_throttle_rate"
    assert migration.down_revision == "500_reconcile_staff_notification_inbox"


def test_501_drops_the_obsolete_column_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration("migration_501_existing")
    executed: list[str] = []
    dropped: list[tuple[str, str]] = []

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
        "drop_column",
        lambda table, column: dropped.append((table, column)),
    )

    migration.upgrade()

    assert executed == [
        "SET LOCAL lock_timeout = '5s'",
        "SET LOCAL statement_timeout = '60s'",
    ]
    assert dropped == [("usage_allowances", "throttle_rate_mbps")]


def test_501_is_idempotent_when_the_obsolete_column_is_already_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration("migration_501_absent")
    dropped: list[tuple[str, str]] = []

    monkeypatch.setattr(migration.op, "execute", lambda _statement: None)
    monkeypatch.setattr(migration.op, "get_bind", lambda: object())
    monkeypatch.setattr(
        migration.sa,
        "inspect",
        lambda _bind: _Inspector(("id",)),
    )
    monkeypatch.setattr(
        migration.op,
        "drop_column",
        lambda table, column: dropped.append((table, column)),
    )

    migration.upgrade()

    assert dropped == []


def test_501_downgrade_does_not_recreate_the_retired_decision() -> None:
    migration = _load_migration("migration_501_downgrade")

    migration.downgrade()

    source = MIGRATION_501.read_text(encoding="utf-8")
    assert "op.add_column" not in source


def test_executable_owner_contract_records_the_completed_schema_retirement() -> None:
    owner = service_relationship("access.fup_throttle_rate")
    assert owner.contract is not None
    assert owner.contract.migration is not None
    retirement = owner.contract.migration.fallback_retirement

    assert retirement is not None
    assert "is dropped" in retirement
