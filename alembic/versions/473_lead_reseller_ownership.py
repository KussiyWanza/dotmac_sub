"""add optional reseller ownership to leads

Revision ID: 473_lead_reseller_ownership
Revises: 472_service_extension_reversals
Create Date: 2026-08-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "473_lead_reseller_ownership"
down_revision: str | None = "472_service_extension_reversals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "leads",
        sa.Column("reseller_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_leads_reseller_id_resellers",
        "leads",
        "resellers",
        ["reseller_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_leads_reseller_id", "leads", ["reseller_id"])


def downgrade() -> None:
    op.drop_index("ix_leads_reseller_id", table_name="leads")
    op.drop_constraint("fk_leads_reseller_id_resellers", "leads", type_="foreignkey")
    op.drop_column("leads", "reseller_id")
