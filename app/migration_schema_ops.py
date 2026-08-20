"""The one owner of "does this schema object already exist?" during migrations.

``alembic/env.py`` wraps a subset of alembic's schema ops so the post-squash
chain tolerates state the squashed ``001`` migration already built: revision
``001`` builds the full current schema via ``Base.metadata.create_all``, while
later revisions were written against the pre-squash incremental schema and
unconditionally ``add_column`` / ``create_table`` things the squash already
produced. Without the wrappers a fresh squash-built database explodes with
``DuplicateColumn`` / ``DuplicateTable`` / ``UndefinedColumn`` partway through
the chain.

This module owns the wrappers and, more importantly, the four existence
predicates they ask. It lives in ``app/`` rather than beside ``env.py`` for the
same reason ``resolve_migration_lock_timeout`` does: ``alembic/env.py`` runs
migrations at import time, so nothing there can be unit-tested, and a guard
nobody can test is a guard nobody can prove.

**Every predicate is schema-qualified.** That is the whole point of this module
existing separately, and it is not cosmetic. The wrappers monkey-patch
module-level ``alembic.op`` functions, so they apply to EVERY revision in a
run — including a composed module lineage that writes fully-qualified DDL into
its own ``mod_*`` schema. A predicate that asks "is there a table called
``addresses``?" without saying *where* answers about ``public`` no matter what
schema the op targets. When the answer is yes, the wrapper returns ``None``,
the revision is stamped as applied, and the table it was supposed to create
does not exist. That is silent corruption, not a migration failure: the lineage
believes it is current until the first query dies on ``UndefinedTable``.

The failure is reachable rather than theoretical: the installable network
modules Sub is the named first consumer for carry six table names that already
exist in Sub's ``public`` schema, every one of them a coincidence rather than a
shared concept — ``mod_ipam.addresses`` is an IP address and Sub's
``addresses`` is a street address; ``mod_netaccess.sessions`` is a RADIUS
session and Sub's ``sessions`` is an auth session. Nobody reviewing either side
in isolation would see it.

Behaviour for UNQUALIFIED ops (every Sub revision today) is unchanged: passing
``schema=None`` inspects the default search-path schema, exactly as before.
The permissive ``except Exception`` fallbacks are preserved deliberately — each
returns the value that makes the wrapper DELEGATE to the real op rather than
skip it, so an inspector failure degrades to alembic's own behaviour instead of
silently doing nothing.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa

from alembic import op

__all__ = [
    "columns_of",
    "constraint_exists",
    "index_exists",
    "install_idempotent_schema_ops",
    "table_exists",
]


def columns_of(table_name: str, schema: str | None = None) -> set[str]:
    """Column names of ``schema.table_name``; empty set if it cannot be read."""
    try:
        inspector = sa.inspect(op.get_bind())
        return {c["name"] for c in inspector.get_columns(table_name, schema=schema)}
    except Exception:
        return set()


def table_exists(table_name: str, schema: str | None = None) -> bool:
    """Whether ``schema.table_name`` exists; False if it cannot be read."""
    try:
        inspector = sa.inspect(op.get_bind())
        return table_name in inspector.get_table_names(schema=schema)
    except Exception:
        return False


def index_exists(table_name: str, index_name: str, schema: str | None = None) -> bool:
    """Whether ``index_name`` exists on ``schema.table_name``."""
    try:
        inspector = sa.inspect(op.get_bind())
        return any(
            ix["name"] == index_name
            for ix in inspector.get_indexes(table_name, schema=schema)
        )
    except Exception:
        return False


def constraint_exists(
    table_name: str, constraint_name: str, schema: str | None = None
) -> bool:
    """Whether ``constraint_name`` exists on ``schema.table_name``.

    Unique, check and foreign-key names share one namespace here because the
    wrappers only ever ask "would creating this name conflict?".
    """
    try:
        inspector = sa.inspect(op.get_bind())
        unique = {
            c["name"]
            for c in inspector.get_unique_constraints(table_name, schema=schema)
        }
        checks = {
            c["name"]
            for c in inspector.get_check_constraints(table_name, schema=schema)
        }
        fks = {
            fk["name"] for fk in inspector.get_foreign_keys(table_name, schema=schema)
        }
        return constraint_name in (unique | checks | fks)
    except Exception:
        return False


def install_idempotent_schema_ops() -> None:
    """Patch ``alembic.op`` so guarded schema ops tolerate already-present state.

    Each wrapper reads the ``schema`` keyword the op itself received and asks
    the predicate about THAT schema. ``schema`` is keyword-only on every one of
    these operations in alembic 1.13, so there is no positional form to miss;
    ``create_foreign_key`` names it ``source_schema`` because the constraint
    lives on the source table.

    Post-squash migrations must call the top-level ``op`` schema methods unless
    they implement equivalent live-schema guards themselves.
    ``op.batch_alter_table`` returns a separate BatchOperations object whose
    methods do not pass through these central guards.
    """
    _original_add_column = op.add_column
    _original_drop_column = op.drop_column
    _original_create_table = op.create_table
    _original_drop_table = op.drop_table
    _original_create_index = op.create_index
    _original_drop_index = op.drop_index
    _original_create_unique_constraint = op.create_unique_constraint
    _original_create_check_constraint = op.create_check_constraint
    _original_create_foreign_key = op.create_foreign_key

    def _safe_add_column(table_name: str, column: Any, *args: Any, **kwargs: Any):
        if column.name in columns_of(table_name, kwargs.get("schema")):
            return None
        return _original_add_column(table_name, column, *args, **kwargs)

    def _safe_drop_column(table_name: str, column_name: str, *args: Any, **kwargs: Any):
        if column_name not in columns_of(table_name, kwargs.get("schema")):
            return None
        return _original_drop_column(table_name, column_name, *args, **kwargs)

    def _safe_create_table(table_name: str, *args: Any, **kwargs: Any):
        if table_exists(table_name, kwargs.get("schema")):
            return None
        return _original_create_table(table_name, *args, **kwargs)

    def _safe_drop_table(table_name: str, *args: Any, **kwargs: Any):
        if not table_exists(table_name, kwargs.get("schema")):
            return None
        return _original_drop_table(table_name, *args, **kwargs)

    def _safe_create_index(index_name: str, table_name: str, *args: Any, **kwargs: Any):
        if index_exists(table_name, index_name, kwargs.get("schema")):
            return None
        return _original_create_index(index_name, table_name, *args, **kwargs)

    def _safe_drop_index(
        index_name: str, table_name: str | None = None, *args: Any, **kwargs: Any
    ):
        if table_name and not index_exists(
            table_name, index_name, kwargs.get("schema")
        ):
            return None
        return _original_drop_index(index_name, table_name, *args, **kwargs)

    def _safe_create_unique_constraint(
        constraint_name: str, table_name: str, *args: Any, **kwargs: Any
    ):
        if constraint_exists(table_name, constraint_name, kwargs.get("schema")):
            return None
        return _original_create_unique_constraint(
            constraint_name, table_name, *args, **kwargs
        )

    def _safe_create_check_constraint(
        constraint_name: str, table_name: str, *args: Any, **kwargs: Any
    ):
        if constraint_exists(table_name, constraint_name, kwargs.get("schema")):
            return None
        return _original_create_check_constraint(
            constraint_name, table_name, *args, **kwargs
        )

    def _safe_create_foreign_key(
        constraint_name: str, source_table: str, *args: Any, **kwargs: Any
    ):
        # The constraint lives on the SOURCE table, so `source_schema` is the
        # one that decides where to look — never `referent_schema`.
        if constraint_name and constraint_exists(
            source_table, constraint_name, kwargs.get("source_schema")
        ):
            return None
        return _original_create_foreign_key(
            constraint_name, source_table, *args, **kwargs
        )

    op.add_column = _safe_add_column
    op.drop_column = _safe_drop_column
    op.create_table = _safe_create_table
    op.drop_table = _safe_drop_table
    op.create_index = _safe_create_index
    op.drop_index = _safe_drop_index
    op.create_unique_constraint = _safe_create_unique_constraint
    op.create_check_constraint = _safe_create_check_constraint
    op.create_foreign_key = _safe_create_foreign_key
