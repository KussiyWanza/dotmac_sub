"""Fresh and deployed-527 PostgreSQL proofs for Roles R1 migration 528."""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from alembic.config import Config
from psycopg import sql
from sqlalchemy.engine import URL, make_url

from alembic import command
from app import config as app_config
from app.services.operator_tenant import OPERATOR_TENANT_ID

ROOT = Path(__file__).resolve().parents[2]
PREDECESSOR = "527_credential_party_binding_additive"
CANDIDATE = "528_roles_kernel_r1_additive"


def _render(url: URL) -> str:
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


@pytest.fixture
def migrated_database(monkeypatch: pytest.MonkeyPatch) -> Iterator[URL]:
    configured = os.getenv("TEST_DATABASE_URL")
    if not configured:
        raise pytest.UsageError("migration-path test requires TEST_DATABASE_URL")
    base = make_url(configured)
    if not base.drivername.startswith("postgresql"):
        raise pytest.UsageError("migration-path test requires PostgreSQL")
    name = f"dotmac_roles_r1_{uuid4().hex}"
    maintenance = base.set(database="postgres")
    with psycopg.connect(_render(maintenance), autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    target = base.set(database=name)
    monkeypatch.setattr(
        app_config,
        "settings",
        replace(
            app_config.settings,
            database_url=target.render_as_string(hide_password=False),
        ),
    )
    try:
        yield target
    finally:
        with psycopg.connect(_render(maintenance), autocommit=True) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name))
            )


def _alembic(revision: str, *, downgrade: bool = False) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    if downgrade:
        command.downgrade(config, revision)
    else:
        command.upgrade(config, revision)


def _roles_contract(
    url: URL,
) -> tuple[dict[str, tuple[str | None, int | None]], dict[str, str], set[str]]:
    with psycopg.connect(_render(url)) as connection:
        columns = {
            row[0]: (row[1], row[2])
            for row in connection.execute(
                "SELECT column_name, column_default, character_maximum_length "
                "FROM information_schema.columns WHERE table_schema = 'public' "
                "AND table_name = 'roles'"
            )
        }
        constraints = {
            row[0]: row[1]
            for row in connection.execute(
                "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid = 'public.roles'::regclass"
            )
        }
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname = 'public' "
                "AND tablename = 'roles'"
            )
        }
    return columns, constraints, indexes


def _assert_candidate_contract(url: URL) -> None:
    columns, constraints, indexes = _roles_contract(url)

    assert columns["name"][1] == 120
    assert columns["slug"][1] == 63
    assert columns["created_at"][0] is not None
    assert columns["updated_at"][0] is not None
    assert {
        "ck_roles_kernel_identity_projection",
        "fk_roles_tenant",
        "uq_roles_tenant_slug",
        "uq_roles_tenant_id_id",
    } <= constraints.keys()
    assert constraints["fk_roles_tenant"].endswith("ON DELETE CASCADE")
    assert "UNIQUE (tenant_id, slug)" in constraints["uq_roles_tenant_slug"]
    assert "UNIQUE (tenant_id, id)" in constraints["uq_roles_tenant_id_id"]
    assert "ix_roles_tenant_id" in indexes


def _restore_deployed_527_roles_shape(url: URL) -> None:
    """Undo current-model leakage from the squashed base before rehearsal."""

    with psycopg.connect(_render(url), autocommit=True) as connection:
        connection.execute("ALTER TABLE roles DROP COLUMN IF EXISTS slug CASCADE")
        connection.execute("ALTER TABLE roles DROP COLUMN IF EXISTS tenant_id CASCADE")
        connection.execute("ALTER TABLE roles ALTER COLUMN name TYPE VARCHAR(80)")
        connection.execute("ALTER TABLE roles ALTER COLUMN created_at DROP DEFAULT")
        connection.execute("ALTER TABLE roles ALTER COLUMN updated_at DROP DEFAULT")


def test_fresh_head_has_exact_kernel_role_expand_contract(
    migrated_database: URL,
) -> None:
    _alembic("head")

    _assert_candidate_contract(migrated_database)

    with psycopg.connect(_render(migrated_database)) as connection:
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                "INSERT INTO roles "
                "(id, name, tenant_id, description, is_active, created_at, updated_at) "
                "VALUES (%s, %s, %s, NULL, true, now(), now())",
                (uuid4(), "half_projected", OPERATOR_TENANT_ID),
            )
        connection.rollback()


def test_deployed_527_upgrade_preserves_roles_and_downgrades_cleanly(
    migrated_database: URL,
) -> None:
    _alembic(PREDECESSOR)
    _restore_deployed_527_roles_shape(migrated_database)
    role_id = uuid4()
    with psycopg.connect(_render(migrated_database)) as connection:
        connection.execute(
            "INSERT INTO roles "
            "(id, name, description, is_active, created_at, updated_at) "
            "VALUES (%s, %s, %s, true, '2026-08-01T00:00:00Z', "
            "'2026-08-02T00:00:00Z')",
            (role_id, "Legacy NOC", "preserve me"),
        )
        connection.commit()

    _alembic(CANDIDATE)

    _assert_candidate_contract(migrated_database)
    with psycopg.connect(_render(migrated_database)) as connection:
        row = connection.execute(
            "SELECT id, name, description, is_active, tenant_id, slug, "
            "created_at::text, updated_at::text FROM roles WHERE id = %s",
            (role_id,),
        ).fetchone()
    assert row is not None
    assert row[:6] == (role_id, "Legacy NOC", "preserve me", True, None, None)
    assert row[6].startswith("2026-08-01 00:00:00")
    assert row[7].startswith("2026-08-02 00:00:00")

    _alembic(PREDECESSOR, downgrade=True)

    columns, constraints, indexes = _roles_contract(migrated_database)
    assert {"tenant_id", "slug"}.isdisjoint(columns)
    assert columns["name"][1] == 80
    assert columns["created_at"][0] is None
    assert columns["updated_at"][0] is None
    assert "uq_roles_tenant_slug" not in constraints
    assert "uq_roles_tenant_id_id" not in constraints
    assert "ix_roles_tenant_id" not in indexes
