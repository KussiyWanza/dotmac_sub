"""Migrated-PostgreSQL canaries for `sla_policy_versions` (OUTAGE_SLA_SPINE §4).

Per the approved standard, a database-backed test claiming deployed behaviour
must run against PostgreSQL with a schema built by the **real Alembic chain** —
`Base.metadata.create_all()` is not deployed-schema evidence. These constraints
are exactly the kind that metadata cannot express and a model-built schema
silently omits:

- `ex_sla_policy_versions_no_overlap` — a GiST exclusion constraint. It exists
  only in migration 467 and is the reason "the policy in force at instant T"
  has a single answer. An application-level check cannot replace it: two
  concurrent writers would each read before writing and both pass.
- `ck_sla_policy_versions_scope_matches_source` — binds the precedence claim
  to the scope column.
- `ck_sla_policy_versions_contractual_target` — a contractual source may not
  omit its availability target, because the design forbids inventing one.

Both migration proofs the standard asks for are covered:

- **fresh acceptance** — baseline → head builds the table with its constraints;
- **incremental acceptance** — the real predecessor (466) → head proves an
  existing production database gains them. This matters because revision 001
  builds from the current `Base.metadata`, so a fresh upgrade would create the
  table even if migration 467 did nothing; the incremental path is what
  actually exercises the new DDL.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from alembic.config import Config
from psycopg import errors as pg_errors
from psycopg import sql
from sqlalchemy.engine import URL, make_url

from alembic import command
from app import config as app_config

ROOT = Path(__file__).resolve().parents[2]
PREDECESSOR = "466_team_inbox_channel_ai_routes"
CANDIDATE = "467_sla_policy_versions"

NOW = datetime(2026, 8, 3, 12, 0, 0, tzinfo=UTC)


def _render(url: URL) -> str:
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


@pytest.fixture
def engine():
    """Satisfy the package PostgreSQL guard without building current schema.

    The package fixture calls `create_all()`, which would bypass the migration
    path under test — the whole point of this module.
    """
    configured = os.getenv("TEST_DATABASE_URL")
    if not configured:
        pytest.skip("migrated-schema test requires TEST_DATABASE_URL")
    url = make_url(configured)
    if not url.drivername.startswith("postgresql"):
        pytest.skip("migrated-schema test requires PostgreSQL")

    class _Dialect:
        name = "postgresql"

    class _Stub:
        dialect = _Dialect()

    return _Stub()


@pytest.fixture
def migrated_database(monkeypatch: pytest.MonkeyPatch) -> Iterator[URL]:
    """A disposable database built by the real Alembic chain.

    `alembic/env.py` resolves its target from `app_config.settings`, NOT from
    the Config's `sqlalchemy.url`, so pointing the Config at the scratch
    database silently does nothing and the upgrade runs against whatever
    `DATABASE_URL` the job exports (sqlite, in CI). Patch settings instead —
    the same seam `test_migrations_423_to_head.py` uses.
    """
    configured = os.getenv("TEST_DATABASE_URL")
    if not configured:
        pytest.skip("migrated-schema test requires TEST_DATABASE_URL")
    base = make_url(configured)
    if not base.drivername.startswith("postgresql"):
        pytest.skip("migrated-schema test requires PostgreSQL")

    name = f"dotmac_sla_policy_{uuid.uuid4().hex}"
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


def _alembic(url: URL, revision: str) -> None:
    """Upgrade the scratch database. The target comes from the patched
    `app_config.settings` (see `migrated_database`), which is what
    `alembic/env.py` actually reads."""

    del url  # documented: env.py resolves the URL from settings, not Config
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(config, revision)


def _constraints(url: URL) -> dict[str, str]:
    with psycopg.connect(_render(url)) as conn:
        rows = conn.execute(
            """
            SELECT conname, contype
            FROM pg_constraint
            WHERE conrelid = 'sla_policy_versions'::regclass
            """
        ).fetchall()
    return {name: kind for name, kind in rows}


def _insert(conn, **overrides) -> None:
    payload = {
        "id": uuid.uuid4(),
        "policy_key": "internal_measurement:global",
        "version": 1,
        "source": "internal_measurement",
        "subscription_id": None,
        "subscriber_id": None,
        "offer_id": None,
        "effective_from": NOW,
        "effective_to": None,
        "availability_target_percent": 99.5,
        "calendar_timezone": "Africa/Lagos",
        "maintenance_excludable": True,
        "command_fingerprint": None,
        "command_idempotency_key": None,
        "created_at": NOW,
    }
    payload.update(overrides)
    conn.execute(
        """
        INSERT INTO sla_policy_versions
          (id, policy_key, version, source, subscription_id, subscriber_id,
           offer_id, effective_from, effective_to, availability_target_percent,
           calendar_timezone, maintenance_excludable, command_fingerprint,
           command_idempotency_key, created_at)
        VALUES
          (%(id)s, %(policy_key)s, %(version)s, %(source)s, %(subscription_id)s,
           %(subscriber_id)s, %(offer_id)s, %(effective_from)s, %(effective_to)s,
           %(availability_target_percent)s, %(calendar_timezone)s,
           %(maintenance_excludable)s, %(command_fingerprint)s,
           %(command_idempotency_key)s, %(created_at)s)
        """,
        payload,
    )


# --- fresh acceptance -------------------------------------------------------


def test_head_builds_the_table_with_its_migration_only_constraints(
    engine, migrated_database
):
    _alembic(migrated_database, "head")

    found = _constraints(migrated_database)
    assert "ex_sla_policy_versions_no_overlap" in found
    assert found["ex_sla_policy_versions_no_overlap"] == "x", "must be EXCLUDE"
    for check in (
        "ck_sla_policy_versions_scope_matches_source",
        "ck_sla_policy_versions_contractual_target",
        "ck_sla_policy_versions_range",
        "ck_sla_policy_versions_target_bounds",
    ):
        assert check in found, f"{check} missing from the migrated schema"


# --- incremental acceptance -------------------------------------------------


def test_existing_production_database_gains_the_constraints(engine, migrated_database):
    """The proof that matters: predecessor → candidate, not baseline → head.

    Revision 001 builds from current `Base.metadata`, so a fresh upgrade would
    create this table even if 467 were a no-op. Stopping at 466 and stepping
    forward is what actually exercises the new DDL.
    """
    _alembic(migrated_database, PREDECESSOR)
    with psycopg.connect(_render(migrated_database)) as conn:
        exists = conn.execute(
            "SELECT to_regclass('public.sla_policy_versions')"
        ).fetchone()[0]
    # Revision 001's metadata bootstrap may or may not have created it; either
    # way the constraint the candidate owns must be absent beforehand.
    before = _constraints(migrated_database) if exists else {}
    assert "ex_sla_policy_versions_no_overlap" not in before

    _alembic(migrated_database, CANDIDATE)

    after = _constraints(migrated_database)
    assert "ex_sla_policy_versions_no_overlap" in after


# --- the constraints actually bite ------------------------------------------


def test_overlapping_versions_of_one_policy_are_rejected(engine, migrated_database):
    """Two versions covering the same instant would make "the policy in force
    at T" ambiguous. Only the database can enforce this against concurrency."""
    _alembic(migrated_database, "head")

    with psycopg.connect(_render(migrated_database), autocommit=True) as conn:
        _insert(
            conn, version=1, effective_from=NOW, effective_to=NOW + timedelta(days=30)
        )
        with pytest.raises(pg_errors.ExclusionViolation) as caught:
            _insert(
                conn,
                version=2,
                effective_from=NOW + timedelta(days=10),
                effective_to=NOW + timedelta(days=40),
            )
    assert caught.value.diag.constraint_name == "ex_sla_policy_versions_no_overlap"


