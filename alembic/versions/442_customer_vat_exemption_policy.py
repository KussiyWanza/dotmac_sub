"""Add customer VAT exemption policy.

Revision ID: 442_customer_vat_exemption_policy
Revises: 441_network_zone_geo_area_binding
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "442_customer_vat_exemption_policy"
down_revision = "441_network_zone_geo_area_binding"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "customer_tax_policies",
        sa.Column(
            "vat_exempt",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("customer_tax_policies", "vat_exempt")
