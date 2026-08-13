"""Retire the splynx_staging import landing schema.

Revision ID: 517_retire_splynx_staging_schema
Revises: 516_material_request_erp_submission
Create Date: 2026-08-11

`splynx_staging` is the raw landing area the original Splynx import tooling
built: 42 tables, 22,896,640 rows, 3.7 GB. Its contents have already been
consumed into the schema the application actually reads --

    invoices          public.invoices holds 113,540 carried-in rows against
                      108,485 staged ones
    payments          public.payments holds 100,013 against 93,967 staged
    telemetry         public.subscriber_daily_usage holds 3,313,929 daily rows
                      spanning 2018-02-24..2026-06-16, the aggregate form of
                      the staged per-session detail

Nothing reads the schema. It is named by no model, no service, no script, no
test and no document; every model resolves to `public` because none declares a
schema; and it is absent from the database `search_path`.

Staging is NOT a strict subset of what was kept, which is why this landed as
its own reviewed change rather than riding along with the earlier retirements.
Roughly 10,631 churned Splynx customers, 36 tickets, and the raw per-session
telemetry (13.3M statistics rows, 3.1M traffic counters) exist nowhere else.
Those were archived before this revision was written:

    dotmac-private/db-archives/splynx_staging_2026-08-11.dump
    sha256 20b2e815e0da4006d3b501a7fea4c36b0645fdae1fb0e3c81fd7e18c0980e2fd

The archive was verified table-by-table against the live schema -- same 42
tables, every row count identical, 22,896,640 rows total -- and its checksum
was confirmed after round-tripping back out of object storage.

CASCADE rather than 42 ordered DROP TABLEs: the tables carry foreign keys
among themselves, so a hand-maintained order is error-prone bulk for DDL that
does not live in this repo. CASCADE's real hazard is silently taking an
external dependent with it, so that is exactly what the guard below checks,
enumerating dependents LIVE rather than trusting a list written from today's
production.

Deliberately no row-count gate. An earlier attempt at this retirement refused
on any row present, on the belief the schema was empty; it has not been empty
since the evidence import. Rows are the expected condition here, and the
archive -- not an assertion -- is what makes dropping them safe.

IF EXISTS because Alembic never created this schema. A database built by
`001_squashed` has never had it, so a fresh chain run must no-op rather than
fail.
"""

from __future__ import annotations

from sqlalchemy import text

from alembic import op

revision = "517_retire_splynx_staging_schema"
down_revision = "516_material_request_erp_submission"
branch_labels = None
depends_on = None

SCHEMA = "splynx_staging"

ARCHIVE = (
    "dotmac-private/db-archives/splynx_staging_2026-08-11.dump "
    "(sha256 20b2e815e0da4006d3b501a7fea4c36b0645fdae1fb0e3c81fd7e18c0980e2fd)"
)


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return

    present = conn.execute(
        text("SELECT 1 FROM pg_namespace WHERE nspname = :schema"),
        {"schema": SCHEMA},
    ).scalar()
    if not present:
        return

    # Refuse if anything outside the schema depends on it. This is the one
    # case CASCADE would resolve by destroying a live object, so it fails
    # closed and names what it found rather than guessing.
    dependents = (
        conn.execute(
            text(
                """
            SELECT DISTINCT dependent_ns.nspname || '.' || dependent.relname
            FROM pg_depend d
            JOIN pg_class referenced ON referenced.oid = d.refobjid
            JOIN pg_namespace referenced_ns
              ON referenced_ns.oid = referenced.relnamespace
            JOIN pg_class dependent ON dependent.oid = d.objid
            JOIN pg_namespace dependent_ns
              ON dependent_ns.oid = dependent.relnamespace
            WHERE referenced_ns.nspname = :schema
              AND dependent_ns.nspname NOT IN (:schema, 'pg_toast')
            ORDER BY 1
            """
            ),
            {"schema": SCHEMA},
        )
        .scalars()
        .all()
    )

    if dependents:
        raise RuntimeError(
            f"Refusing to drop {SCHEMA}: "
            f"{len(dependents)} object(s) outside it depend on it "
            f"({', '.join(dependents[:10])}). CASCADE would drop them too. "
            "Re-point or retire those dependents first."
        )

    op.execute(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE')


def downgrade() -> None:
    """Deliberately not reversible.

    Recreating an empty schema would be a lie -- it would restore the name
    without the 22,896,640 rows, and a later upgrade would then drop an empty
    schema and report success. Restore from the archive instead.
    """

    raise RuntimeError(
        f"{SCHEMA} cannot be recreated by a downgrade. "
        f"Restore it from {ARCHIVE} with `pg_restore -n {SCHEMA}`."
    )
