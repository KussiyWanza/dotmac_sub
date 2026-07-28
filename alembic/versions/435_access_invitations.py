"""Add the access-invitation lifecycle aggregate.

Identity/onboarding chain (docs/designs/IDENTITY_ONBOARDING_CHAIN.md):
invitations gain issued/accepted/expired/revoked evidence with a durable
expiry timer. Capabilities keep their redeem-time fail-closed TTL checks;
this table is lifecycle evidence, never an access grant.

Revision ID: 435_access_invitations
Revises: 434_sales_funding_erp_exports
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "435_access_invitations"
down_revision = "434_sales_funding_erp_exports"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "access_invitations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("principal_type", sa.String(length=40), nullable=False),
        sa.Column("principal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("purpose", sa.String(length=40), nullable=False),
        sa.Column(
            "status", sa.String(length=20), nullable=False, server_default="issued"
        ),
        sa.Column("email_sha256", sa.String(length=64), nullable=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(length=200), nullable=True),
        sa.Column("source", sa.String(length=40), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_access_invitations_principal",
        "access_invitations",
        ["principal_type", "principal_id"],
    )
    op.create_index("ix_access_invitations_status", "access_invitations", ["status"])


def downgrade() -> None:
    op.drop_index("ix_access_invitations_status", table_name="access_invitations")
    op.drop_index("ix_access_invitations_principal", table_name="access_invitations")
    op.drop_table("access_invitations")
