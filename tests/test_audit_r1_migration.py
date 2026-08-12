"""Migration 524 is an additive, history-preserving audit expansion."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic/versions/524_audit_events_kernel_r1.py"


def _load_migration() -> ModuleType:
    assert MIGRATION.exists()
    spec = importlib.util.spec_from_file_location("migration_524_audit_r1", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _RecordingOperations:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def __getattr__(self, name: str):
        def record(*args: Any, **kwargs: Any) -> None:
            self.calls.append((name, args, kwargs))

        return record


def test_revision_extends_the_current_product_head() -> None:
    migration = _load_migration()

    assert migration.revision == "524_audit_events_kernel_r1"
    assert migration.down_revision == "523_domain_settings_tenant_fk"


def test_upgrade_is_nullable_additive_and_sets_created_default_separately() -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    migration.op = operations

    migration.upgrade()

    assert [name for name, _args, _kwargs in operations.calls] == [
        "add_column",
        "add_column",
        "add_column",
        "alter_column",
        "create_index",
    ]
    columns = {
        args[1].name: args[1]
        for name, args, _kwargs in operations.calls
        if name == "add_column"
    }
    assert set(columns) == {"actor_party_id", "details", "created_at"}
    assert all(column.nullable is True for column in columns.values())
    assert isinstance(columns["actor_party_id"].type, postgresql.UUID)
    assert isinstance(columns["details"].type, postgresql.JSONB)
    assert isinstance(columns["created_at"].type, sa.DateTime)
    assert columns["created_at"].server_default is None
    assert not columns["actor_party_id"].foreign_keys

    _name, alter_args, alter_kwargs = operations.calls[3]
    assert alter_args == ("audit_events", "created_at")
    assert str(alter_kwargs["server_default"]).lower() == "now()"
    assert alter_kwargs["existing_nullable"] is True


def test_downgrade_removes_only_the_r1_expansion() -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    migration.op = operations

    migration.downgrade()

    assert operations.calls == [
        (
            "drop_index",
            ("ix_audit_events_actor_party_id",),
            {"table_name": "audit_events"},
        ),
        ("drop_column", ("audit_events", "created_at"), {}),
        ("drop_column", ("audit_events", "details"), {}),
        ("drop_column", ("audit_events", "actor_party_id"), {}),
    ]
