"""add Team Inbox automation rules

Revision ID: 492_team_inbox_automation_rules
Revises: 491_team_inbox_fifo_queue
Create Date: 2026-08-06
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "492_team_inbox_automation_rules"
down_revision = "491_team_inbox_fifo_queue"
branch_labels = None
depends_on = None


def upgrade() -> None:
    trigger = postgresql.ENUM(
        "conversation_created",
        "inbound_message_received",
        name="inboxautomationtrigger",
        create_type=False,
    )
    action = postgresql.ENUM(
        "assign_agent",
        "auto_assign",
        "add_tag",
        name="inboxautomationactiontype",
        create_type=False,
    )
    trigger.create(op.get_bind(), checkfirst=True)
    action.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "inbox_automation_rules",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("trigger", trigger, nullable=False),
        sa.Column("conditions", sa.JSON(), nullable=False),
        sa.Column("action_type", action, nullable=False),
        sa.Column("action_value", sa.JSON(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_executed_at", sa.DateTime(timezone=True)),
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
    )
    op.create_index(
        "ix_inbox_automation_trigger_active",
        "inbox_automation_rules",
        ["trigger", "is_active"],
    )
    op.create_index(
        "ix_inbox_automation_sort",
        "inbox_automation_rules",
        ["sort_order", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_inbox_automation_sort", table_name="inbox_automation_rules")
    op.drop_index(
        "ix_inbox_automation_trigger_active", table_name="inbox_automation_rules"
    )
    op.drop_table("inbox_automation_rules")
    sa.Enum(name="inboxautomationactiontype").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="inboxautomationtrigger").drop(op.get_bind(), checkfirst=True)
