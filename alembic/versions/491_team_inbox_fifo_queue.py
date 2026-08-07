"""add durable Team Inbox FIFO queue entries

Revision ID: 491_team_inbox_fifo_queue
Revises: 490_ont_reconcile_positive_admission
Create Date: 2026-08-06
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "491_team_inbox_fifo_queue"
down_revision = "490_ont_reconcile_positive_admission"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "inbox_conversation_queue_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("inbox_conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "service_team_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("service_teams.id"),
            nullable=False,
        ),
        sa.Column("queue_position", sa.Integer(), nullable=False),
        sa.Column(
            "status", sa.String(length=24), nullable=False, server_default="queued"
        ),
        sa.Column(
            "entered_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("settled_at", sa.DateTime(timezone=True)),
        sa.Column("metadata", sa.JSON()),
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
        sa.CheckConstraint(
            "queue_position > 0", name="ck_inbox_queue_position_positive"
        ),
        sa.UniqueConstraint(
            "conversation_id", name="uq_inbox_queue_entry_conversation"
        ),
        sa.UniqueConstraint(
            "service_team_id",
            "queue_position",
            name="uq_inbox_queue_team_position",
        ),
    )
    op.create_index(
        "ix_inbox_queue_team_status_position",
        "inbox_conversation_queue_entries",
        ["service_team_id", "status", "queue_position"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_inbox_queue_team_status_position",
        table_name="inbox_conversation_queue_entries",
    )
    op.drop_table("inbox_conversation_queue_entries")
