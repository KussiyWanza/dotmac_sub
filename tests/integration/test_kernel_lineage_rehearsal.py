"""Does the kernel's migration lineage actually apply to Sub's schema?

This is the ADR-0017 gate, executed rather than described. Everything the
starter's `docs/inventories/sub-lineage-dispositions.md` says about collisions
was derived by parsing migration files in two repositories, and that method
produced four confident wrong answers before it was right. A database answers
the question definitively, and answers a larger one besides: parsing can compare
table NAMES, but only Postgres can tell you a column type conflicts, a CHECK
rejects existing rows, or an RLS policy collides.

## What it does

1. Provisions an isolated database (same fixture shape as the other migration
   rehearsals in this directory).
2. Builds **Sub's real schema by running Sub's own alembic chain to head** —
   never `create_all`, per the standing rule that database-backed acceptance
   uses production-engine schemas built by the real migration chain.
3. Points alembic at the **installed kernel's** migration directory and attempts
   to run that lineage on top.
4. Records exactly which revision fails and why.

The kernel lineage is expected to fail today, and the point is to pin WHERE.
Each disposition that lands moves the failure later; when it stops failing, the
gate is closed and the kernel lineage runs in a product database. That is a
ratchet, in the ADR-0018 sense — `EXPECTED_FIRST_FAILURE` may only move forward,
and a run that gets further than expected fails this test so the expectation is
lowered deliberately rather than drifting.

## Why the failure is asserted rather than tolerated

A test that merely reported would go unread. Asserting the exact failure point
means a disposition landing anywhere in the chain is visible immediately, and an
accidental regression in an already-dispositioned table is caught by the same
assertion.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from alembic.config import Config
from alembic.runtime.environment import EnvironmentContext
from alembic.script import ScriptDirectory
from psycopg import sql
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url

from alembic import command
from app import config as app_config

#: The first kernel revision expected to fail against Sub's schema.
#:
#: `0001_initial_tenant_schema` creates `tenants`, `tenant_domains`, `roles` and
#: `audit_events`. Sub has all four. Measured against staging (head
#: `519_fiber_cost_items`, 595 live tables): `tenants` 8/8 and `tenant_domains`
#: 6/6 are byte-identical to the kernel's, but `roles` (4 of 6 shared) and
#: `audit_events` (4 of 15) are not — so this revision cannot apply as written.
#:
#: MOVE THIS FORWARD as dispositions land. Never backwards without a recorded
#: reason: an earlier failure means something regressed.
EXPECTED_FIRST_FAILURE = "0001_initial_tenant_schema"

#: Lineage-head collisions remeasured against kernel 0.1.0a42 and Sub's
#: party-identity integration batch on 2026-08-13. Kernel 0022 renamed its RBAC
#: grant from `party_roles` to `party_role_grants`, so the current head has nine
#: overlaps. The lineage still creates `party_roles` at 0003 before renaming it;
#: that transient chain hazard remains a required disposition even though it is
#: intentionally absent from this current-metadata intersection.
EXPECTED_COLLISIONS = {
    "audit_events",
    "communication_suppressions",
    "domain_setting_history",
    "domain_settings",
    "parties",
    "roles",
    "tenant_domains",
    "tenants",
    "user_credentials",
}


class KernelLineageFailure(RuntimeError):
    """A kernel migration failure annotated with its active revision."""

    def __init__(self, revision: str, cause: Exception) -> None:
        super().__init__(f"kernel revision {revision} failed: {cause}")
        self.revision = revision


def _render(url: URL) -> str:
    return url.render_as_string(hide_password=False)


def _psycopg_url(url: URL) -> str:
    return _render(url.set(drivername="postgresql"))


def _kernel_versions_dir() -> Path:
    """The INSTALLED kernel's lineage, not a copy of it.

    Resolved through the package so this rehearsal follows the pin in
    `pyproject.toml`. A vendored copy would let the test pass against a lineage
    the product does not actually install.
    """
    import dotmac_kernel

    directory = Path(dotmac_kernel.__file__).parent / "migrations" / "versions"
    if not directory.is_dir():
        raise pytest.UsageError(f"installed kernel has no lineage at {directory}")
    return directory


@pytest.fixture
def isolated_database(monkeypatch: pytest.MonkeyPatch) -> Iterator[URL]:
    configured = os.getenv("TEST_DATABASE_URL")
    if not configured:
        raise pytest.UsageError("kernel lineage rehearsal requires TEST_DATABASE_URL")
    base_url = make_url(configured)
    if not base_url.drivername.startswith("postgresql"):
        raise pytest.UsageError("kernel lineage rehearsal requires PostgreSQL")

    name = f"dotmac_kernel_rehearsal_{uuid4().hex}"
    maintenance = base_url.set(database="postgres")
    with psycopg.connect(_psycopg_url(maintenance), autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        database_url = base_url.set(database=name)
        monkeypatch.setattr(
            app_config,
            "settings",
            replace(app_config.settings, database_url=_render(database_url)),
        )
        yield database_url
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


def _sub_config(database_url: URL) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("script_location", "alembic")
    config.set_main_option("sqlalchemy.url", _render(database_url))
    return config


def _kernel_config(database_url: URL) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("script_location", "alembic")
    config.set_main_option("version_locations", str(_kernel_versions_dir()))
    config.set_main_option("sqlalchemy.url", _render(database_url))
    return config


def _run_kernel_lineage(database_url: URL) -> None:
    """Run the independent kernel lineage without consuming Sub's head row.

    Loading kernel revisions through Sub's ``env.py`` makes a kernel-only
    revision map try to resolve Sub's own current revision row and also installs
    Sub's idempotent schema wrappers. A distinct version table and a direct
    Alembic environment preserve both owners' histories while exercising the
    installed kernel migrations without changing their DDL semantics.
    """
    config = _kernel_config(database_url)
    script = ScriptDirectory.from_config(config)
    active_revision: str | None = None

    def upgrade(revision, _context):
        steps = script._upgrade_revs("heads", revision)
        for step in steps:
            migration = step.migration_fn
            migration_revision = step.revision.revision

            def tracked_migration(
                *args,
                _migration=migration,
                _revision=migration_revision,
                **kwargs,
            ):
                nonlocal active_revision
                active_revision = _revision
                return _migration(*args, **kwargs)

            step.migration_fn = tracked_migration
        return steps

    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            with EnvironmentContext(
                config,
                script,
                fn=upgrade,
                destination_rev="heads",
            ) as environment:
                environment.configure(
                    connection=connection,
                    version_table="dotmac_kernel_alembic_version",
                )
                with environment.begin_transaction():
                    try:
                        environment.run_migrations()
                    except Exception as failure:
                        if active_revision is None:
                            raise
                        raise KernelLineageFailure(
                            active_revision,
                            failure,
                        ) from failure
    finally:
        engine.dispose()


def _tables(database_url: URL) -> set[str]:
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            return set(
                connection.execute(
                    text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                ).scalars()
            )
    finally:
        engine.dispose()


def test_subs_own_chain_builds_the_schema_the_rehearsal_needs(
    isolated_database: URL,
) -> None:
    """Guard the premise: a rehearsal against an empty database proves nothing."""
    command.upgrade(_sub_config(isolated_database), "heads")
    tables = _tables(isolated_database)
    assert len(tables) > 500, (
        f"Sub's chain produced only {len(tables)} tables; the rehearsal below "
        "would be measuring an empty database rather than Sub's schema"
    )


def test_the_measured_collisions_are_the_ones_actually_present(
    isolated_database: URL,
) -> None:
    """The collision set, checked against a schema rather than a parser.

    This is the assertion the starter's inventory could not make: its numbers
    came from parsing migration files, which took four attempts to get right.
    """
    command.upgrade(_sub_config(isolated_database), "heads")
    sub_tables = _tables(isolated_database)

    import dotmac_kernel.audit  # noqa: F401
    import dotmac_kernel.consent_models  # noqa: F401
    import dotmac_kernel.settings_models  # noqa: F401
    from dotmac_kernel.models import Base

    kernel_tables = {
        table.name
        for table in Base.metadata.tables.values()
        if table.schema in (None, "public")
    }
    measured = kernel_tables & sub_tables
    assert measured == EXPECTED_COLLISIONS, (
        "kernel/Sub lineage-head collision inventory changed; classify every "
        "added or removed name before changing this reviewed set. The separate "
        "transient-chain disposition for `party_roles` still applies.\n"
        f"added: {sorted(measured - EXPECTED_COLLISIONS)}\n"
        f"removed: {sorted(EXPECTED_COLLISIONS - measured)}\n"
        "See dotmac_starter_mt/docs/inventories/sub-lineage-dispositions.md."
    )


def test_the_kernel_lineage_fails_exactly_where_expected(
    isolated_database: URL,
) -> None:
    """Run the kernel lineage on Sub's schema and pin where it stops.

    Failure here is the CURRENT expected state, not a defect in this test. The
    assertion is about WHERE it fails: each landed disposition should move that
    point forward, and this test is how that progress is measured.
    """
    command.upgrade(_sub_config(isolated_database), "heads")

    with pytest.raises(KernelLineageFailure) as captured:
        _run_kernel_lineage(isolated_database)

    failure = captured.value
    assert failure.revision == EXPECTED_FIRST_FAILURE, (
        "the kernel lineage failed, but not at the expected revision "
        f"({EXPECTED_FIRST_FAILURE}). This is progress or regression, not "
        "noise — read it and move EXPECTED_FIRST_FAILURE deliberately:\n"
        f"{failure}"
    )
