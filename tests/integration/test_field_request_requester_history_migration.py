"""PostgreSQL proof for the field-request requester-history repair."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

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
PREDECESSOR = "583_staff_expense_requesters"
CANDIDATE = "587_field_request_requester_history"


def _render(url: URL) -> str:
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def _upgrade(revision: str) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(config, revision)


@pytest.fixture
def fresh_migration_database(
    template_base_url: URL,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[URL]:
    """Create an empty database so this test replays the real migration chain."""

    name = f"dotmac_test_requester_history_{uuid4().hex}"
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


def _seed_legacy_requesters(url: URL) -> tuple[UUID, UUID, tuple[UUID, ...]]:
    system_user_id = uuid4()
    person_party_id = uuid4()
    technician_id = uuid4()
    work_order_id = uuid4()
    technician_owned_material_id = uuid4()
    legacy_person_material_id = uuid4()
    technician_owned_expense_id = uuid4()
    legacy_person_expense_id = uuid4()
    now = datetime.now(UTC)

    with psycopg.connect(_render(url), autocommit=True) as connection:
        connection.execute("SET session_replication_role = replica")
        try:
            connection.execute(
                """
                INSERT INTO parties (
                    id, party_type, display_name, status, data_classification,
                    created_at, updated_at
                ) VALUES (%s, 'person', 'History Owner', 'active', 'test', %s, %s)
                """,
                (person_party_id, now, now),
            )
            connection.execute(
                """
                INSERT INTO system_users (
                    id, person_party_id, party_bound_at, party_binding_source,
                    party_binding_reason, first_name, last_name, email, user_type,
                    is_active, device_login_enabled, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, 'History', 'Owner', %s,
                          'system_user', TRUE, FALSE, %s, %s)
                """,
                (
                    system_user_id,
                    person_party_id,
                    now,
                    "request-history-migration-test",
                    "Prove exact Party requester recovery",
                    f"history-{system_user_id}@example.test",
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO technician_profiles (
                    id, person_id, system_user_id, is_active, created_at, updated_at
                ) VALUES (%s, %s, %s, TRUE, %s, %s)
                """,
                (technician_id, uuid4(), system_user_id, now, now),
            )
            connection.execute(
                """
                INSERT INTO work_order (
                    id, public_id, subscriber_id, title, status,
                    requires_as_built_evidence, is_active, created_at, updated_at
                ) VALUES (%s, %s, %s, 'Requester history repair', 'scheduled',
                          TRUE, TRUE, %s, %s)
                """,
                (
                    work_order_id,
                    f"sub-{work_order_id.hex}",
                    uuid4(),
                    now,
                    now,
                ),
            )
            for request_id, technician, person in (
                (technician_owned_material_id, technician_id, uuid4()),
                (legacy_person_material_id, None, person_party_id),
            ):
                connection.execute(
                    """
                    INSERT INTO field_material_requests (
                        id, work_order_mirror_id, requested_by_technician_id,
                        requested_by_person_id, requested_by_system_user_id,
                        status, priority, fulfillment_channel, is_active,
                        created_at, updated_at
                    ) VALUES (%s, %s, %s, %s, NULL, 'draft', 'medium',
                              'manual', TRUE, %s, %s)
                    """,
                    (request_id, work_order_id, technician, person, now, now),
                )
            for request_id, technician, person in (
                (technician_owned_expense_id, technician_id, uuid4()),
                (legacy_person_expense_id, None, system_user_id),
            ):
                connection.execute(
                    """
                    INSERT INTO field_expense_requests (
                        id, work_order_mirror_id, requested_by_technician_id,
                        requested_by_person_id, requested_by_system_user_id,
                        status, purpose, currency, is_active, created_at, updated_at
                    ) VALUES (%s, %s, %s, %s, NULL, 'draft',
                              'Requester history repair', 'NGN', TRUE, %s, %s)
                    """,
                    (request_id, work_order_id, technician, person, now, now),
                )
        finally:
            connection.execute("SET session_replication_role = origin")

    return (
        system_user_id,
        person_party_id,
        (
            technician_owned_material_id,
            legacy_person_material_id,
            technician_owned_expense_id,
            legacy_person_expense_id,
        ),
    )


def test_predecessor_to_candidate_repairs_exact_requester_links(
    fresh_migration_database: URL,
) -> None:
    database_url = fresh_migration_database
    _upgrade(PREDECESSOR)
    system_user_id, person_party_id, request_ids = _seed_legacy_requesters(database_url)

    _upgrade(CANDIDATE)

    with psycopg.connect(_render(database_url)) as connection:
        material_rows = connection.execute(
            """
            SELECT id, requested_by_system_user_id, requested_by_technician_id,
                   requested_by_person_id
            FROM field_material_requests
            WHERE id = ANY(%s)
            ORDER BY id
            """,
            (list(request_ids[:2]),),
        ).fetchall()
        expense_rows = connection.execute(
            """
            SELECT id, requested_by_system_user_id, requested_by_technician_id,
                   requested_by_person_id
            FROM field_expense_requests
            WHERE id = ANY(%s)
            ORDER BY id
            """,
            (list(request_ids[2:]),),
        ).fetchall()
        indexes = {
            name
            for (name,) in connection.execute(
                """
                SELECT indexname
                FROM pg_indexes
                WHERE tablename IN (
                    'field_material_requests', 'field_expense_requests'
                )
                """
            ).fetchall()
        }

    assert {row[1] for row in (*material_rows, *expense_rows)} == {system_user_id}
    assert all(row[2] is not None for row in (*material_rows, *expense_rows))
    repaired_person_ids = {row[0]: row[3] for row in (*material_rows, *expense_rows)}
    assert repaired_person_ids[request_ids[1]] == person_party_id
    assert repaired_person_ids[request_ids[3]] == person_party_id
    assert {
        "ix_field_material_requests_requested_by_person",
        "ix_field_material_requests_requested_by_system_user",
        "ix_field_expense_requests_requested_by_person",
        "ix_field_expense_requests_requested_by_system_user",
    } <= indexes

    # Rehearse the remaining release and composed-module migrations after
    # verifying the historical rows survived the requester repair.
    _upgrade("heads")
    engine = create_engine(database_url)
    try:
        require_migrated_schema(engine)
    finally:
        engine.dispose()
