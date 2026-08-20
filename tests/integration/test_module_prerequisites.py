"""Sub supplies the two effects every installable module requires.

ADR-0011 rules that Sub composes module lineages beside its own chain and
answers their declared prerequisites with its OWN revisions, rather than by
running kernel `0001` — the position the kernel documents for ERP, which hosts
`public.tenants` in its own lineage and structurally cannot run `0001` either.

This file is the proof, and it is deliberately not a hand-written re-statement
of the contract. It calls the kernel's own verifier against the real migrated
database, so what is asserted here is exactly what a module migration will
assert at `alembic upgrade` — not a second, drifting copy of the same rules.
The ADR's phrase for this is "binding is not belief".

Two effects:

- ``tenant_scope_catalog.v1`` — `public.tenants` and `public.tenant_domains`
  with the kernel column/key/index contract, plus `app_current_tenant_id()`
  reading the `app.current_tenant` GUC as uuid and returning NULL when unset or
  malformed. Supplied by 508/509 (the tables) and 545 (the function and the four
  timestamp defaults 508 never set).
- ``module_database_roles.v1`` — `app_admin` (BYPASSRLS, not superuser),
  `app_user` and `platform_api` (neither). Supplied by 546.

PostgreSQL only. Roles, functions and column defaults are exactly the things the
SQLite lane cannot represent, which is why this is not a unit test.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from dotmac_kernel.migrations.verify import require_prerequisites
from dotmac_kernel.prerequisites import (
    MODULE_DATABASE_ROLES_V1,
    TENANT_SCOPE_CATALOG_V1,
    PrerequisiteError,
)
from sqlalchemy.engine import Engine

REQUIRED = (TENANT_SCOPE_CATALOG_V1.name, MODULE_DATABASE_ROLES_V1.name)


@pytest.fixture
def rollback_connection(engine: Engine):
    """A connection whose DDL is discarded, so a test may break the schema."""
    connection = engine.connect()
    transaction = connection.begin()
    try:
        yield connection
    finally:
        transaction.rollback()
        connection.close()


def test_sub_supplies_every_effect_a_network_module_requires(engine: Engine) -> None:
    """The load-bearing assertion, in the exact form a module migration makes."""
    with engine.connect() as connection:
        require_prerequisites(connection, REQUIRED)


@pytest.mark.parametrize("name", REQUIRED)
def test_each_effect_is_verifiable_rather_than_merely_declared(
    engine: Engine, name: str
) -> None:
    """A prerequisite with no verifier raises rather than passing quietly.

    Worth asserting per-effect: `require_prerequisites` refuses a registered but
    unverifiable name, so this also proves neither of ours is in that state.
    """
    with engine.connect() as connection:
        require_prerequisites(connection, (name,))


def test_the_tenant_function_answers_the_guc(engine: Engine) -> None:
    """Behaviour, not just definition: the three cases RLS depends on.

    The verifier reads the function's TEXT for its semantic markers. That
    catches a rewrite, but it cannot catch a Postgres-version difference in how
    the body behaves, and every composed module's RLS policy is this function's
    return value.
    """
    with engine.connect() as connection:
        unset = connection.execute(sa.text("SELECT app_current_tenant_id()")).scalar()
        assert unset is None, "unset GUC must resolve to NULL, not raise"

        connection.execute(
            sa.text("SELECT set_config('app.current_tenant', :value, true)"),
            {"value": "not-a-uuid"},
        )
        malformed = connection.execute(
            sa.text("SELECT app_current_tenant_id()")
        ).scalar()
        assert malformed is None, (
            "a malformed GUC must resolve to NULL — raising here would make "
            "every RLS policy in every composed module error instead of "
            "denying"
        )

        connection.execute(
            sa.text("SELECT set_config('app.current_tenant', :value, true)"),
            {"value": "11111111-1111-1111-1111-111111111111"},
        )
        resolved = connection.execute(
            sa.text("SELECT app_current_tenant_id()")
        ).scalar()
        assert str(resolved) == "11111111-1111-1111-1111-111111111111"


def test_the_verifier_bites_when_the_function_is_missing(rollback_connection) -> None:
    """Sensitivity proof.

    Without this, `test_sub_supplies_every_effect…` could pass because the
    verifier is inert rather than because Sub satisfies the contract. Dropping
    the function inside a rolled-back transaction is the cheapest way to prove
    it actually inspects this database.
    """
    rollback_connection.execute(sa.text("DROP FUNCTION app_current_tenant_id();"))

    with pytest.raises(PrerequisiteError):
        require_prerequisites(rollback_connection, (TENANT_SCOPE_CATALOG_V1.name,))


def test_the_verifier_bites_on_a_wrong_role_posture(rollback_connection) -> None:
    """The other half, and the one that matters most.

    A superuser bypasses RLS whether or not `rolbypassrls` is set, so an online
    role that acquired SUPERUSER would defeat tenant isolation for every
    composed module while still looking correct to a naive check. Proving the
    verifier catches it is proving the isolation claim has something behind it.
    """
    rollback_connection.execute(sa.text("ALTER ROLE app_user BYPASSRLS;"))

    with pytest.raises(PrerequisiteError):
        require_prerequisites(rollback_connection, (MODULE_DATABASE_ROLES_V1.name,))


def test_the_timestamp_defaults_exist_on_both_catalogue_tables(
    engine: Engine,
) -> None:
    """508 created these columns NOT NULL with no DEFAULT; 545 added the default.

    Called out separately because "the table exists" was mistaken for "the
    prerequisite is satisfied" — the column contract is the half that was
    actually missing, and a writer that is not Sub's ORM would have hit a NOT
    NULL violation.
    """
    inspector = sa.inspect(engine)
    for table in ("tenants", "tenant_domains"):
        columns = {c["name"]: c for c in inspector.get_columns(table)}
        for column in ("created_at", "updated_at"):
            assert columns[column]["default"] is not None, (
                f"{table}.{column} has no server default; "
                "tenant_scope_catalog.v1 requires one"
            )
