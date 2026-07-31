"""add planned splice work (cut sheets) bound to work orders

Revision ID: 449_fiber_splice_plans
Revises: 448_fiber_segment_color_construction
Create Date: 2026-07-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "449_fiber_splice_plans"
down_revision: str | None = "448_fiber_segment_color_construction"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "fiber_splice_plans",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("work_order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_by_person_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('draft', 'issued', 'cancelled')",
            name="ck_fiber_splice_plans_status_known",
        ),
        sa.ForeignKeyConstraint(
            ["work_order_id"], ["work_order.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_fiber_splice_plans_work_order_id",
        "fiber_splice_plans",
        ["work_order_id"],
    )
    op.create_index(
        "uq_fiber_splice_plans_live_work_order",
        "fiber_splice_plans",
        ["work_order_id"],
        unique=True,
        postgresql_where=sa.text("status != 'cancelled'"),
        sqlite_where=sa.text("status != 'cancelled'"),
    )
    op.create_table(
        "fiber_splice_plan_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("plan_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("position_index", sa.Integer(), nullable=False),
        sa.Column("closure_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tray_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("tray_position", sa.Integer(), nullable=True),
        sa.Column("from_strand_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("from_strand_end", sa.String(length=1), nullable=False),
        sa.Column("to_strand_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("to_strand_end", sa.String(length=1), nullable=False),
        sa.Column("splice_type", sa.String(length=80), nullable=False),
        sa.Column("expected_loss_db", sa.Float(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "executed_change_request_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "from_strand_id <> to_strand_id",
            name="ck_fiber_splice_plan_items_distinct_strands",
        ),
        sa.CheckConstraint(
            "from_strand_end IN ('a', 'b') AND to_strand_end IN ('a', 'b')",
            name="ck_fiber_splice_plan_items_strand_ends",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"], ["fiber_splice_plans.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["closure_id"], ["fiber_splice_closures.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["tray_id"], ["fiber_splice_trays.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["from_strand_id"], ["fiber_strands.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["to_strand_id"], ["fiber_strands.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["executed_change_request_id"],
            ["fiber_change_requests.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "plan_id", "position_index", name="uq_fiber_splice_plan_items_position"
        ),
    )
    op.create_index(
        "ix_fiber_splice_plan_items_plan_id",
        "fiber_splice_plan_items",
        ["plan_id"],
    )
    op.create_index(
        "uq_fiber_splice_plan_items_execution",
        "fiber_splice_plan_items",
        ["executed_change_request_id"],
        unique=True,
        postgresql_where=sa.text("executed_change_request_id IS NOT NULL"),
        sqlite_where=sa.text("executed_change_request_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_fiber_splice_plan_items_execution", table_name="fiber_splice_plan_items"
    )
    op.drop_index(
        "ix_fiber_splice_plan_items_plan_id", table_name="fiber_splice_plan_items"
    )
    op.drop_table("fiber_splice_plan_items")
    op.drop_index(
        "uq_fiber_splice_plans_live_work_order", table_name="fiber_splice_plans"
    )
    op.drop_index(
        "ix_fiber_splice_plans_work_order_id", table_name="fiber_splice_plans"
    )
    op.drop_table("fiber_splice_plans")
