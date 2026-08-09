"""Provision the operator tenant and move settings from platform to tenant scope

ADR-0009, and the correction of a default I chose without a decision.
Migration 507 added the kernel's scope columns to `domain_settings` and left
every row at PLATFORM scope. That asserts Sub's settings belong to no tenant,
which contradicts starter ADR-0003: a single-tenant deployment provisions
exactly one tenant, and for this product shape the ISP operator IS that tenant.
In the kernel's scope model `platform` is the deployment-wide fallback BENEATH
tenant.

Two steps, in one transaction so no window exists where a settings row
references a tenant that does not exist:

1. Insert the operator tenant if absent, with the deterministic id
   `app.services.operator_tenant.OPERATOR_TENANT_ID`.
2. Move every `domain_settings` row to that tenant's scope.

The id is a literal rather than an import: a migration must not depend on
application code that can change under it. `tests/test_operator_tenant.py`
asserts this copy still matches the runtime constant, so the duplication cannot
drift silently.

Idempotent and re-runnable. Rows already at tenant scope are left alone, so a
database that has run this is unaffected by running it again.

Revision ID: 509_backfill_operator_tenant_scope
Revises: 508_operator_tenant_tables
Create Date: 2026-08-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "509_backfill_operator_tenant_scope"
down_revision: str | None = "508_operator_tenant_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Must equal `app.services.operator_tenant.OPERATOR_TENANT_ID`.
OPERATOR_TENANT_ID = "8c7ae830-51fc-52ae-9818-d84b2a35e568"
OPERATOR_TENANT_SLUG = "operator"
OPERATOR_TENANT_NAME = "Operator"


def upgrade() -> None:
    bind = op.get_bind()

    bind.execute(
        sa.text(
            """
            INSERT INTO tenants (id, slug, name, is_active, created_at, updated_at)
            VALUES (:id, :slug, :name, true, NOW(), NOW())
            ON CONFLICT (id) DO NOTHING
            """
        )
        if bind.dialect.name == "postgresql"
        else sa.text(
            """
            INSERT OR IGNORE INTO tenants
                (id, slug, name, is_active, created_at, updated_at)
            VALUES (:id, :slug, :name, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """
        ),
        {
            "id": OPERATOR_TENANT_ID,
            "slug": OPERATOR_TENANT_SLUG,
            "name": OPERATOR_TENANT_NAME,
        },
    )

    # Only platform rows move. A row already at tenant scope is left exactly as
    # it is, which is what makes re-running this safe.
    bind.execute(
        sa.text(
            """
            UPDATE domain_settings
               SET tenant_id = :tenant_id, scope_kind = 'tenant'
             WHERE scope_kind = 'platform' OR tenant_id IS NULL
            """
        ),
        {"tenant_id": OPERATOR_TENANT_ID},
    )


def downgrade() -> None:
    """Return settings to platform scope; leave the tenant row in place.

    Dropping the tenant is `508`'s job. Removing it here would orphan any row
    another slice has since attributed to it, and this migration cannot know
    whether one exists.
    """

    op.get_bind().execute(
        sa.text(
            """
            UPDATE domain_settings
               SET tenant_id = NULL, scope_kind = 'platform'
             WHERE tenant_id = :tenant_id
            """
        ),
        {"tenant_id": OPERATOR_TENANT_ID},
    )
