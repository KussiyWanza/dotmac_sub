"""Keyset cycle checkpoint for the bounded prepaid sweep.

Revision ID: 451_prepaid_sweep_cycle
Revises: 450_fiber_test_acceptance
Create Date: 2026-08-01
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "451_prepaid_sweep_cycle"
down_revision: str | None = "450_fiber_test_acceptance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "prepaid_sweep_cycle_state",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("runner", sa.String(length=64), nullable=False, unique=True),
        sa.Column("cursor_key", sa.String(length=64), nullable=True),
        sa.Column("cycle_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "cycles_completed",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("prepaid_sweep_cycle_state")
