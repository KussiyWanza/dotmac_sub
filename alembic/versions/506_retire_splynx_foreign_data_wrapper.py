"""Retire the foreign-data wrapper onto the former billing system.

Production carries a ``splynx_fdw`` schema of 26 foreign tables against a
``splynx_server`` pointing at 149.102.135.97:5435. The remote does not answer —
a select against it times out — and nothing references the schema anywhere in
the application, migrations, scripts or tests. Foreign tables hold no data of
their own, so removing them loses nothing; the data lived on the remote that is
gone.

None of it was ever created by a migration. It was hand-run DDL during the
migration era, which is why the objects exist in production and in no
environment built from this chain. Dropping it by hand would be a second
untracked change of exactly the kind that produced the drift, so the removal is
recorded here instead: any environment still carrying the residue is cleaned
when it upgrades, and one built from scratch is unaffected.

The user mapping is dropped with it, which also removes a stored credential for
a host that no longer exists.

Downgrade deliberately does not recreate any of this. Restoring a connection to
a decommissioned system is not a migration concern, and the remote is gone.

Revision ID: 506_retire_splynx_foreign_data_wrapper
Revises: 505_retire_scheduler_refresh_minutes
Create Date: 2026-08-08
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "506_retire_splynx_foreign_data_wrapper"
down_revision: str | None = "505_retire_scheduler_refresh_minutes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SERVER = "splynx_server"
_SCHEMA = "splynx_fdw"


def upgrade() -> None:
    # Every statement is conditional: most environments never had these
    # objects, and the migration must be a no-op there rather than an error.
    op.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_foreign_server WHERE srvname = '{_SERVER}')
            THEN
                DROP SERVER {_SERVER} CASCADE;
            END IF;
        END
        $$
        """
    )


def downgrade() -> None:
    """No-op.

    The foreign server addressed a decommissioned host. Recreating a broken
    connection would restore the drift, not the capability.
    """
