"""Persist the unique reusable Lead for each customer-backed Quote flow.

Revision ID: 528_customer_quote_lead_linkage
Revises: 527_credential_party_binding_additive
Create Date: 2026-08-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "528_customer_quote_lead_linkage"
down_revision: str | None = "527_credential_party_binding_additive"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "customer_quote_lead_links",
        sa.Column("subscriber_id", sa.Uuid(), nullable=False),
        sa.Column("lead_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["subscriber_id"], ["subscribers.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("subscriber_id"),
        sa.UniqueConstraint("lead_id", name="uq_customer_quote_lead_links_lead_id"),
    )


def downgrade() -> None:
    op.drop_table("customer_quote_lead_links")
