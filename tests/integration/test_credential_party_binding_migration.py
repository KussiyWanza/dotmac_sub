"""Fresh and deployed-524 PostgreSQL proofs for migration 525."""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from alembic.config import Config
from psycopg import sql
from psycopg.errors import CheckViolation
from sqlalchemy import create_engine
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.orm import Session

from alembic import command
from app import config as app_config
from app.models.auth import AuthProvider, UserCredential
from app.models.party import Party, PartyRole, PartyRoleStatus, PartyRoleType, PartyType
from app.models.subscriber import UserType
from app.models.system_user import SystemUser

ROOT = Path(__file__).resolve().parents[2]
PREDECESSOR = "524_audit_events_kernel_r1"
CANDIDATE = "525_credential_party_binding_additive"


def _render(url: URL) -> str:
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


@pytest.fixture
def engine() -> Iterator[Engine]:
    configured = os.getenv("TEST_DATABASE_URL")
    if not configured or not make_url(configured).drivername.startswith("postgresql"):
        raise pytest.UsageError("migration-path test requires PostgreSQL")
    value = create_engine(configured)
    try:
        yield value
    finally:
        value.dispose()


@pytest.fixture
def migrated_database(monkeypatch: pytest.MonkeyPatch) -> Iterator[URL]:
    configured = os.getenv("TEST_DATABASE_URL")
    if not configured:
        raise pytest.UsageError("migration-path test requires TEST_DATABASE_URL")
    base = make_url(configured)
    if not base.drivername.startswith("postgresql"):
        raise pytest.UsageError("migration-path test requires PostgreSQL")
    name = f"dotmac_credential_party_{uuid4().hex}"
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


def _upgrade(revision: str) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(config, revision)


def _downgrade(revision: str) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    command.downgrade(config, revision)


def _candidate_contract(url: URL) -> tuple[set[str], set[str], set[str]]:
    with psycopg.connect(_render(url)) as connection:
        columns = {
            row[0]
            for row in connection.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'user_credentials'"
            )
        }
        constraints = {
            row[0]
            for row in connection.execute(
                "SELECT conname FROM pg_constraint WHERE conrelid = "
                "'user_credentials'::regclass"
            )
        }
        bindings = {
            row[0]
            for row in connection.execute(
                "SELECT binding_key FROM authentication_bindings"
            )
        }
    return columns, constraints, bindings


def _identity_trigger_exists(url: URL) -> bool:
    with psycopg.connect(_render(url)) as connection:
        return bool(
            connection.execute(
                "SELECT 1 FROM pg_trigger WHERE tgname = "
                "'trg_authentication_binding_identity_immutable' "
                "AND NOT tgisinternal"
            ).fetchone()
        )


def test_fresh_head_has_complete_projection_contract(engine, migrated_database) -> None:
    _upgrade("head")

    columns, constraints, bindings = _candidate_contract(migrated_database)

    assert {
        "party_id",
        "authentication_binding_id",
        "tenant_id",
        "party_bound_at",
        "party_binding_source",
        "party_binding_reason",
    } <= columns
    assert {
        "ck_user_credentials_party_binding_projection",
        "uq_user_credentials_tenant_party_auth_binding",
        "fk_user_credentials_tenant",
    } <= constraints
    assert bindings == {"local.default", "radius.default"}
    assert _identity_trigger_exists(migrated_database)
    with psycopg.connect(_render(migrated_database)) as connection:
        with pytest.raises(CheckViolation):
            connection.execute(
                "UPDATE authentication_bindings SET mechanism_code = 'changed' "
                "WHERE binding_key = 'local.default'"
            )

    _downgrade(PREDECESSOR)

    with psycopg.connect(_render(migrated_database)) as connection:
        remaining_columns = {
            row[0]
            for row in connection.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'user_credentials'"
            )
        }
        bindings_table = connection.execute(
            "SELECT to_regclass('public.authentication_bindings')"
        ).fetchone()
    assert {
        "party_id",
        "authentication_binding_id",
        "tenant_id",
        "party_bound_at",
        "party_binding_source",
        "party_binding_reason",
    }.isdisjoint(remaining_columns)
    assert bindings_table == (None,)


def _seed_legacy_canaries(url: URL) -> tuple[str, str, tuple[object, ...]]:
    sqlalchemy_engine = create_engine(url)
    try:
        with Session(sqlalchemy_engine) as session:
            party = Party(
                party_type=PartyType.person.value,
                display_name="Migration canary",
                status="active",
                data_classification="test",
            )
            staff = SystemUser(
                person_party=party,
                party_bound_at=datetime(2026, 8, 12, tzinfo=UTC),
                party_binding_source="migration_canary",
                party_binding_reason="migration preservation proof",
                first_name="Migration",
                last_name="Canary",
                email=f"migration-{uuid4().hex}@example.test",
                user_type=UserType.system_user,
                is_active=True,
            )
            role = PartyRole(
                party=party,
                role_type=PartyRoleType.staff.value,
                role_key="default",
                status=PartyRoleStatus.active.value,
                source="migration_canary",
            )
            credential = UserCredential(
                system_user=staff,
                provider=AuthProvider.local,
                username=f"migration-{uuid4().hex}",
                password_hash="not-a-real-hash",
                is_active=True,
            )
            session.add_all((party, staff, role, credential))
            session.commit()
            role_snapshot = (
                role.id,
                role.party_id,
                role.role_type,
                role.role_key,
                role.status,
                role.source,
            )
            return str(credential.id), str(staff.id), role_snapshot
    finally:
        sqlalchemy_engine.dispose()


def _restore_deployed_524_shape(url: URL) -> None:
    with psycopg.connect(_render(url), autocommit=True) as connection:
        for column in (
            "tenant_id",
            "authentication_binding_id",
            "party_binding_reason",
            "party_binding_source",
            "party_bound_at",
            "party_id",
        ):
            connection.execute(
                sql.SQL(
                    "ALTER TABLE user_credentials DROP COLUMN IF EXISTS {} CASCADE"
                ).format(sql.Identifier(column))
            )
        connection.execute("DROP TABLE IF EXISTS authentication_bindings CASCADE")


def test_deployed_524_upgrade_preserves_credentials_and_party_roles(
    engine, migrated_database
) -> None:
    _upgrade(PREDECESSOR)
    credential_id, staff_id, role_before = _seed_legacy_canaries(migrated_database)
    _restore_deployed_524_shape(migrated_database)

    _upgrade(CANDIDATE)

    with psycopg.connect(_render(migrated_database)) as connection:
        credential = connection.execute(
            "SELECT id::text, system_user_id::text, provider::text, party_id, "
            "authentication_binding_id, tenant_id FROM user_credentials "
            "WHERE id = %s",
            (credential_id,),
        ).fetchone()
        role_after = connection.execute(
            "SELECT id, party_id, role_type, role_key, status, source "
            "FROM party_roles WHERE id = %s",
            (role_before[0],),
        ).fetchone()

    assert credential == (credential_id, staff_id, "local", None, None, None)
    assert role_after == role_before
    assert _candidate_contract(migrated_database)[2] == {
        "local.default",
        "radius.default",
    }
