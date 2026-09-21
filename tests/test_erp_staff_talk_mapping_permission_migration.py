"""Deployment contract for the ERP-to-Talk machine permission."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATION = "605_erp_staff_talk_mapping_scope.py"
SCOPE = "communications:nextcloud_talk_staff:manage"


def _load():
    path = REPO_ROOT / "alembic" / "versions" / MIGRATION
    spec = importlib.util.spec_from_file_location("erp_staff_talk_scope", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_permission_migration_is_idempotent_and_reversible(monkeypatch) -> None:
    migration = _load()
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    permissions = sa.Table(
        "permissions",
        metadata,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("key", sa.String, unique=True, nullable=False),
        sa.Column("description", sa.String),
        sa.Column("is_active", sa.Boolean, nullable=False),
        sa.Column("is_ui_assignable", sa.Boolean, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )
    metadata.create_all(engine)

    with engine.begin() as connection:
        monkeypatch.setattr(migration.op, "get_bind", lambda: connection)
        migration.upgrade()
        migration.upgrade()
        rows = connection.execute(
            sa.select(permissions.c.key, permissions.c.is_ui_assignable)
        ).all()
        assert rows == [(SCOPE, False)]
        migration.downgrade()
        assert connection.execute(sa.select(permissions.c.key)).all() == []


def test_permission_migration_extends_current_head() -> None:
    migration = _load()
    assert migration.revision == "605_erp_staff_talk_mapping_scope"
    assert migration.down_revision == "604_material_cancel_pending"


def test_mapping_routes_and_seed_use_the_migrated_scope() -> None:
    api_source = (REPO_ROOT / "app" / "api" / "staff_sync.py").read_text(
        encoding="utf-8"
    )
    owner_source = (
        REPO_ROOT / "app" / "services" / "nextcloud_talk_staff.py"
    ).read_text(encoding="utf-8")
    seed_source = (REPO_ROOT / "scripts" / "seed" / "seed_rbac.py").read_text(
        encoding="utf-8"
    )

    assert f'COMMAND_SCOPE = "{SCOPE}"' in owner_source
    assert (
        api_source.count("require_permission(nextcloud_talk_staff.COMMAND_SCOPE)") == 2
    )
    assert SCOPE in seed_source
