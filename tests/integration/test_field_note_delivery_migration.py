"""PostgreSQL predecessor-to-head proof for field-note idempotency."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from alembic.config import Config
from psycopg import sql
from sqlalchemy import create_engine
from sqlalchemy.engine import URL

from alembic import command
from app import config as app_config
from scripts.ci.migrated_test_database import require_migrated_schema

ROOT = Path(__file__).resolve().parents[2]
PREDECESSOR = "590_olt_observation_read_status"
CANDIDATE = "591_field_note_delivery_idempotency"
INDEX = "uq_field_work_order_notes_author_client_ref"


def _render(url: URL) -> str:
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def _upgrade(revision: str) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(config, revision)


@pytest.fixture
def predecessor_database(
    template_base_url: URL, monkeypatch: pytest.MonkeyPatch
) -> Iterator[URL]:
    name = f"dotmac_test_field_note_delivery_{uuid4().hex}"
    maintenance = template_base_url.set(drivername="postgresql", database="postgres")
    with psycopg.connect(_render(maintenance), autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    target = template_base_url.set(database=name)
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


def test_revision_591_adds_retry_identity_then_reaches_head(
    predecessor_database: URL,
) -> None:
    _upgrade(PREDECESSOR)
    # Revision 001 creates a fresh database from current model metadata. Remove
    # the current-model additions to reconstruct the real deployed predecessor
    # before rehearsing the incremental upgrade boundary.
    with psycopg.connect(_render(predecessor_database)) as connection:
        connection.execute(f"DROP INDEX IF EXISTS {INDEX}")
        connection.execute(
            "ALTER TABLE field_work_order_notes DROP COLUMN IF EXISTS client_ref"
        )
        connection.commit()
    with psycopg.connect(_render(predecessor_database)) as connection:
        before = connection.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'field_work_order_notes' AND column_name = 'client_ref'"
        ).fetchone()
    assert before is None

    _upgrade(CANDIDATE)
    with psycopg.connect(_render(predecessor_database)) as connection:
        nullable = connection.execute(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = 'field_work_order_notes' AND column_name = 'client_ref'"
        ).fetchone()
        index_definition = connection.execute(
            "SELECT indexdef FROM pg_indexes WHERE tablename = "
            "'field_work_order_notes' AND indexname = %s",
            (INDEX,),
        ).fetchone()

    assert nullable == ("YES",)
    assert index_definition is not None
    normalized = " ".join(index_definition[0].split())
    assert "UNIQUE INDEX" in normalized
    assert "(author_system_user_id, client_ref)" in normalized
    assert "WHERE (client_ref IS NOT NULL)" in normalized

    _upgrade("heads")
    engine = create_engine(predecessor_database)
    try:
        require_migrated_schema(engine)
    finally:
        engine.dispose()