def test_abutting_versions_of_one_policy_are_allowed(engine, migrated_database):
    """Half-open ranges must let one version end exactly where the next
    begins — otherwise a lawful policy change could not be recorded."""
    _alembic(migrated_database, "head")
    boundary = NOW + timedelta(days=30)

    with psycopg.connect(_render(migrated_database), autocommit=True) as conn:
        _insert(conn, version=1, effective_from=NOW, effective_to=boundary)
        _insert(conn, version=2, effective_from=boundary, effective_to=None)

        count = conn.execute(
            "SELECT count(*) FROM sla_policy_versions WHERE policy_key = %s",
            ("internal_measurement:global",),
        ).fetchone()[0]
    assert count == 2


def test_a_precedence_claim_requires_its_scope(engine, migrated_database):
    _alembic(migrated_database, "head")

    with psycopg.connect(_render(migrated_database), autocommit=True) as conn:
        # subscription_contract with no subscription_id. The key is set to the
        # value COALESCE would derive, so the derived-key rule is satisfied and
        # ONLY the scope rule can fire — otherwise Postgres could report either.
        with pytest.raises(pg_errors.CheckViolation) as caught:
            _insert(
                conn,
                source="subscription_contract",
                subscription_id=None,
                policy_key="subscription_contract:global",
            )
    assert (
        caught.value.diag.constraint_name
        == "ck_sla_policy_versions_scope_matches_source"
    )


def test_a_contractual_policy_may_not_omit_its_target(engine, migrated_database):
    """The design forbids inventing a target, so the schema forbids a
    contractual row without one — while still allowing the internal
    measurement policy to stay silent about what was promised."""
    _alembic(migrated_database, "head")
    subscription_id = uuid.uuid4()

    with psycopg.connect(_render(migrated_database), autocommit=True) as conn:
        with pytest.raises(pg_errors.CheckViolation) as caught:
            _insert(
                conn,
                source="account_contract",
                subscription_id=None,
                subscriber_id=subscription_id,
                availability_target_percent=None,
                policy_key=f"account_contract:{subscription_id}",
            )
    # CHECK constraints are evaluated before the FK trigger fires, so this is
    # the target rule biting and not the unrelated subscriber FK.
    assert (
        caught.value.diag.constraint_name == "ck_sla_policy_versions_contractual_target"
    )

    # The other direction: internal_measurement legitimately has no target,
    # and the constraint must not block it.
    with psycopg.connect(_render(migrated_database), autocommit=True) as conn:
        # The key must be the derived one — `ck_sla_policy_versions_key_is_derived`
        # rejects an invented name, which is the point of that constraint.
        _insert(
            conn,
            source="internal_measurement",
            subscription_id=None,
            availability_target_percent=None,
        )
        stored = conn.execute(
            "SELECT availability_target_percent FROM sla_policy_versions "
            "WHERE policy_key = %s",
            ("internal_measurement:global",),
        ).fetchone()[0]
    assert stored is None


