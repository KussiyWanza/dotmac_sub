"""add durable Team Inbox reply reminders

Revision ID: 493_team_inbox_reply_reminders
Revises: 492_team_inbox_automation_rules
Create Date: 2026-08-06
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "493_team_inbox_reply_reminders"
down_revision = "492_team_inbox_automation_rules"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "inbox_reply_reminders",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "assignment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("inbox_conversation_assignments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("inbox_conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("person_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("waiting_since", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_sent_at", sa.DateTime(timezone=True)),
        sa.Column("sent_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
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
        sa.UniqueConstraint("assignment_id", name="uq_inbox_reply_reminder_assignment"),
    )
    op.create_index(
        "ix_inbox_reply_reminders_due",
        "inbox_reply_reminders",
        ["is_active", "next_due_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_inbox_reply_reminders_due", table_name="inbox_reply_reminders")
    op.drop_table("inbox_reply_reminders")
