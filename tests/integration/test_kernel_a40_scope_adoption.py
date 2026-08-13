"""Rehearse kernel 0021 against Sub's deployed settings invariant.

This is deliberately not Alembic lineage composition.  Sub migration 514 owns
the existing constraint and Sub's ``alembic_version`` remains untouched.  The
canary executes the released kernel migration body inside a rollback-only
transaction to prove that a40 recognises Sub's stronger predecessor, marks it
as adopted, and preserves it on downgrade.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from alembic.migration import MigrationContext
from alembic.operations import Operations
from dotmac_kernel.migrations import versions_dir
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

KERNEL_SCOPE_REVISION = "0021_setting_scope_alignment"
KERNEL_SCOPE_PREDECESSOR = "0020_delivery_receipts"
SCOPE_CONSTRAINT = "ck_domain_settings_scope_alignment"
ADOPTION_MARKER = "dotmac-kernel:0021:adopted-existing"


def _load_scope_migration() -> ModuleType:
    matches = tuple(versions_dir().glob("*_0021_setting_scope_alignment.py"))
    assert len(matches) == 1, (
        "the reviewed a40 wheel must ship exactly one kernel 0021 migration; "
        f"found {[path.name for path in matches]}"
    )
    path: Path = matches[0]
    spec = importlib.util.spec_from_file_location("kernel_scope_alignment", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == KERNEL_SCOPE_REVISION
    assert module.down_revision == KERNEL_SCOPE_PREDECESSOR
    return module


def _versions(connection: Connection) -> tuple[str, ...]:
    return tuple(
        connection.execute(
            text("SELECT version_num FROM alembic_version ORDER BY version_num")
        ).scalars()
    )


def _scope_contract(connection: Connection) -> tuple[str, str | None, str]:
    row = connection.execute(
        text(
            "SELECT pg_get_constraintdef(c.oid), "
            "obj_description(c.oid, 'pg_constraint'), cols.column_default "
            "FROM pg_constraint c "
            "JOIN pg_class t ON t.oid = c.conrelid "
            "JOIN pg_namespace n ON n.oid = t.relnamespace "
            "JOIN information_schema.columns cols "
            "ON cols.table_schema = n.nspname "
            "AND cols.table_name = t.relname "
            "AND cols.column_name = 'scope_kind' "
            "WHERE n.nspname = current_schema() "
            "AND t.relname = 'domain_settings' AND c.conname = :name"
        ),
        {"name": SCOPE_CONSTRAINT},
    ).one()
    definition, comment, default = row
    return str(definition), str(comment) if comment is not None else None, str(default)


def _run(module: ModuleType, operation: str, connection: Connection) -> None:
    migration_context = MigrationContext.configure(connection)
    with Operations.context(migration_context):
        getattr(module, operation)()


def test_kernel_0021_adopts_and_preserves_subs_514_invariant(engine: Engine) -> None:
    """a40 may adopt Sub's constraint; it may not weaken or take it away."""

    assert engine.dialect.name == "postgresql"
    module = _load_scope_migration()

    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            versions_before = _versions(connection)
            contract_before = _scope_contract(connection)
            definition_before, comment_before, default_before = contract_before
            assert comment_before is None
            assert "platform" in default_before

            _run(module, "upgrade", connection)

            definition_adopted, comment_adopted, default_adopted = _scope_contract(
                connection
            )
            assert definition_adopted == definition_before
            assert comment_adopted == ADOPTION_MARKER
            assert default_adopted == default_before
            assert _versions(connection) == versions_before

            _run(module, "downgrade", connection)

            assert _scope_contract(connection) == contract_before
            assert _versions(connection) == versions_before
        finally:
            transaction.rollback()
