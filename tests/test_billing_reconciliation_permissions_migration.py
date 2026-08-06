"""Grant contracts for the billing reconciliation permission migration.

The reconciliation routes gate on keys that lived only in ``seed_rbac.py``, so
no deployed database ever had them and the surface was unreachable for every
principal without the ``*`` wildcard. These tests pin the repair and tie the
seeded keys to the module constants the routes actually enforce, so a rename
cannot silently reopen the gap.
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

SOURCE_KEY = "billing:invoice:update"
READ_KEY = "billing:reconciliation:read"
WRITE_KEY = "billing:reconciliation:write"
MIGRATION = "481_billing_reconciliation_permissions.py"


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


def _seed_source_holders(connection, tables, ids, now) -> None:
    connection.execute(
        tables["permissions"].insert(),
        [
            {
                "id": ids["source"],
                "key": SOURCE_KEY,
                "description": "Update invoices",
                "is_active": True,
                "is_ui_assignable": True,
                "created_at": now,
                "updated_at": now,
            }
        ],
    )
    connection.execute(
        tables["role_permissions"].insert(),
        {"id": str(uuid4()), "role_id": ids["role"], "permission_id": ids["source"]},
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


def test_migration_extends_the_single_head_chain():
    migration = _load("billing_recon_chain", MIGRATION)
    parent = _load("billing_recon_parent", "480_quote_discount_history.py")

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


def test_upgrade_grants_both_keys_to_every_invoice_editor(monkeypatch):
    migration = _load("billing_recon_grants", MIGRATION)
    engine = sa.create_engine("sqlite://")
    tables = _schema(engine)
    ids = {
        "source": str(uuid4()),
        "role": str(uuid4()),
        "subscriber": str(uuid4()),
        "system_user": str(uuid4()),
    }
    now = datetime.now(UTC)
    expected = {READ_KEY, WRITE_KEY}

    with engine.begin() as connection:
        _seed_source_holders(connection, tables, ids, now)
        monkeypatch.setattr(migration.op, "get_bind", lambda: connection)

        migration.upgrade()

        assert (
            _grant_keys(connection, tables["role_permissions"], "role_id", ids["role"])
            >= expected
        )
        assert (
            _grant_keys(
                connection,
                tables["subscriber_permissions"],
                "subscriber_id",
                ids["subscriber"],
            )
            >= expected
        )
        assert (
            _grant_keys(
                connection,
                tables["system_user_permissions"],
                "system_user_id",
                ids["system_user"],
            )
            >= expected
        )

        # Re-running must not duplicate permissions or grants.
        migration.upgrade()
        assert (
            connection.execute(
                sa.select(sa.func.count())
                .select_from(tables["permissions"])
                .where(tables["permissions"].c.key.in_(sorted(expected)))
            ).scalar_one()
            == 2
        )
        assert (
            connection.execute(
                sa.select(sa.func.count()).select_from(tables["role_permissions"])
            ).scalar_one()
            == 3
        )

        migration.downgrade()
        remaining = _grant_keys(
            connection, tables["role_permissions"], "role_id", ids["role"]
        )
        assert not (remaining & expected)
        # The authority the grants were derived from is never collateral damage.
        assert SOURCE_KEY in remaining


def test_upgrade_seeds_the_keys_even_with_no_existing_holders(monkeypatch):
    """A database that never granted the source key still gets both rows.

    Without the rows the routes fail closed for everyone but the wildcard,
    which is exactly how this surface shipped dark.
    """

    migration = _load("billing_recon_bare", MIGRATION)
    engine = sa.create_engine("sqlite://")
    tables = _schema(engine)

    with engine.begin() as connection:
        monkeypatch.setattr(migration.op, "get_bind", lambda: connection)
        migration.upgrade()

        rows = connection.execute(
            sa.select(
                tables["permissions"].c.key, tables["permissions"].c.is_ui_assignable
            ).where(tables["permissions"].c.key.in_([READ_KEY, WRITE_KEY]))
        ).all()
        assert {row[0] for row in rows} == {READ_KEY, WRITE_KEY}
        assert all(row[1] is True for row in rows)


def test_seeded_keys_match_the_constants_the_routes_enforce():
    """The migration, the seed catalogue and the route constants must agree.

    The original gap survived CI because these routes reference the keys
    through module constants, which the literal-only parity guard cannot see.
    """

    from app.services import web_prepaid_billing_calendar_reconciliation as service
    from scripts.seed.seed_rbac import DEFAULT_PERMISSIONS

    seeded = {key for key, _description in DEFAULT_PERMISSIONS}
    migration = _load("billing_recon_keys", MIGRATION)
    migrated = {key for key, _description in migration.TARGET_KEYS}

    assert service.READ_PERMISSION == READ_KEY
    assert service.WRITE_PERMISSION == WRITE_KEY
    assert {service.READ_PERMISSION, service.WRITE_PERMISSION} == migrated
    assert migrated <= seeded
