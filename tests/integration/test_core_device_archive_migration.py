"""Fresh and predecessor PostgreSQL proofs for core-device archive migration."""

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

ROOT = Path(__file__).resolve().parents[2]
PREDECESSOR = "532_sales_order_waivers"
CANDIDATE = "533_core_device_archive"


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
    name = f"dotmac_core_device_archive_{uuid4().hex}"
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


def _archive_contract(url: URL) -> tuple[set[str], set[str], set[str]]:
    with psycopg.connect(_render(url)) as connection:
        columns = {
            row[0]
            for row in connection.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' "
                "AND table_name = 'network_devices'"
            )
        }
        constraints = {
            row[0]
            for row in connection.execute(
                "SELECT conname FROM pg_constraint WHERE conrelid = "
                "'public.network_devices'::regclass"
            )
        }
        projection_constraints = {
            row[0]: row[1]
            for row in connection.execute(
                "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid = 'public.device_projections'::regclass"
            )
        }
    return columns, constraints, set(projection_constraints.values())


def _assert_candidate_contract(url: URL) -> None:
    columns, constraints, projection_constraints = _archive_contract(url)
    assert {"archived_at", "archived_by", "archive_reason"} <= columns
    assert "ck_network_device_archive_state" in constraints
    assert any("archived" in definition for definition in projection_constraints)
    with psycopg.connect(_render(url)) as connection:
        permission = connection.execute(
            "SELECT key, is_active, is_ui_assignable FROM permissions "
            "WHERE key = 'network:device:archive'"
        ).fetchone()
    assert permission == ("network:device:archive", True, True)


def test_fresh_head_has_core_device_archive_contract(
    migrated_database: URL,
) -> None:
    _alembic("head")
    _assert_candidate_contract(migrated_database)


def test_predecessor_upgrade_preserves_device_and_downgrade_fails_closed(
    migrated_database: URL,
) -> None:
    _alembic(PREDECESSOR)
    device_id = uuid4()
    with psycopg.connect(_render(migrated_database)) as connection:
        connection.execute(
            "INSERT INTO network_devices "
            "(id, name, role, status, ping_enabled, snmp_enabled, "
            "send_notifications, notification_delay_minutes, is_active, "
            "current_subscriber_count, health_status, created_at, updated_at) "
            "VALUES (%s, 'Legacy core', 'edge', 'offline', true, false, true, "
            "0, true, 0, 'unknown', now(), now())",
            (device_id,),
        )
        connection.commit()

    _alembic(CANDIDATE)
    _assert_candidate_contract(migrated_database)
    with psycopg.connect(_render(migrated_database)) as connection:
        row = connection.execute(
            "SELECT name, archived_at FROM network_devices WHERE id = %s",
            (device_id,),
        ).fetchone()
        assert row == ("Legacy core", None)
        connection.execute(
            "UPDATE network_devices SET is_active = false, archived_at = now(), "
            "archived_by = 'migration-test', archive_reason = 'Retired in test' "
            "WHERE id = %s",
            (device_id,),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="Restore all archived core devices"):
        _alembic(PREDECESSOR, downgrade=True)

    with psycopg.connect(_render(migrated_database)) as connection:
        connection.execute(
            "UPDATE network_devices SET archived_at = NULL, archived_by = NULL, "
            "archive_reason = NULL WHERE id = %s",
            (device_id,),
        )
        connection.commit()
    _alembic(PREDECESSOR, downgrade=True)
    columns, constraints, projection_constraints = _archive_contract(migrated_database)
    assert {"archived_at", "archived_by", "archive_reason"}.isdisjoint(columns)
    assert "ck_network_device_archive_state" not in constraints
    assert not any("archived" in value for value in projection_constraints)
