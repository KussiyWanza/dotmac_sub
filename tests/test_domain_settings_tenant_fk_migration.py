"""Migration 523 adds only the missing kernel tenant foreign key.

Sub already owns the safe scope default and alignment CHECK through migration
514.  Kernel 0.1.0a40 adopts that exact shape, so replacing either fact here
would reopen the invalid tenant/NULL row that prompted the release.  The one
remaining schema delta is ``domain_settings.tenant_id -> tenants.id``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic/versions/523_domain_settings_tenant_fk.py"


def _load_migration() -> ModuleType:
    assert MIGRATION.exists(), "write the FK-only migration before making this green"
    spec = importlib.util.spec_from_file_location("migration_523_tenant_fk", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _RecordingOperations:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def create_foreign_key(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("create_foreign_key", args, kwargs))

    def drop_constraint(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("drop_constraint", args, kwargs))

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"migration 523 must not call op.{name}")


class _Inspector:
    def __init__(self, foreign_keys: list[dict[str, Any]]) -> None:
        self.foreign_keys = foreign_keys

    def get_foreign_keys(self, table: str) -> list[dict[str, Any]]:
        assert table == "domain_settings"
        return self.foreign_keys


def test_revision_extends_the_current_head() -> None:
    migration = _load_migration()

    assert migration.revision == "523_domain_settings_tenant_fk"
    assert migration.down_revision == "522_ont_service_configuration_lifecycle"


def test_upgrade_adds_only_the_kernel_tenant_fk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)
    monkeypatch.setattr(migration, "_fk_exists", lambda: False)

    migration.upgrade()

    assert operations.calls == [
        (
            "create_foreign_key",
            (
                "fk_domain_settings_tenant",
                "domain_settings",
                "tenants",
                ["tenant_id"],
                ["id"],
            ),
            {"ondelete": "CASCADE"},
        )
    ]


def test_upgrade_adopts_an_existing_exact_fk(monkeypatch: pytest.MonkeyPatch) -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)
    monkeypatch.setattr(migration, "_fk_exists", lambda: True)

    migration.upgrade()

    assert operations.calls == []


def test_existing_fk_is_adopted_only_when_its_shape_is_exact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    inspector = _Inspector(
        [
            {
                "name": "fk_domain_settings_tenant",
                "constrained_columns": ["tenant_id"],
                "referred_table": "tenants",
                "referred_columns": ["id"],
                "options": {"ondelete": "CASCADE"},
            }
        ]
    )
    monkeypatch.setattr(migration, "op", SimpleNamespace(get_bind=lambda: object()))
    monkeypatch.setattr(migration.sa, "inspect", lambda _bind: inspector)

    assert migration._fk_exists() is True


def test_same_named_fk_with_different_shape_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    inspector = _Inspector(
        [
            {
                "name": "fk_domain_settings_tenant",
                "constrained_columns": ["tenant_id"],
                "referred_table": "tenants",
                "referred_columns": ["id"],
                "options": {"ondelete": "RESTRICT"},
            }
        ]
    )
    monkeypatch.setattr(migration, "op", SimpleNamespace(get_bind=lambda: object()))
    monkeypatch.setattr(migration.sa, "inspect", lambda _bind: inspector)

    with pytest.raises(RuntimeError, match="unexpected definition"):
        migration._fk_exists()


def test_downgrade_removes_only_the_tenant_fk(monkeypatch: pytest.MonkeyPatch) -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)
    monkeypatch.setattr(migration, "_fk_exists", lambda: True)

    migration.downgrade()

    assert operations.calls == [
        (
            "drop_constraint",
            ("fk_domain_settings_tenant", "domain_settings"),
            {"type_": "foreignkey"},
        )
    ]


def test_downgrade_is_safe_when_the_fk_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)
    monkeypatch.setattr(migration, "_fk_exists", lambda: False)

    migration.downgrade()

    assert operations.calls == []
