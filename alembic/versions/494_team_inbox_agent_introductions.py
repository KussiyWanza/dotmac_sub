"""add Team Inbox per-agent introduction preferences

Revision ID: 494_team_inbox_agent_introductions
Revises: 493_team_inbox_reply_reminders
Create Date: 2026-08-06
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "494_team_inbox_agent_introductions"
down_revision = "493_team_inbox_reply_reminders"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "inbox_agent_introduction_preferences",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("person_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("template", sa.Text(), nullable=False),
        sa.Column(
            "auto_send_chat_widget",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
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
        sa.UniqueConstraint("person_id", name="uq_inbox_agent_intro_person"),
    )


def downgrade() -> None:
    op.drop_table("inbox_agent_introduction_preferences")
