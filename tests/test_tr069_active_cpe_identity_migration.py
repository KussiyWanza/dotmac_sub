"""Contract tests for the active CPE/TR-069 identity migration."""

from __future__ import annotations

import importlib
import uuid
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

MIGRATION = (
    Path(__file__).resolve().parents[1] / "alembic/versions/595_active_cpe_identity.py"
)


def _migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "migration_595_active_cpe_identity", MIGRATION
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_refuses_duplicate_active_cpe_links(monkeypatch) -> None:
    migration = _migration()
    cpe_id = uuid.uuid4()
    result = SimpleNamespace(fetchall=lambda: [(cpe_id, 2)])
    bind = SimpleNamespace(execute=lambda _statement: result)
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    # The upgrade path checks ``_has_index`` first (an idempotency guard, not
    # the behavior under test here) -- stub it directly rather than making a
    # bare SimpleNamespace bind inspectable, which is what the real
    # ``sqlalchemy.inspect(...)`` call inside ``_has_index`` requires.
    monkeypatch.setattr(migration, "_has_index", lambda _name: False)

    with pytest.raises(RuntimeError, match="duplicate active links"):
        migration.upgrade()


def test_migration_skips_when_index_already_exists(monkeypatch) -> None:
    """``_has_index`` short-circuits both the duplicate check and the index
    build -- a rerun after a successful prior run (or a manually-repaired
    invalid CONCURRENTLY build, per the module docstring) is a no-op, not a
    second refusal or a second CREATE INDEX attempt."""
    migration = _migration()
    calls: list[str] = []
    bind = SimpleNamespace(
        execute=lambda _statement: calls.append("duplicate_check_ran")
    )
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    monkeypatch.setattr(migration, "_has_index", lambda _name: True)

    migration.upgrade()

    assert calls == []


def test_migration_declares_matching_partial_unique_index() -> None:
    source = Path("alembic/versions/595_active_cpe_identity.py").read_text(
        encoding="utf-8"
    )

    assert "uq_tr069_cpe_devices_active_cpe_device_id" in source
    assert "GROUP BY cpe_device_id HAVING count(*) > 1" in source
    # PostgreSQL: built CONCURRENTLY so live TR-069 Inform writes to this
    # high-write table are not blocked for the duration of the index build
    # (the repo's established pattern -- see 581_inbox_delivery_status_index.py
    # and 591_field_note_delivery_idempotency.py for the same shape).
    assert "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS" in source
    assert '_WHERE = "is_active AND cpe_device_id IS NOT NULL"' in source
    assert "WHERE {_WHERE}" in source
    assert "autocommit_block" in source
    assert "DROP INDEX CONCURRENTLY IF EXISTS" in source
    # SQLite: no CONCURRENTLY support, no live-write concurrency concern for
    # the test/dev lane -- the plain dialect-kwarg path stays.
    assert "sqlite_where=sa.text(_WHERE)" in source
