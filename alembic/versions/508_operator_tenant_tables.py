"""Create tenants and tenant_domains for the operator-tenant bridge

ADR-0009. Sub has no tenant; the kernel is multi-tenant by construction, so
every stateful kernel module meets that wall. This creates the two tables the
kernel's `Tenant`/`TenantDomain` map to, so Sub can provision its one operator
tenant.

Expand only: no row is written here and nothing reads these tables yet.
Provisioning and the `domain_settings` backfill from platform to tenant scope
follow separately.

**Sub writes this migration itself rather than composing the kernel's Alembic
lineage.** That is not a stylistic choice: kernel revision `0004_custom_fields`
executes `op.add_column("parties", ...)`, and Sub has its own `parties` table,
so composing the lineages would alter a live Sub table. Composition can only
follow resolution of the six colliding tables, not enable it — see the ledger's
Alembic section.

The column definitions mirror `dotmac_kernel.models.Tenant` and `TenantDomain`
exactly, because the kernel's ORM reads these tables. `tenants` and
`tenant_domains` carry no `tenant_id` and get no RLS: they ARE the tenant, and
the kernel's resolver reads them before tenant context exists.

Revision ID: 508_operator_tenant_tables
Revises: 507_domain_settings_scope_columns
Create Date: 2026-08-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "508_operator_tenant_tables"
down_revision: str | None = "507_domain_settings_scope_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_table(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if not _has_table("tenants"):
        op.create_table(
            "tenants",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("slug", sa.String(length=63), nullable=False),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.Column(
                "is_active", sa.Boolean(), nullable=False, server_default=sa.true()
            ),
            sa.Column("suspended_at", sa.DateTime(timezone=True)),
            sa.Column("deleted_at", sa.DateTime(timezone=True)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("slug", name="uq_tenants_slug"),
        )
        op.create_index("ix_tenants_slug", "tenants", ["slug"])

    if not _has_table("tenant_domains"):
        op.create_table(
            "tenant_domains",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column(
                "tenant_id",
                sa.Uuid(),
                sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("domain", sa.String(length=253), nullable=False),
            sa.Column("verified_at", sa.DateTime(timezone=True)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("domain", name="uq_tenant_domains_domain"),
        )
        op.create_index("ix_tenant_domains_tenant_id", "tenant_domains", ["tenant_id"])


def downgrade() -> None:
    """Clean while nothing references a tenant.

    Once a second table is tenant-scoped this stops being reversible, which is
    the point at which ADR-0009 hardens.
    """

    if _has_table("tenant_domains"):
        op.drop_table("tenant_domains")
    if _has_table("tenants"):
        op.drop_table("tenants")
