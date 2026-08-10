"""Give domain_settings the kernel's scope columns (expand only)

First step of the kernel settings cutover, and pure EXPAND: Sub's code does not
read these columns, no behaviour changes, and nothing is retired. What it buys
is that `dotmac_kernel.settings_models.DomainSetting` can read Sub's
`domain_settings` table at all — until now it could not, because the kernel's
model requires three columns Sub's table lacks. Without this, a parity harness
comparing the two resolvers cannot even be written.

The delta is exactly three columns. Everything else — `domain`, `key`,
`value_type`, `value_text`, `value_json`, `is_secret`, `is_active`, timestamps —
already matches the kernel's model.

Two traps, both from reading the kernel's source rather than assuming:

1. **`scope_kind`'s kernel server default is `'tenant'`.** Sub is a
   single-operator deployment: every existing row is platform scope with no
   tenant. Adopting the kernel's default would silently relabel all of them
   `tenant` while `tenant_id` stayed NULL — a scope claim contradicted by the
   row's own data. This backfills `'platform'` explicitly and sets the server
   default to `'platform'` for the same reason: a Sub row created without an
   explicit scope is a platform row.

2. **Uniqueness must move to the kernel's COALESCE index.** Sub has
   `UniqueConstraint(domain, key)`. The kernel uses a unique index over
   `COALESCE`d nullable columns, because PostgreSQL treats NULL as distinct
   inside a unique constraint, so a plain composite over nullable scope columns
   would admit duplicate platform rows. Keeping Sub's two-column constraint
   would also forbid the same key at two scopes, which is a different model
   from the one the kernel resolves against.

Revision ID: 507_domain_settings_scope_columns
Revises: 506_retire_splynx_foreign_data_wrapper
Create Date: 2026-08-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "507_domain_settings_scope_columns"
down_revision: str | None = "506_retire_splynx_foreign_data_wrapper"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "domain_settings"
OLD_UNIQUE = "uq_domain_settings_domain_key"
NEW_UNIQUE = "uq_domain_settings_scope_domain_key"
#: The kernel's sentinel for a NULL inside the uniqueness index.
NIL_UUID = "00000000-0000-0000-0000-000000000000"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _has_column(name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return name in {column["name"] for column in inspector.get_columns(TABLE)}


def upgrade() -> None:
    if not _has_column("tenant_id"):
        op.add_column(TABLE, sa.Column("tenant_id", sa.Uuid(), nullable=True))
    if not _has_column("scope_id"):
        op.add_column(TABLE, sa.Column("scope_id", sa.Uuid(), nullable=True))
    if not _has_column("scope_kind"):
        # server_default 'platform', NOT the kernel's 'tenant' — see the
        # docstring. Existing rows are backfilled by the same default.
        op.add_column(
            TABLE,
            sa.Column(
                "scope_kind",
                sa.String(length=20),
                nullable=False,
                server_default="platform",
            ),
        )

    if not _is_postgres():
        # SQLite builds this schema from model metadata; the uniqueness swap
        # below is a PostgreSQL index shape with no SQLite equivalent.
        return

    op.execute(sa.text(f"UPDATE {TABLE} SET scope_kind = 'platform'"))
    op.execute(sa.text(f"ALTER TABLE {TABLE} DROP CONSTRAINT IF EXISTS {OLD_UNIQUE}"))
    op.execute(
        sa.text(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {NEW_UNIQUE} ON {TABLE} "
            f"(domain, key, COALESCE(tenant_id, '{NIL_UUID}'), scope_kind, "
            f"COALESCE(scope_id, '{NIL_UUID}'))"
        )
    )


def downgrade() -> None:
    """Restores the two-column constraint, and will fail loudly if it cannot.

    Once rows exist at more than one scope, `(domain, key)` is no longer unique
    and the constraint cannot be recreated. Failing is correct: silently
    dropping rows to make a downgrade succeed would lose settings.
    """

    if _is_postgres():
        op.execute(sa.text(f"DROP INDEX IF EXISTS {NEW_UNIQUE}"))
        op.execute(
            sa.text(
                f"ALTER TABLE {TABLE} ADD CONSTRAINT {OLD_UNIQUE} UNIQUE (domain, key)"
            )
        )
    for column in ("scope_kind", "scope_id", "tenant_id"):
        if _has_column(column):
            op.drop_column(TABLE, column)
