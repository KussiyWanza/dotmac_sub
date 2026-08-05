"""Grant contracts for the crm:quote:send permission migration.

Migration 471 shipped the Quote email surface with its permission key only in
``scripts/seed/seed_rbac.py``, which no deploy runs — so deployed databases had
no ``crm:quote:send`` and the feature was unreachable. These tests pin the
repair: the permission is seeded by migration, and every principal already
trusted to manage quotes keeps that authority when sending is split out.
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import sqlalchemy as sa
from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT = Path(__file__).resolve().parent.parent

SOURCE_KEY = "crm:quote:write"
TARGET_KEY = "crm:quote:send"


def _load(name: str, file_name: str):
    path = REPO_ROOT / "alembic" / "versions" / file_name
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _grant_keys(connection, table, holder_column: str, holder_id: str) -> set[str]:
    permissions = sa.table("permissions", sa.column("id"), sa.column("key"))
    return set(
        connection.execute(
            sa.select(permissions.c.key)
            .select_from(
                table.join(permissions, table.c.permission_id == permissions.c.id)
            )
            .where(getattr(table.c, holder_column) == holder_id)
        ).scalars()
    )


def _schema(engine) -> dict[str, sa.Table]:
    metadata = sa.MetaData()
    tables = {
        "permissions": sa.Table(
            "permissions",
            metadata,
            sa.Column("id", sa.String, primary_key=True),
            sa.Column("key", sa.String, unique=True, nullable=False),
            sa.Column("description", sa.String),
            sa.Column("is_active", sa.Boolean, nullable=False),
            sa.Column("is_ui_assignable", sa.Boolean, nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
        ),
        "role_permissions": sa.Table(
            "role_permissions",
            metadata,
            sa.Column("id", sa.String, primary_key=True),
            sa.Column("role_id", sa.String, nullable=False),
            sa.Column("permission_id", sa.String, nullable=False),
            sa.UniqueConstraint("role_id", "permission_id"),
        ),
        "subscriber_permissions": sa.Table(
            "subscriber_permissions",
            metadata,
            sa.Column("id", sa.String, primary_key=True),
            sa.Column("subscriber_id", sa.String, nullable=False),
            sa.Column("permission_id", sa.String, nullable=False),
            sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("granted_by_subscriber_id", sa.String),
            sa.UniqueConstraint("subscriber_id", "permission_id"),
        ),
        "system_user_permissions": sa.Table(
            "system_user_permissions",
            metadata,
            sa.Column("id", sa.String, primary_key=True),
            sa.Column("system_user_id", sa.String, nullable=False),
            sa.Column("permission_id", sa.String, nullable=False),
            sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("granted_by_system_user_id", sa.String),
            sa.UniqueConstraint("system_user_id", "permission_id"),
        ),
    }
    metadata.create_all(engine)
    return tables


def _seed_write_holders(connection, tables, ids, now) -> None:
    connection.execute(
        tables["permissions"].insert(),
        [
            {
                "id": ids["source"],
                "key": SOURCE_KEY,
                "description": "Manage quotes",
                "is_active": True,
                "is_ui_assignable": True,
                "created_at": now,
                "updated_at": now,
            }
        ],
    )
    connection.execute(
        tables["role_permissions"].insert(),
        {
            "id": str(uuid4()),
            "role_id": ids["role"],
            "permission_id": ids["source"],
        },
    )
    connection.execute(
        tables["subscriber_permissions"].insert(),
        {
            "id": str(uuid4()),
            "subscriber_id": ids["subscriber"],
            "permission_id": ids["source"],
            "granted_at": now,
            "granted_by_subscriber_id": None,
        },
    )
    connection.execute(
        tables["system_user_permissions"].insert(),
        {
            "id": str(uuid4()),
            "system_user_id": ids["system_user"],
            "permission_id": ids["source"],
            "granted_at": now,
            "granted_by_system_user_id": None,
        },
    )


def test_quote_send_permission_migration_extends_the_single_head_chain():
    migration = _load("quote_send_permission_chain", "477_quote_send_permission.py")
    parent = _load("chain_parent", "476_reconcile_project_number_series.py")

    assert migration.down_revision == parent.revision

    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    assert len(heads) == 1
    assert migration.revision in {
        item.revision
        for item in script.iterate_revisions(
            heads[0], migration.revision, inclusive=True
        )
    }


def test_upgrade_seeds_send_permission_for_every_quote_write_holder(monkeypatch):
    migration = _load("quote_send_permission_grants", "477_quote_send_permission.py")
    engine = sa.create_engine("sqlite://")
    tables = _schema(engine)
    ids = {
        "source": str(uuid4()),
        "role": str(uuid4()),
        "subscriber": str(uuid4()),
        "system_user": str(uuid4()),
    }
    now = datetime.now(UTC)

    with engine.begin() as connection:
        _seed_write_holders(connection, tables, ids, now)
        monkeypatch.setattr(migration.op, "get_bind", lambda: connection)

        migration.upgrade()

        assert TARGET_KEY in _grant_keys(
            connection, tables["role_permissions"], "role_id", ids["role"]
        )
        assert TARGET_KEY in _grant_keys(
            connection,
            tables["subscriber_permissions"],
            "subscriber_id",
            ids["subscriber"],
        )
        assert TARGET_KEY in _grant_keys(
            connection,
            tables["system_user_permissions"],
            "system_user_id",
            ids["system_user"],
        )

        # Re-running must not duplicate the permission or its grants.
        migration.upgrade()
        assert (
            connection.execute(
                sa.select(sa.func.count())
                .select_from(tables["permissions"])
                .where(tables["permissions"].c.key == TARGET_KEY)
            ).scalar_one()
            == 1
        )
        assert (
            connection.execute(
                sa.select(sa.func.count()).select_from(tables["role_permissions"])
            ).scalar_one()
            == 2
        )

        migration.downgrade()
        assert TARGET_KEY not in _grant_keys(
            connection, tables["role_permissions"], "role_id", ids["role"]
        )
        assert (
            connection.execute(
                sa.select(sa.func.count())
                .select_from(tables["permissions"])
                .where(tables["permissions"].c.key == TARGET_KEY)
            ).scalar_one()
            == 0
        )
        # The established quote-management grant is never collateral damage.
        assert SOURCE_KEY in _grant_keys(
            connection, tables["role_permissions"], "role_id", ids["role"]
        )


def test_upgrade_seeds_the_permission_even_with_no_existing_holders(monkeypatch):
    """A database that never granted crm:quote:write still gets the key.

    Without the row the admin template hides the button and the route
    dependency fails closed, which is exactly how the feature shipped dark.
    """

    migration = _load("quote_send_permission_bare", "477_quote_send_permission.py")
    engine = sa.create_engine("sqlite://")
    tables = _schema(engine)

    with engine.begin() as connection:
        monkeypatch.setattr(migration.op, "get_bind", lambda: connection)
        migration.upgrade()

        assert (
            connection.execute(
                sa.select(tables["permissions"].c.is_ui_assignable).where(
                    tables["permissions"].c.key == TARGET_KEY
                )
            ).scalar_one()
            is True
        )


def test_send_permission_is_registered_in_the_seed_catalogue():
    """Migration and seed must agree, or fresh installs drift from upgrades."""

    from scripts.seed.seed_rbac import DEFAULT_PERMISSIONS

    assert TARGET_KEY in {key for key, _description in DEFAULT_PERMISSIONS}
