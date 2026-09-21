"""Make Team Inbox queue lifecycles and notification state explicit.

Revision ID: 588_team_inbox_queue_correctness
Revises: 587_field_request_requester_history
"""

import sqlalchemy as sa

from alembic import op

revision: str = "588_team_inbox_queue_correctness"
down_revision: str | None = "587_field_request_requester_history"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.add_column(
        "inbox_conversation_queue_entries",
        sa.Column(
            "admission_generation",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )
    op.add_column(
        "inbox_conversation_queue_entries",
        sa.Column("last_notified_position", sa.Integer(), nullable=True),
    )
    op.add_column(
        "inbox_conversation_queue_entries",
        sa.Column(
            "last_position_notified_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    op.add_column(
        "inbox_conversation_queue_entries",
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "inbox_queue_notifications",
        sa.Column(
            "admission_generation",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )
    op.add_column(
        "inbox_queue_notifications",
        sa.Column("suppression_reason", sa.String(length=80), nullable=True),
    )
    op.create_check_constraint(
        "ck_inbox_queue_admission_generation_positive",
        "inbox_conversation_queue_entries",
        "admission_generation > 0",
    )
    op.create_check_constraint(
        "ck_inbox_queue_notification_generation_positive",
        "inbox_queue_notifications",
        "admission_generation > 0",
    )
    op.create_index(
        "ix_inbox_queue_team_fifo",
        "inbox_conversation_queue_entries",
        ["service_team_id", "status", "entered_at", "queue_position"],
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.drop_index(
        "ix_inbox_queue_team_fifo", table_name="inbox_conversation_queue_entries"
    )
    op.drop_constraint(
        "ck_inbox_queue_admission_generation_positive",
        "inbox_conversation_queue_entries",
        type_="check",
    )
    op.drop_constraint(
        "ck_inbox_queue_notification_generation_positive",
        "inbox_queue_notifications",
        type_="check",
    )
    op.drop_column("inbox_queue_notifications", "suppression_reason")
    op.drop_column("inbox_queue_notifications", "admission_generation")
    op.drop_column("inbox_conversation_queue_entries", "last_heartbeat_at")
    op.drop_column("inbox_conversation_queue_entries", "last_position_notified_at")
    op.drop_column("inbox_conversation_queue_entries", "last_notified_position")
    op.drop_column("inbox_conversation_queue_entries", "admission_generation")
