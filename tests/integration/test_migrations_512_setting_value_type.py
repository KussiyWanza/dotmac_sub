"""Predecessor-to-candidate proof for the open setting-value-type vocabulary.

Real PostgreSQL, real migration chain. Sibling of
``test_migrations_502_setting_domain``; the claim is the same shape one column
across, but the predecessor state has to be built differently and that
difference is the interesting part.

502 could assert its predecessor directly because migration ``001`` restores the
``settingdomain`` type — roughly fifteen revisions between 001 and 501 name it,
so a fresh chain cannot replay without it. NOTHING in the chain names
``settingvaluetype``: it only ever existed because the models declared
``Enum(SettingValueType)``, and ``create_all`` emitted it. The moment those
models became ``SettingValueTypeType`` a fresh database stopped having the type
at all — while every DEPLOYED database still has it, on three tables.

So a fresh chain is not evidence here. ``_install_deployed_shape`` reconstructs
what production actually looks like at 511 — the native enum on all three
columns and the old value-alignment CHECK that names ``json`` — and 512 is run
against that. The second test covers the other real case: a database built
after the model change, where 512 must be a well-behaved no-op rather than an
error.

The behavioural claim being proved is not "the column is text now". It is that
a SECOND JSON-stored value type can be written, which the old CHECK made
impossible for any type but ``json`` no matter who declared it.
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

REVISION_511 = "511_sales_order_invoice_links"
REVISION_512 = "512_open_setting_value_type_vocabulary"
ENUM_NAME = "settingvaluetype"
ALIGNMENT_CONSTRAINT = "ck_domain_settings_value_alignment"

#: The members the deployed type carries.
LEGACY_MEMBERS = ("string", "integer", "boolean", "json")

#: Every column that carries the enum in a deployed database.
LEGACY_COLUMNS = (
    ("domain_settings", "value_type"),
    ("subscriber_custom_fields", "value_type"),
    ("subscription_engine_settings", "value_type"),
)

#: A type the enum never had, and one that stores in the JSON column — the
#: combination the old CHECK rejected outright.
NEW_JSON_TYPE = "list"


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


def _use_database(monkeypatch: pytest.MonkeyPatch, database_url: URL) -> None:
    monkeypatch.setattr(
        app_config,
        "settings",
        replace(app_config.settings, database_url=_render_url(database_url)),
    )


def _execute(database_url: URL, statement: str, **params: object) -> None:
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(text(statement), params)
    finally:
        engine.dispose()


def _scalar(database_url: URL, statement: str, **params: object) -> object:
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            return connection.execute(text(statement), params).scalar()
    finally:
        engine.dispose()


def _column_type(database_url: URL, table: str) -> str:
    return str(
        _scalar(
            database_url,
            """
            SELECT data_type FROM information_schema.columns
            WHERE table_name = :table AND column_name = 'value_type'
            """,
            table=table,
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


def _alignment_check(database_url: URL) -> str:
    return str(
        _scalar(
            database_url,
            """
            SELECT pg_get_constraintdef(oid) FROM pg_constraint
            WHERE conname = :name
            """,
            name=ALIGNMENT_CONSTRAINT,
        )
    )


def _install_deployed_shape(database_url: URL) -> None:
    """Reconstruct what a database migrated before the model change looks like.

    Not a fixture of convenience: this IS the production predecessor. Every Sub
    database in service carries the native enum on these three columns and the
    ``json``-naming CHECK, because they were created by ``create_all`` from
    models that declared ``Enum(SettingValueType)``. A fresh chain no longer
    produces it, so without this the assertions below would pass vacuously
    against a shape no deployment has.
    """

    members = ", ".join(f"'{member}'" for member in LEGACY_MEMBERS)
    _execute(database_url, f"CREATE TYPE {ENUM_NAME} AS ENUM ({members})")
    for table, column in LEGACY_COLUMNS:
        _execute(
            database_url,
            f"ALTER TABLE {table} ALTER COLUMN {column} "
            f"TYPE {ENUM_NAME} USING {column}::{ENUM_NAME}",
        )
    _execute(
        database_url,
        f"ALTER TABLE domain_settings DROP CONSTRAINT IF EXISTS {ALIGNMENT_CONSTRAINT}",
    )
    _execute(
        database_url,
        f"ALTER TABLE domain_settings ADD CONSTRAINT {ALIGNMENT_CONSTRAINT} CHECK ("
        f"(value_type = 'json' AND value_json IS NOT NULL AND value_text IS NULL) "
        f"OR (value_type != 'json' AND value_text IS NOT NULL))",
    )


_INSERT_TEXT_VALUE = (
    "INSERT INTO domain_settings "
    "(id, domain, key, value_type, value_text, is_secret, is_active, "
    "created_at, updated_at) "
    "VALUES (:id, :domain, :key, :value_type, :value, false, true, :now, :now)"
)

_INSERT_JSON_VALUE = (
    "INSERT INTO domain_settings "
    "(id, domain, key, value_type, value_json, is_secret, is_active, "
    "created_at, updated_at) "
    "VALUES (:id, :domain, :key, :value_type, CAST(:value AS json), "
    "false, true, :now, :now)"
)


def _insert_text(database_url: URL, key: str, value_type: str) -> None:
    _execute(
        database_url,
        _INSERT_TEXT_VALUE,
        id=str(uuid4()),
        domain="audit",
        key=key,
        value_type=value_type,
        value=f"value-for-{key}",
        now=datetime.now(UTC),
    )


def _insert_json(database_url: URL, key: str, value_type: str, value: str) -> None:
    _execute(
        database_url,
        _INSERT_JSON_VALUE,
        id=str(uuid4()),
        domain="audit",
        key=key,
        value_type=value_type,
        value=value,
        now=datetime.now(UTC),
    )


def _stored_type(database_url: URL, key: str) -> object:
    return _scalar(
        database_url,
        "SELECT value_type::text FROM domain_settings WHERE key = :key",
        key=key,
    )


def test_the_enum_becomes_open_text_and_a_second_json_type_becomes_writable(
    isolated_migration_database: URL,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = isolated_migration_database
    _use_database(monkeypatch, database_url)
    config = _alembic_config()

    command.upgrade(config, REVISION_511)
    _install_deployed_shape(database_url)

    for table, _ in LEGACY_COLUMNS:
        assert _column_type(database_url, table) == "USER-DEFINED"
    assert _enum_exists(database_url)

    # Rows written exactly as the predecessor schema allowed.
    _insert_text(database_url, "kept_string", "string")
    _insert_json(database_url, "kept_json", "json", '{"a": 1}')

    # The claim, stated as two failures first. Both must be impossible BEFORE
    # 512, and they are separate walls: the CHECK and the enum.
    #
    # 1. The CHECK reserved the JSON column for the type literally named
    #    `json`, so any other type storing a JSON value is rejected.
    with pytest.raises(Exception, match=ALIGNMENT_CONSTRAINT):
        _insert_json(database_url, "impossible_check", "string", '{"b": 2}')

    # 2. And a type the enum never had cannot even be named.
    with pytest.raises(Exception, match="invalid input value for enum"):
        _insert_text(database_url, "impossible_enum", NEW_JSON_TYPE)

    command.upgrade(config, "head")

    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    assert len(heads) == 1
    assert REVISION_512 in {
        item.revision
        for item in script.iterate_revisions(heads[0], REVISION_512, inclusive=True)
    }

    for table, _ in LEGACY_COLUMNS:
        assert _column_type(database_url, table) == "character varying", (
            f"{table} still carries the enum, so the type cannot be dropped"
        )
    assert not _enum_exists(database_url), (
        "all three dependants were converted, so the type must be gone"
    )

    # Values preserved, types preserved.
    assert _stored_type(database_url, "kept_string") == "string"
    assert _stored_type(database_url, "kept_json") == "json"

    # The CHECK no longer names a type.
    definition = _alignment_check(database_url)
    assert "json'" not in definition, definition
    assert "value_text" in definition and "value_json" in definition

    # And the thing that was impossible above now stores: a value type the enum
    # never had, holding a JSON value, with no ALTER TYPE anywhere.
    _insert_json(database_url, "brand_new", NEW_JSON_TYPE, '["POST", "PUT"]')
    assert _stored_type(database_url, "brand_new") == NEW_JSON_TYPE


def test_a_valueless_row_is_still_refused(
    isolated_migration_database: URL,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The replacement CHECK must still be a constraint, not a formality.

    Replacing a CHECK with a weaker one is an easy way to make a migration
    pass. What survives is "a row carries a value somewhere", and a row with
    neither column populated must still be rejected.
    """

    database_url = isolated_migration_database
    _use_database(monkeypatch, database_url)
    command.upgrade(_alembic_config(), "head")

    with pytest.raises(Exception, match=ALIGNMENT_CONSTRAINT):
        _execute(
            database_url,
            "INSERT INTO domain_settings "
            "(id, domain, key, value_type, value_text, value_json, is_secret, "
            "is_active, created_at, updated_at) "
            "VALUES (:id, 'audit', 'no_value', 'string', NULL, NULL, "
            "false, true, :now, :now)",
            id=str(uuid4()),
            now=datetime.now(UTC),
        )


