"""Allow migrated opening funding consumption source.

Revision ID: 577_migrated_opening_consumption_source
Revises: 576_support_csat_requests
Create Date: 2026-09-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "577_migrated_opening_consumption_source"
down_revision: str | None = "576_support_csat_requests"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "prepaid_opening_funding_consumptions"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column("opening_position_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.alter_column(
        _TABLE,
        "baseline_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )
    op.create_foreign_key(
        "fk_prepaid_opening_consumption_opening_position",
        _TABLE,
        "customer_subledger_opening_positions",
        ["opening_position_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_prepaid_opening_consumption_one_source",
        _TABLE,
        "(baseline_id IS NOT NULL AND opening_position_id IS NULL) "
        "OR (baseline_id IS NULL AND opening_position_id IS NOT NULL)",
    )
    op.create_index(
        "ix_prepaid_opening_consumption_opening_position",
        _TABLE,
        ["opening_position_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_prepaid_opening_consumption_opening_position",
        table_name=_TABLE,
    )
    op.drop_constraint(
        "ck_prepaid_opening_consumption_one_source",
        _TABLE,
        type_="check",
    )
    op.drop_constraint(
        "fk_prepaid_opening_consumption_opening_position",
        _TABLE,
        type_="foreignkey",
    )
    op.alter_column(
        _TABLE,
        "baseline_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )
    op.drop_column(_TABLE, "opening_position_id")
