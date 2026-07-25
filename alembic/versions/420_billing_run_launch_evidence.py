"""Add durable staff launch evidence and retire the unused schedule facade.

Revision ID: 420_billing_run_launch_evidence
Revises: 419_customer_wht_policy_and_direct_targets
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "420_billing_run_launch_evidence"
down_revision = "419_customer_wht_policy_and_direct_targets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM domain_settings "
            "WHERE domain = 'billing' AND key = 'billing_run_schedule_config'"
        )
    )
    op.drop_table("billing_run_schedules")
    op.add_column(
        "billing_runs",
        sa.Column(
            "launch_kind",
            sa.String(length=24),
            nullable=False,
            server_default="scheduled",
        ),
    )
    op.add_column(
        "billing_runs",
        sa.Column("requested_by", sa.String(length=120), nullable=True),
    )
    op.add_column(
        "billing_runs",
        sa.Column("preview_fingerprint", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "billing_runs",
        sa.Column(
            "source_run_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_billing_runs_source_run_id",
        "billing_runs",
        "billing_runs",
        ["source_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_billing_runs_source_run_id",
        "billing_runs",
        ["source_run_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_billing_runs_source_run_id", table_name="billing_runs")
    op.drop_constraint(
        "fk_billing_runs_source_run_id",
        "billing_runs",
        type_="foreignkey",
    )
    op.drop_column("billing_runs", "source_run_id")
    op.drop_column("billing_runs", "preview_fingerprint")
    op.drop_column("billing_runs", "requested_by")
    op.drop_column("billing_runs", "launch_kind")
    op.create_table(
        "billing_run_schedules",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("run_day", sa.Integer(), nullable=False),
        sa.Column("run_time", sa.String(length=8), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("billing_cycle", sa.String(length=40), nullable=False),
        sa.Column("partner_ids", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="billing_run_schedules_pkey"),
    )
