"""The migration guards ask about the schema the op actually targets.

``alembic/env.py`` monkey-patches nine ``alembic.op`` functions so the
post-squash chain tolerates state the squashed ``001`` migration already built.
Those wrappers apply to EVERY revision in a run. The moment Sub composes an
installable module's lineage, that includes fully schema-qualified DDL writing
into a ``mod_*`` schema.

The failure this file exists to make impossible: a guard that asks "is there a
table called ``addresses``?" without saying WHERE answers about ``public``. If
``public.addresses`` exists, the wrapper returns ``None``, the revision is
stamped as applied, and ``mod_ipam.addresses`` is never created. Nothing raises
— the lineage reports itself current and the first query dies on
``UndefinedTable`` in production.

Six real collisions make this reachable rather than theoretical:
``addresses`` (ipam / subscriber), ``alerts`` (network-observability /
network_monitoring), ``pon_ports`` (pon-access / network), ``ports``
(network-inventory / network), ``sessions`` (network-access / auth) and
``vlans`` (network-inventory / network). None is an authority collision —
``mod_ipam.addresses`` is an IP address and Sub's is a street address — which
is exactly why nobody would notice by reading the DDL.

PostgreSQL only, deliberately: the bug is about schema qualification, and
SQLite has no comparable notion. Every test here rolls its DDL back.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.engine import Engine

from alembic import op
from app.migration_schema_ops import (
    columns_of,
    constraint_exists,
    index_exists,
    install_idempotent_schema_ops,
    table_exists,
)

#: One bare name that exists in `public` and is created again, elsewhere.
SHARED_NAME = "guard_canary_widgets"
CANARY_SCHEMA = "mod_guard_canary"

#: The nine ops the wrappers patch. Saved and restored so one test's install
#: cannot double-wrap the next one's.
PATCHED_OPS = (
    "add_column",
    "drop_column",
    "create_table",
    "drop_table",
    "create_index",
    "drop_index",
    "create_unique_constraint",
    "create_check_constraint",
    "create_foreign_key",
)


@pytest.fixture
def guarded_ops(engine: Engine):
    """A rolled-back transaction with the real wrappers installed on ``op``.

    Yields the live connection. ``public.<SHARED_NAME>`` exists and
    ``CANARY_SCHEMA`` exists and is empty — the exact shape that makes a
    schema-blind guard answer the wrong question.
    """
    saved = {name: getattr(op, name) for name in PATCHED_OPS}
    connection = engine.connect()
    transaction = connection.begin()
    try:
        connection.exec_driver_sql(f'CREATE SCHEMA "{CANARY_SCHEMA}"')
        connection.exec_driver_sql(
            f'CREATE TABLE public."{SHARED_NAME}" '
            "(id integer PRIMARY KEY, only_in_public text)"
        )
        migration_context = MigrationContext.configure(connection)
        with Operations.context(migration_context):
            install_idempotent_schema_ops()
            yield connection
    finally:
        for name, original in saved.items():
            setattr(op, name, original)
        transaction.rollback()
        connection.close()


def _qualified_table_names(connection, schema: str) -> set[str]:
    """Read the catalog directly — never through the code under test."""
    rows = connection.execute(
        sa.text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = :schema"
        ),
        {"schema": schema},
    )
    return {row[0] for row in rows}


def test_a_qualified_create_is_not_skipped_by_a_same_named_public_table(
    guarded_ops,
) -> None:
    """THE canary. This is the silent-corruption path, driven end to end."""
    connection = guarded_ops

    op.create_table(
        SHARED_NAME,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("only_in_module", sa.Text()),
        schema=CANARY_SCHEMA,
    )

    assert SHARED_NAME in _qualified_table_names(connection, CANARY_SCHEMA), (
        f"{CANARY_SCHEMA}.{SHARED_NAME} was NOT created. The guard answered "
        f"about public.{SHARED_NAME} instead of the schema the op named, "
        "returned None, and the revision would have been stamped as applied "
        "with the table absent."
    )

    # It is the module's table, not a view of Sub's — the columns differ.
    assert "only_in_module" in columns_of(SHARED_NAME, CANARY_SCHEMA)
    assert "only_in_public" not in columns_of(SHARED_NAME, CANARY_SCHEMA)


def test_the_canary_would_catch_a_regression_to_the_schema_blind_guard(
    guarded_ops,
) -> None:
    """Sensitivity proof: the old predicate really does answer "yes" here.

    Without this, the test above could pass for the wrong reason — e.g. if the
    two names never actually collided in the default schema, it would prove
    nothing about the guard. This reconstructs the pre-fix predicate (an
    inspector call with no ``schema=``) and asserts it reports the bare name as
    present while ``CANARY_SCHEMA`` is still empty. That "yes" is precisely what
    used to skip the create.
    """
    connection = guarded_ops

    schema_blind = SHARED_NAME in sa.inspect(connection).get_table_names()
    assert schema_blind, (
        "the fixture no longer creates a colliding public table, so the canary "
        "above proves nothing — fix the fixture, not this assertion"
    )
    assert not table_exists(SHARED_NAME, CANARY_SCHEMA), (
        "the schema-qualified predicate must say NO here; if it says yes the "
        "guard is still reading the wrong schema"
    )


def test_unqualified_ops_keep_their_squash_tolerating_behaviour(
    guarded_ops,
) -> None:
    """The regime this file must not break: unqualified ops still no-op.

    Every Sub revision today is unqualified and depends on exactly this.
    """
    connection = guarded_ops

    op.create_table(
        SHARED_NAME,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("never_added", sa.Text()),
    )
    op.add_column(SHARED_NAME, sa.Column("only_in_public", sa.Text()))

    assert table_exists(SHARED_NAME)
    assert "never_added" not in columns_of(SHARED_NAME), (
        "the unqualified create was NOT skipped — the squash-tolerating "
        "behaviour the post-squash chain depends on has regressed"
    )
    # The duplicate add_column was skipped rather than raising DuplicateColumn.
    duplicates = connection.execute(
        sa.text(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :table "
            "AND column_name = 'only_in_public'"
        ),
        {"table": SHARED_NAME},
    ).scalar()
    assert duplicates == 1


def test_every_predicate_is_schema_qualified(guarded_ops) -> None:
    """All four predicates, not just the one the canary happens to exercise.

    A fix that only reached ``table_exists`` would leave the same defect in
    column, index and constraint guarding.
    """
    op.create_table(
        SHARED_NAME,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("only_in_module", sa.Text()),
        schema=CANARY_SCHEMA,
    )
    op.create_index(
        "ix_guard_canary", SHARED_NAME, ["only_in_module"], schema=CANARY_SCHEMA
    )
    op.create_unique_constraint(
        "uq_guard_canary", SHARED_NAME, ["only_in_module"], schema=CANARY_SCHEMA
    )

    # Present in the module schema...
    assert table_exists(SHARED_NAME, CANARY_SCHEMA)
    assert "only_in_module" in columns_of(SHARED_NAME, CANARY_SCHEMA)
    assert index_exists(SHARED_NAME, "ix_guard_canary", CANARY_SCHEMA)
    assert constraint_exists(SHARED_NAME, "uq_guard_canary", CANARY_SCHEMA)

    # ...and absent from public, whose same-named table has none of them.
    assert "only_in_module" not in columns_of(SHARED_NAME)
    assert not index_exists(SHARED_NAME, "ix_guard_canary")
    assert not constraint_exists(SHARED_NAME, "uq_guard_canary")


def test_a_missing_schema_does_not_report_phantom_objects(guarded_ops) -> None:
    """An unreadable target degrades to "absent", so the wrapper delegates.

    The predicates swallow inspector errors on purpose. The direction matters:
    returning "absent" makes the wrapper call the real op, so alembic raises its
    own error. Returning "present" would silently skip real DDL — the bug this
    file is about.
    """
    assert not table_exists(SHARED_NAME, "mod_does_not_exist")
    assert columns_of(SHARED_NAME, "mod_does_not_exist") == set()
    assert not index_exists(SHARED_NAME, "ix_guard_canary", "mod_does_not_exist")
    assert not constraint_exists(SHARED_NAME, "uq_guard_canary", "mod_does_not_exist")