# --- scope-bound identity and retention safety (review blockers 1 and 2) -----


def test_two_series_cannot_cover_one_scope_for_the_same_period(
    engine, migrated_database
):
    """Keying the exclusion on policy_key alone would let two different keys
    target one subscription for the same period, producing two
    equal-precedence policies and an undefined resolver winner."""
    _alembic(migrated_database, "head")

    with psycopg.connect(_render(migrated_database), autocommit=True) as conn:
        _insert(
            conn,
            policy_key="internal_measurement:global",
            version=1,
            effective_to=NOW + timedelta(days=30),
        )
        # A second series name over the SAME scope and period must still be
        # rejected — the exclusion keys on (source, scope, range).
        # Same scope, same period, a different series name. Either the
        # derived-key rule rejects the name or the scope-keyed exclusion
        # rejects the overlap — both close the gap the review identified.
        with pytest.raises(
            (pg_errors.ExclusionViolation, pg_errors.CheckViolation)
        ) as caught:
            _insert(
                conn,
                policy_key="internal_measurement:other",
                version=2,
                effective_from=NOW + timedelta(days=10),
                effective_to=NOW + timedelta(days=40),
            )
    assert caught.value.diag.constraint_name in {
        "ex_sla_policy_versions_no_overlap",
        "ck_sla_policy_versions_key_is_derived",
    }


def test_policy_key_must_match_the_derived_scope_identity(engine, migrated_database):
    _alembic(migrated_database, "head")

    with psycopg.connect(_render(migrated_database), autocommit=True) as conn:
        with pytest.raises(pg_errors.CheckViolation) as caught:
            _insert(conn, policy_key="something:invented")
    assert caught.value.diag.constraint_name == "ck_sla_policy_versions_key_is_derived"


def test_contractual_history_outlives_its_parents(engine, migrated_database):
    """CASCADE would erase the record of what a customer was owed. The FKs
    must RESTRICT so a parent delete fails loudly instead."""
    _alembic(migrated_database, "head")

    with psycopg.connect(_render(migrated_database)) as conn:
        rows = conn.execute(
            """
            SELECT a.attname, c.confdeltype
            FROM pg_constraint c
            JOIN pg_attribute a
              ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
            WHERE c.conrelid = 'sla_policy_versions'::regclass
              AND c.contype = 'f'
            """
        ).fetchall()
    behaviour = {name: delete_rule for name, delete_rule in rows}
    for column in ("subscription_id", "subscriber_id", "offer_id", "supersedes_id"):
        assert behaviour.get(column) == "r", (
            f"{column} must RESTRICT (got {behaviour.get(column)!r}); "
            "cascading would delete contractual history"
        )


def test_a_replayed_command_cannot_append_a_second_row(engine, migrated_database):
    """The fingerprint uniqueness is the durable backstop for replay, holding
    even when two processes retry concurrently."""
    _alembic(migrated_database, "head")

    with psycopg.connect(_render(migrated_database), autocommit=True) as conn:
        # Close the first range so the ranges abut rather than overlap — the
        # exclusion constraint must not fire first and mask the fingerprint.
        _insert(
            conn,
            version=1,
            effective_to=NOW + timedelta(days=1),
            command_fingerprint="sha256:same",
        )
        with pytest.raises(pg_errors.UniqueViolation) as caught:
            _insert(
                conn,
                version=2,
                effective_from=NOW + timedelta(days=1),
                command_fingerprint="sha256:same",
            )
    assert caught.value.diag.constraint_name == "uq_sla_policy_versions_fingerprint"


def test_concurrent_idempotency_key_reuse_is_arbitrated_by_the_database(
    engine, migrated_database
):
    """The read-side check cannot serialise two processes on its own, so the
    key carries a unique constraint as the real arbiter."""
    _alembic(migrated_database, "head")

    with psycopg.connect(_render(migrated_database), autocommit=True) as conn:
        _insert(
            conn,
            version=1,
            effective_to=NOW + timedelta(days=1),
            command_fingerprint="sha256:a",
            command_idempotency_key="key-1",
        )
        with pytest.raises(pg_errors.UniqueViolation) as caught:
            _insert(
                conn,
                version=2,
                effective_from=NOW + timedelta(days=1),
                command_fingerprint="sha256:b",
                command_idempotency_key="key-1",
            )
    assert caught.value.diag.constraint_name == "uq_sla_policy_versions_idempotency_key"
