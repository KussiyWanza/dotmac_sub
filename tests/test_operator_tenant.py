"""The operator tenant: exactly one, idempotent, and agreed with the migration.

ADR-0009. Sub provisions one tenant and that tenant is the ISP operator. The
invariants worth pinning are the ones that fail silently: a second tenant row,
a provisioning pass that reverts an operator's edit, and the migration's copy
of the tenant id drifting from the runtime's.
"""

from __future__ import annotations

import ast
import pathlib
from uuid import UUID

import pytest
from dotmac_kernel.models import Tenant

from app.services.operator_tenant import (
    OPERATOR_TENANT_ID,
    OPERATOR_TENANT_NAME,
    OPERATOR_TENANT_SLUG,
    OperatorTenantMissingError,
    operator_tenant,
    operator_tenant_id,
    provision_operator_tenant,
)

MIGRATION = (
    pathlib.Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "509_backfill_operator_tenant_scope.py"
)


def _migration_constant(name: str) -> str:
    """Read a literal from the migration without importing it.

    Importing would pull in `alembic.op`, which has no context outside a
    migration run.
    """

    tree = ast.parse(MIGRATION.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            assert isinstance(node.value, ast.Constant)
            return str(node.value.value)
    raise AssertionError(f"{name} is not assigned in {MIGRATION.name}")


def test_the_migration_and_the_runtime_agree_on_the_tenant() -> None:
    """The id is duplicated on purpose; this is what stops it drifting.

    A migration must not import application code that can change under it, so
    the literal is copied. A copy nobody checks is how the backfill ends up
    attributing settings to a tenant the runtime never looks up.
    """

    assert UUID(_migration_constant("OPERATOR_TENANT_ID")) == OPERATOR_TENANT_ID
    assert _migration_constant("OPERATOR_TENANT_SLUG") == OPERATOR_TENANT_SLUG
    assert _migration_constant("OPERATOR_TENANT_NAME") == OPERATOR_TENANT_NAME


def test_provisioning_creates_exactly_one_tenant(db_session) -> None:
    tenant = provision_operator_tenant(db_session)

    assert tenant.id == OPERATOR_TENANT_ID
    assert tenant.slug == OPERATOR_TENANT_SLUG
    assert tenant.is_active is True
    assert db_session.query(Tenant).count() == 1


def test_provisioning_is_idempotent_across_boots(db_session) -> None:
    """Startup runs this on every boot; the second must be a no-op."""

    first = provision_operator_tenant(db_session)
    second = provision_operator_tenant(db_session)

    assert first.id == second.id
    assert db_session.query(Tenant).count() == 1


def test_provisioning_never_reverts_an_operator_edit(db_session) -> None:
    """An existence check, not an upsert.

    A rename an operator made through the database must survive the next
    restart; an upsert would quietly put the seeded name back.
    """

    provision_operator_tenant(db_session)
    renamed = db_session.get(Tenant, OPERATOR_TENANT_ID)
    renamed.name = "Dotmac Abuja"
    db_session.commit()

    provision_operator_tenant(db_session)

    assert db_session.get(Tenant, OPERATOR_TENANT_ID).name == "Dotmac Abuja"


def test_reading_before_provisioning_fails_loudly(db_session) -> None:
    """Silence here would write rows attributed to nothing."""

    with pytest.raises(OperatorTenantMissingError):
        operator_tenant(db_session)


def test_the_id_accessor_needs_no_database(db_session) -> None:
    provision_operator_tenant(db_session)

    assert operator_tenant_id() == OPERATOR_TENANT_ID
    assert operator_tenant(db_session).id == operator_tenant_id()
