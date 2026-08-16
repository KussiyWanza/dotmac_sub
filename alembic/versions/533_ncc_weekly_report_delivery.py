"""Durable NCC weekly report delivery evidence.

Revision ID: 533_ncc_weekly_report_delivery
Revises: 532_sales_order_waivers
Create Date: 2026-08-15
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "533_ncc_weekly_report_delivery"
down_revision = "532_sales_order_waivers"
branch_labels = None
depends_on = None

_STATUS = "nccweeklyreportrunstatus"


def upgrade() -> None:
    status = postgresql.ENUM("failed", "queued", name=_STATUS)
    status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "ncc_weekly_report_runs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("schedule_key", sa.String(80), nullable=False),
        sa.Column("scheduled_local_date", sa.Date(), nullable=False),
        sa.Column("schedule_timezone", sa.String(80), nullable=False),
        sa.Column("scheduled_local_time", sa.String(8), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("configuration_fingerprint", sa.String(64), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM("failed", "queued", name=_STATUS, create_type=False),
            nullable=False,
        ),
        sa.Column("artifact_filename", sa.String(255)),
        sa.Column("artifact_content_type", sa.String(160)),
        sa.Column("artifact_content", sa.LargeBinary()),
        sa.Column("artifact_sha256", sa.String(64)),
        sa.Column("row_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "not_filable_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "notification_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("notifications.id", ondelete="SET NULL"),
        ),
        sa.Column("failure_code", sa.String(120)),
        sa.Column("failure_detail", sa.Text()),
        sa.Column("command_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=False),
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
        sa.UniqueConstraint(
            "schedule_key",
            "scheduled_local_date",
            name="uq_ncc_weekly_report_runs_occurrence",
        ),
        sa.CheckConstraint("row_count >= 0", name="ck_ncc_weekly_runs_row_count"),
        sa.CheckConstraint(
            "not_filable_count >= 0 AND not_filable_count <= row_count",
            name="ck_ncc_weekly_runs_not_filable_count",
        ),
        sa.CheckConstraint(
            "(status = 'queued' AND artifact_content IS NOT NULL "
            "AND artifact_sha256 IS NOT NULL AND notification_id IS NOT NULL) "
            "OR (status = 'failed' AND failure_code IS NOT NULL)",
            name="ck_ncc_weekly_runs_state_evidence",
        ),
    )
    op.create_index(
        "ix_ncc_weekly_report_runs_created",
        "ncc_weekly_report_runs",
        ["created_at"],
    )
    op.create_index(
        "ix_ncc_weekly_report_runs_notification",
        "ncc_weekly_report_runs",
        ["notification_id"],
    )

    # The old marker was a best-effort setting written after queuing. The run
    # table now owns idempotency and retains exact evidence, so remove the
    # parallel cursor while leaving the automation disabled until cutover.
    op.execute(
        sa.text(
            "DELETE FROM domain_settings "
            "WHERE domain = 'notification' "
            "AND key = 'ncc_report_email_last_sent_local_date'"
        )
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ncc_weekly_report_runs_notification",
        table_name="ncc_weekly_report_runs",
    )
    op.drop_index(
        "ix_ncc_weekly_report_runs_created", table_name="ncc_weekly_report_runs"
    )
    op.drop_table("ncc_weekly_report_runs")
    postgresql.ENUM(name=_STATUS).drop(op.get_bind(), checkfirst=True)
