"""Exercise both deployed histories across the 501 compatibility repair."""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import replace
from uuid import uuid4

import psycopg
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from psycopg import sql
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL, Engine, make_url

from alembic import command
from app import config as app_config

REVISION_500 = "500_reconcile_staff_notification_inbox"
REVISION_501 = "501_retire_allowance_throttle_rate"
TABLE = "usage_allowances"
COLUMN = "throttle_rate_mbps"


def _render_url(url: URL) -> str:
    return url.render_as_string(hide_password=False)


def _psycopg_url(url: URL) -> str:
    return _render_url(url.set(drivername="postgresql"))


@pytest.fixture
def engine() -> Iterator[Engine]:
    """Satisfy the package guard while this test owns a separate database."""

    configured_url = os.getenv("TEST_DATABASE_URL")
    if not configured_url:
        raise pytest.UsageError("migration-path test requires TEST_DATABASE_URL")
    database_url = make_url(configured_url)
    if not database_url.drivername.startswith("postgresql"):
        raise pytest.UsageError("migration-path test requires PostgreSQL")
    test_engine = create_engine(database_url)
    try:
        yield test_engine
    finally:
        test_engine.dispose()


@pytest.fixture
def isolated_migration_database() -> Iterator[URL]:
    configured_url = os.getenv("TEST_DATABASE_URL")
    if not configured_url:
        raise pytest.UsageError("migration-path test requires TEST_DATABASE_URL")
    base_url = make_url(configured_url)
    if not base_url.drivername.startswith("postgresql"):
        raise pytest.UsageError("migration-path test requires PostgreSQL")

    database_name = f"dotmac_migration_{uuid4().hex}"
    maintenance_url = base_url.set(database="postgres")
    with psycopg.connect(_psycopg_url(maintenance_url), autocommit=True) as admin:
        admin.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name))
        )

    try:
        yield base_url.set(database=database_name)
    finally:
        with psycopg.connect(
            _psycopg_url(maintenance_url),
            autocommit=True,
        ) as admin:
            admin.execute(
                """
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE datname = %s
                  AND pid <> pg_backend_pid()
                """,
                (database_name,),
            )
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(
                    sql.Identifier(database_name)
                )
            )


def _alembic_config() -> Config:
    config = Config("alembic.ini")
    config.set_main_option("script_location", "alembic")
    return config


def _revision_rows(database_url: URL) -> set[str]:
    test_engine = create_engine(database_url)
    try:
        with test_engine.connect() as connection:
            return set(
                connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalars()
            )
    finally:
        test_engine.dispose()


def _column_exists(database_url: URL) -> bool:
    test_engine = create_engine(database_url)
    try:
        return COLUMN in {
            column["name"] for column in inspect(test_engine).get_columns(TABLE)
        }
    finally:
        test_engine.dispose()


def test_postgres_drops_the_obsolete_column_and_keeps_501_resolvable(
    isolated_migration_database: URL,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = isolated_migration_database
    monkeypatch.setattr(
        app_config,
        "settings",
        replace(app_config.settings, database_url=_render_url(database_url)),
    )
    config = _alembic_config()

    command.upgrade(config, REVISION_500)
    assert _revision_rows(database_url) == {REVISION_500}

    # Recreate the incremental deployed shape that still carries the retired
    # allowance-level decision. A squashed fresh baseline may already omit it.
    with psycopg.connect(_psycopg_url(database_url), autocommit=True) as connection:
        connection.execute(
            sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS {} INTEGER").format(
                sql.Identifier(TABLE),
                sql.Identifier(COLUMN),
            )
        )
    assert _column_exists(database_url)

    command.upgrade(config, "head")

    # Single-headedness and 501's presence in the head's ancestry — NOT a
    # literal head revision, which every later migration would falsify. See
    # tests/architecture/test_migration_chain_assertions.py.
    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    assert len(heads) == 1
    assert REVISION_501 in {
        item.revision
        for item in script.iterate_revisions(heads[0], REVISION_501, inclusive=True)
    }
    expected_heads = set(heads)
    assert _revision_rows(database_url) == expected_heads
    assert not _column_exists(database_url)

    # A database that already recorded 501 remains resolvable, and moving the
    # marker backward for compatibility checks does not recreate old data.
    command.upgrade(config, "head")
    assert _revision_rows(database_url) == expected_heads
    command.downgrade(config, REVISION_500)
    assert _revision_rows(database_url) == {REVISION_500}
    assert not _column_exists(database_url)

    command.upgrade(config, "head")
    assert _revision_rows(database_url) == expected_heads
    assert not _column_exists(database_url)
