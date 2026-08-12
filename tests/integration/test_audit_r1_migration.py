"""Predecessor-to-524 proof for the additive kernel audit R1 shape.

The squashed bootstrap constructs current model metadata even when Alembic is
stopped at 523. This test therefore runs the real chain to 523, removes exactly
the three R1 columns to reconstruct the deployed predecessor shape, seeds a
historical row, and then runs the real 524 upgrade. SQLite or ``create_all``
would not prove the two-step PostgreSQL default behavior or JSONB shape.
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
from psycopg import sql
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session

from alembic import command
from app import config as app_config
from app.models.audit import AuditActorType, AuditEvent
from app.schemas.audit import AuditEventCreate
from app.services.audit import audit_events

PREDECESSOR = "523_domain_settings_tenant_fk"
REVISION = "524_audit_events_kernel_r1"


def _render(url: URL) -> str:
    return url.render_as_string(hide_password=False)


def _psycopg_url(url: URL) -> str:
    return _render(url.set(drivername="postgresql"))


@pytest.fixture
def isolated_database() -> Iterator[URL]:
    configured = os.getenv("TEST_DATABASE_URL")
    if not configured:
        raise pytest.UsageError("audit R1 migration test requires TEST_DATABASE_URL")
    base_url = make_url(configured)
    if not base_url.drivername.startswith("postgresql"):
        raise pytest.UsageError("audit R1 migration test requires PostgreSQL")

    name = f"dotmac_audit_r1_{uuid4().hex}"
    maintenance = base_url.set(database="postgres")
    with psycopg.connect(_psycopg_url(maintenance), autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        yield base_url.set(database=name)
    finally:
        with psycopg.connect(_psycopg_url(maintenance), autocommit=True) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name))
            )


def _config() -> Config:
    config = Config("alembic.ini")
    config.set_main_option("script_location", "alembic")
    return config


def test_524_preserves_unknown_history_and_defaults_only_future_rows(
    isolated_database: URL,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        app_config,
        "settings",
        replace(app_config.settings, database_url=_render(isolated_database)),
    )
    config = _config()
    command.upgrade(config, PREDECESSOR)
    engine = create_engine(isolated_database)
    historical_id = uuid4()
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE audit_events "
                    "DROP COLUMN actor_party_id, "
                    "DROP COLUMN details, "
                    "DROP COLUMN created_at"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO audit_events "
                    "(id, actor_type, actor_id, action, entity_type, status_code, "
                    "is_success, is_active, occurred_at) "
                    "VALUES (:id, 'system', 'pre-r1', 'pre-r1', 'audit_event', "
                    "200, true, true, :occurred_at)"
                ),
                {"id": historical_id, "occurred_at": datetime.now(UTC)},
            )

        command.upgrade(config, REVISION)

        with Session(engine) as db:
            historical = db.get(AuditEvent, historical_id)
            assert historical is not None
            assert historical.actor_party_id is None
            assert historical.details is None
            assert historical.created_at is None

            event = audit_events.stage(
                db,
                AuditEventCreate(
                    actor_type=AuditActorType.service,
                    actor_id="service:audit-r1-rehearsal",
                    action="audit_r1_rehearsal",
                    entity_type="audit_event",
                    metadata_={"source": "migration-test"},
                    ip_address="192.0.2.20",
                    user_agent="audit-r1-rehearsal",
                ),
            )
            db.commit()
            db.refresh(event)

            assert event.created_at is not None
            assert event.details == {
                "source": "migration-test",
                "ip_address": "192.0.2.20",
                "user_agent": "audit-r1-rehearsal",
            }
            report = audit_events.r1_parity(db)
            assert report.historical_rows_without_created_at >= 1
            assert report.r1_rows == 1
            assert report.status == "parity"

        with engine.connect() as connection:
            columns = {
                row.column_name: (row.data_type, row.is_nullable, row.column_default)
                for row in connection.execute(
                    text(
                        "SELECT column_name, data_type, is_nullable, column_default "
                        "FROM information_schema.columns "
                        "WHERE table_schema = 'public' "
                        "AND table_name = 'audit_events' "
                        "AND column_name IN "
                        "('actor_party_id', 'details', 'created_at')"
                    )
                )
            }
            assert columns["actor_party_id"][:2] == ("uuid", "YES")
            assert columns["details"][:2] == ("jsonb", "YES")
            assert columns["created_at"][:2] == (
                "timestamp with time zone",
                "YES",
            )
            assert "now()" in str(columns["created_at"][2]).lower()
            actor_fks = connection.execute(
                text(
                    "SELECT count(*) FROM information_schema.key_column_usage "
                    "WHERE table_schema = 'public' "
                    "AND table_name = 'audit_events' "
                    "AND column_name = 'actor_party_id'"
                )
            ).scalar_one()
            assert actor_fks == 0
    finally:
        engine.dispose()
