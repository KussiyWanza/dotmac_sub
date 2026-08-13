"""Predecessor-to-candidate proof for the open setting-domain vocabulary.

Real PostgreSQL, real migration chain: build the schema at 501 with the native
``settingdomain`` enum still in place, write rows through it — including one
under the domain that is about to be retired — then upgrade and assert the
values survived, the type is gone, and a domain that never existed as an enum
member is now writable.

Fresh metadata creation or SQLite is not evidence for any of that: on SQLite
the column is already VARCHAR, so every assertion below would pass vacuously.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import psycopg
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from psycopg import sql
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url

from alembic import command
from app import config as app_config

REVISION_501 = "501_retire_allowance_throttle_rate"
REVISION_502 = "502_open_setting_domain_vocabulary"
ENUM_NAME = "settingdomain"
RETIRED_DOMAIN = "subscription_engine"
NEW_DOMAIN = "a_domain_the_enum_never_had"


def _render_url(url: URL) -> str:
    return url.render_as_string(hide_password=False)


def _psycopg_url(url: URL) -> str:
    return _render_url(url.set(drivername="postgresql"))


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
        with psycopg.connect(_psycopg_url(maintenance_url), autocommit=True) as admin:
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


def _scalar(database_url: URL, statement: str, **params: object) -> object:
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            return connection.execute(text(statement), params).scalar()
    finally:
        engine.dispose()


def _column_type(database_url: URL) -> str:
    return str(
        _scalar(
            database_url,
            """
            SELECT data_type FROM information_schema.columns
            WHERE table_name = 'domain_settings' AND column_name = 'domain'
            """,
        )
    )


def _enum_exists(database_url: URL) -> bool:
    return bool(
        _scalar(
            database_url,
            "SELECT 1 FROM pg_type WHERE typname = :name",
            name=ENUM_NAME,
        )
    )


# Two whole literal statements rather than one interpolated: the only
# difference is whether the domain is cast through the enum, and splicing that
# in would be a string-built query for no gain.
#
# `created_at`/`updated_at` are NOT NULL with PYTHON-side defaults, so a
# statement that goes round the ORM — which every insert here does, on purpose,
# to write exactly what the schema of the moment allows — has to supply them.
_INSERT_AS_ENUM = (
    "INSERT INTO domain_settings "
    "(id, domain, key, value_type, value_text, is_secret, is_active, "
    "created_at, updated_at) "
    "VALUES (:id, CAST(:domain AS settingdomain), :key, 'string', :value, "
    "false, true, :now, :now)"
)
_INSERT_AS_TEXT = (
    "INSERT INTO domain_settings "
    "(id, domain, key, value_type, value_text, is_secret, is_active, "
    "created_at, updated_at) "
    "VALUES (:id, :domain, :key, 'string', :value, false, true, :now, :now)"
)


def _insert(database_url: URL, domain: str, key: str, *, cast_enum: bool) -> None:
    engine = create_engine(database_url)
    statement = _INSERT_AS_ENUM if cast_enum else _INSERT_AS_TEXT
    try:
        with engine.begin() as connection:
            connection.execute(
                text(statement),
                {
                    "id": str(uuid4()),
                    "domain": domain,
                    "key": key,
                    "value": f"value-for-{key}",
                    "now": datetime.now(UTC),
                },
            )
    finally:
        engine.dispose()


def _value(database_url: URL, key: str) -> object:
    return _scalar(
        database_url,
        "SELECT value_text FROM domain_settings WHERE key = :key",
        key=key,
    )


def _domain(database_url: URL, key: str) -> object:
    return _scalar(
        database_url,
        "SELECT domain::text FROM domain_settings WHERE key = :key",
        key=key,
    )


def test_the_enum_becomes_open_text_and_keeps_every_row(
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

    command.upgrade(config, REVISION_501)

    # 001 restores the retired enum precisely so the chain up to 501 replays
    # against the shape it was authored for. Asserting it here is also the
    # regression test for that restoration: without it the chain dies at 144
    # with `type "settingdomain" does not exist`.
    assert _enum_exists(database_url), "predecessor must still carry the enum type"
    assert _column_type(database_url) == "USER-DEFINED"

    # A live row and a row under the domain this change retires. Both are cast
    # through the enum, i.e. written exactly as the predecessor schema allowed.
    _insert(database_url, "auth", "kept_live", cast_enum=True)
    _insert(database_url, RETIRED_DOMAIN, "kept_retired", cast_enum=True)

    command.upgrade(config, "head")

    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    assert len(heads) == 1
    assert REVISION_502 in {
        item.revision
        for item in script.iterate_revisions(heads[0], REVISION_502, inclusive=True)
    }

    assert _column_type(database_url) == "character varying"
    assert not _enum_exists(database_url), (
        "nothing else depended on the type, so it must be gone"
    )

    # Every value preserved, including the retired domain's.
    assert _value(database_url, "kept_live") == "value-for-kept_live"
    assert _domain(database_url, "kept_live") == "auth"
    assert _value(database_url, "kept_retired") == "value-for-kept_retired"
    assert _domain(database_url, "kept_retired") == RETIRED_DOMAIN

    # A domain that was never an enum member now stores, with no ALTER TYPE.
    _insert(database_url, NEW_DOMAIN, "brand_new", cast_enum=False)
    assert _domain(database_url, "brand_new") == NEW_DOMAIN


def test_the_downgrade_restores_the_enum(
    isolated_migration_database: URL,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Destructive by design, but it must not lose a legacy-member row."""

    database_url = isolated_migration_database
    monkeypatch.setattr(
        app_config,
        "settings",
        replace(app_config.settings, database_url=_render_url(database_url)),
    )
    config = _alembic_config()

    command.upgrade(config, "head")
    _insert(database_url, RETIRED_DOMAIN, "legacy_member", cast_enum=False)

    command.downgrade(config, REVISION_501)

    assert _enum_exists(database_url)
    assert _column_type(database_url) == "USER-DEFINED"
    assert _domain(database_url, "legacy_member") == RETIRED_DOMAIN
