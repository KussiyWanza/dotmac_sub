"""Exercise the PostgreSQL migration boundary that introduces revisions 430-434."""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from psycopg import sql
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL, make_url

from alembic import command
from app import config as app_config

ROOT = Path(__file__).resolve().parents[2]
REVISION_423 = "423_prepaid_opening_funding_reconciliation"

TABLES_430_TO_434 = (
    "billing_contracts",
    "billing_contract_versions",
    "billing_contract_lines",
    "billing_obligations",
    "customer_posting_groups",
    "customer_position_effects",
    "owner_output_receipts",
    "durable_timers",
    "collections_cases",
    "sales_order_funding_gates",
    "sales_order_funding_obligations",
    "erp_billing_exports",
)

ENUMS_430_TO_434 = (
    "billingrecordauthority",
    "billingcontractsourcekind",
    "billingcontractversionstatus",
    "ratebasis",
    "intervalunit",
    "collectiontiming",
    "cadencealignment",
    "endofmonthrule",
    "prorationpolicy",
    "chargecomponent",
    "accountingtreatment",
    "obligationstate",
    "obligationresolutionkind",
    "postingcommandkind",
    "positioneffectkind",
    "receiptoutcome",
    "timerstatus",
    "collectionsreason",
    "collectionscasestate",
    "fundinggatestate",
    "erpbillingflow",
    "erpexportstatus",
)


def _render_url(url: URL) -> str:
    return url.render_as_string(hide_password=False)


def _psycopg_url(url: URL) -> str:
    return _render_url(url.set(drivername="postgresql"))


@pytest.fixture
def isolated_migration_database() -> Iterator[URL]:
    configured_url = os.getenv("TEST_DATABASE_URL")
    if not configured_url:
        pytest.skip("migration-path test requires TEST_DATABASE_URL")
    base_url = make_url(configured_url)
    if not base_url.drivername.startswith("postgresql"):
        pytest.skip("migration-path test requires PostgreSQL")

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
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    return config


def _revision_rows(database_url: URL) -> set[str]:
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            return set(
                connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalars()
            )
    finally:
        engine.dispose()


def _restore_pre_430_shape(database_url: URL) -> None:
    """Undo current-model objects created by the squashed initial migration.

    Revision 001 builds ``Base.metadata`` from the current checkout, so even an
    upgrade stopping at 423 contains later model tables. Removing only the
    objects introduced by 430-434 recreates the production boundary while
    preserving the real revision-423 stamp and all earlier schema.
    """

    with psycopg.connect(_psycopg_url(database_url), autocommit=True) as connection:
        for table_name in reversed(TABLES_430_TO_434):
            connection.execute(
                sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(
                    sql.Identifier(table_name)
                )
            )
        for enum_name in reversed(ENUMS_430_TO_434):
            connection.execute(
                sql.SQL("DROP TYPE IF EXISTS {} CASCADE").format(
                    sql.Identifier(enum_name)
                )
            )


def test_postgres_upgrades_revision_423_through_current_head(
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

    command.upgrade(config, REVISION_423)
    assert _revision_rows(database_url) == {REVISION_423}
    _restore_pre_430_shape(database_url)
    assert _revision_rows(database_url) == {REVISION_423}

    command.upgrade(config, "head")

    expected_heads = set(ScriptDirectory.from_config(config).get_heads())
    assert _revision_rows(database_url) == expected_heads
    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        table_names = set(inspector.get_table_names())
        assert set(TABLES_430_TO_434) <= table_names

        with engine.connect() as connection:
            enum_names = list(
                connection.execute(
                    text(
                        """
                        SELECT type.typname
                        FROM pg_type AS type
                        JOIN pg_namespace AS namespace
                          ON namespace.oid = type.typnamespace
                        WHERE namespace.nspname = current_schema()
                          AND type.typtype = 'e'
                        """
                    )
                ).scalars()
            )
        for enum_name in ENUMS_430_TO_434:
            assert enum_names.count(enum_name) == 1
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    assert _revision_rows(database_url) == expected_heads
