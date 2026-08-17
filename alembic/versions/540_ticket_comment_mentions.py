"""Persist explicit support-ticket comment mention targets.

Revision ID: 540_ticket_comment_mentions
Revises: 539_active_sub_billing_anchor
Create Date: 2026-08-17

Legacy comment text is deliberately not backfilled. Display labels are mutable
and non-unique, so parsing ``@label`` text would invent authoritative staff or
team identity. Existing text remains unchanged; new selections become durable
through the lifecycle owner after this migration.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "540_ticket_comment_mentions"
down_revision = "539_active_sub_billing_anchor"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "support_ticket_comment_mentions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("comment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("system_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("service_team_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "(system_user_id IS NOT NULL) <> (service_team_id IS NOT NULL)",
            name="ck_ticket_comment_mention_exact_target",
        ),
        sa.ForeignKeyConstraint(
            ["comment_id"],
            ["support_ticket_comments.id"],
            name="fk_ticket_comment_mentions_comment",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["system_user_id"],
            ["system_users.id"],
            name="fk_ticket_comment_mentions_system_user",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["service_team_id"],
            ["service_teams.id"],
            name="fk_ticket_comment_mentions_service_team",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "comment_id",
            "system_user_id",
            name="uq_ticket_comment_mention_user",
        ),
        sa.UniqueConstraint(
            "comment_id",
            "service_team_id",
            name="uq_ticket_comment_mention_team",
        ),
    )
    op.create_index(
        "ix_ticket_comment_mentions_comment",
        "support_ticket_comment_mentions",
        ["comment_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ticket_comment_mentions_comment",
        table_name="support_ticket_comment_mentions",
    )
    op.drop_table("support_ticket_comment_mentions")