def test_a_boolean_written_to_both_columns_is_accepted(
    isolated_migration_database: URL,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sub's actual storage convention, pinned so the cutover changes it on purpose.

    `normalize_for_db` writes a boolean to BOTH `value_text` and `value_json` —
    the seed did, the retired per-domain handlers did, and `_to_bool` in
    `app.main` reads `value_json` first, so a NULL there made a row's shape
    depend on its writer. The kernel's equivalent constraint is exactly-one and
    would reject this; adopting that is a storage-convention change owned by the
    settings cutover, not by this migration. Until then, dual-write is legal and
    this test says so out loud rather than leaving it to a passing CI run.
    """

    database_url = isolated_migration_database
    _use_database(monkeypatch, database_url)
    command.upgrade(_alembic_config(), "head")

    _execute(
        database_url,
        "INSERT INTO domain_settings "
        "(id, domain, key, value_type, value_text, value_json, is_secret, "
        "is_active, created_at, updated_at) "
        "VALUES (:id, 'audit', 'dual_written_boolean', 'boolean', 'true', "
        "CAST('true' AS json), false, true, :now, :now)",
        id=str(uuid4()),
        now=datetime.now(UTC),
    )

    assert _stored_type(database_url, "dual_written_boolean") == "boolean"


def test_a_database_built_after_the_model_change_migrates_cleanly(
    isolated_migration_database: URL,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other real case: no enum to convert, and 512 must not error.

    A fresh chain never creates ``settingvaluetype`` — the models stopped
    declaring it — so every branch in the migration has to tolerate its absence
    rather than assuming the deployed shape.
    """

    database_url = isolated_migration_database
    _use_database(monkeypatch, database_url)
    command.upgrade(_alembic_config(), "head")

    assert not _enum_exists(database_url)
    for table, _ in LEGACY_COLUMNS:
        assert _column_type(database_url, table) == "character varying"

    _insert_json(database_url, "fresh_list", NEW_JSON_TYPE, '["a"]')
    assert _stored_type(database_url, "fresh_list") == NEW_JSON_TYPE
