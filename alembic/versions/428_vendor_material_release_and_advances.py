"""Vendor material releases and vendor advances.

Two project-anchored vendor obligations the existing models could not carry:
``field_material_requests`` is work-order scoped with a technician requester, so
a contractor drawing our cable for a buildout had no path; and nothing modelled
an advance at all.

Both hold provider-neutral references plus an explicit source-system name per
``docs/BACKOFFICE_INTEGRATION_BOUNDARY.md`` — correlation evidence, never
delegated decision authority.

Revision ID: 428_vendor_material_release_and_advances
Revises: 427_vendor_principal_user_type
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "428_vendor_material_release_and_advances"
down_revision = "427_vendor_principal_user_type"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "vendor_material_releases",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("vendor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "status", sa.String(length=20), nullable=False, server_default="draft"
        ),
        sa.Column(
            "requested_by_person_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "reviewed_by_person_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_notes", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("support_system", sa.String(length=40), nullable=True),
        sa.Column("support_reference", sa.String(length=120), nullable=True),
        sa.Column("support_status", sa.String(length=40), nullable=True),
        sa.Column("support_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["project_id"], ["installation_projects.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["vendor_id"], ["vendors.id"], ondelete="RESTRICT"),
    )
    op.create_index(
        "ix_vendor_material_releases_project",
        "vendor_material_releases",
        ["project_id", "status"],
    )
    op.create_index(
        "ix_vendor_material_releases_vendor",
        "vendor_material_releases",
        ["vendor_id"],
    )

    op.create_table(
        "vendor_material_release_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("release_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("item_code", sa.String(length=80), nullable=True),
        sa.Column("description", sa.String(length=255), nullable=False),
        sa.Column("unit", sa.String(length=40), nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("issued_quantity", sa.Integer(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["release_id"], ["vendor_material_releases.id"], ondelete="CASCADE"
        ),
        sa.CheckConstraint(
            "quantity > 0", name="ck_vendor_material_release_items_quantity_positive"
        ),
    )
    op.create_index(
        "ix_vendor_material_release_items_release",
        "vendor_material_release_items",
        ["release_id"],
    )

    op.create_table(
        "vendor_advances",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("vendor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("quote_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "status", sa.String(length=20), nullable=False, server_default="requested"
        ),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "requested_by_person_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "reviewed_by_person_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_notes", sa.Text(), nullable=True),
        sa.Column("payables_system", sa.String(length=40), nullable=True),
        sa.Column("payables_reference", sa.String(length=120), nullable=True),
        sa.Column("payables_status", sa.String(length=40), nullable=True),
        sa.Column("payables_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["project_id"], ["installation_projects.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["vendor_id"], ["vendors.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["quote_id"], ["project_quotes.id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint("amount > 0", name="ck_vendor_advances_amount_positive"),
    )
    op.create_index(
        "ix_vendor_advances_project", "vendor_advances", ["project_id", "status"]
    )
    op.create_index("ix_vendor_advances_vendor", "vendor_advances", ["vendor_id"])


def downgrade() -> None:
    op.drop_index("ix_vendor_advances_vendor", table_name="vendor_advances")
    op.drop_index("ix_vendor_advances_project", table_name="vendor_advances")
    op.drop_table("vendor_advances")
    op.drop_index(
        "ix_vendor_material_release_items_release",
        table_name="vendor_material_release_items",
    )
    op.drop_table("vendor_material_release_items")
    op.drop_index(
        "ix_vendor_material_releases_vendor", table_name="vendor_material_releases"
    )
    op.drop_index(
        "ix_vendor_material_releases_project", table_name="vendor_material_releases"
    )
    op.drop_table("vendor_material_releases")
