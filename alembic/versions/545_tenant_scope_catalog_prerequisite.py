"""Supply `tenant_scope_catalog.v1` from Sub's own lineage.

Revision ID: 545_tenant_scope_catalog_prerequisite
Revises: 544_carried_source_adjudication
Create Date: 2026-08-20

ADR-0011 (module lineages compose beside Sub's own): an installable module
declares the database EFFECTS it needs, never a foreign revision, and this
repository answers with its own revisions rather than by running kernel `0001`.
`dotmac-ipam` and every other network module declares
`requires=(TENANT_SCOPE_CATALOG_V1.name, MODULE_DATABASE_ROLES_V1.name)`.

The kernel documents a product supplying this from its own lineage as the
intended case, with ERP's `20260813_tenant_projection` as the worked example.
Sub is in the same position: migrations 508/509 already host `tenants` and
`tenant_domains` under ADR-0009's operator-tenant bridge, and Sub structurally
does not run kernel `0001`.

Two gaps, both measured against `dotmac_kernel.migrations.verify` rather than
assumed from the fact that the tables exist:

1. **`public.app_current_tenant_id()` does not exist.** Sub already sets the
   exact GUC the kernel names — `app.current_tenant`, in
   `app.services.operator_tenant` — but never defined the function that reads
   it. The body here is kernel `0001`'s verbatim, and it must stay that way:
   the verifier checks the real function definition for `returns uuid`,
   `stable`, `current_setting('app.current_tenant', true)` and
   `invalid_text_representation`, because a function of the right name that
   reads a different GUC, or is VOLATILE, or raises on a malformed value
   instead of returning NULL, silently changes what every RLS policy in every
   composed module evaluates to.

2. **Four timestamp columns have no server default.** 508 created
   `tenants`/`tenant_domains` with `created_at`/`updated_at` `NOT NULL` but no
   `DEFAULT`, while the contract requires one on each. Sub's ORM has always
   supplied the value, so nothing was broken — but a module's migration
   inserting a catalogue row, or any writer that is not Sub's ORM, would hit a
   NOT NULL violation. This is the column-level half of the contract, and it is
   why "the table exists" was not the same as "the prerequisite is satisfied".

Additive and forward-only. No row changes, no behaviour changes for existing
writers, and nothing here composes a module or a kernel lineage.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "545_tenant_scope_catalog_prerequisite"
down_revision: str | None = "544_carried_source_adjudication"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: (table, column) pairs the contract requires a server default on.
_TIMESTAMP_DEFAULTS: tuple[tuple[str, str], ...] = (
    ("tenants", "created_at"),
    ("tenants", "updated_at"),
    ("tenant_domains", "created_at"),
    ("tenant_domains", "updated_at"),
)

#: Kernel `0001`'s definition, verbatim. See the module docstring for why the
#: body is copied rather than paraphrased.
_CREATE_FUNCTION = """
CREATE OR REPLACE FUNCTION app_current_tenant_id()
RETURNS uuid
LANGUAGE plpgsql
STABLE
AS $$
BEGIN
    RETURN NULLIF(current_setting('app.current_tenant', true), '')::uuid;
EXCEPTION
    WHEN invalid_text_representation THEN
        RETURN NULL;
END;
$$;
"""


def upgrade() -> None:
    for table, column in _TIMESTAMP_DEFAULTS:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} SET DEFAULT now();")
    op.execute(_CREATE_FUNCTION)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS app_current_tenant_id();")
    for table, column in _TIMESTAMP_DEFAULTS:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} DROP DEFAULT;")
