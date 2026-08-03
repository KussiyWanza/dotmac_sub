"""Team Inbox channel and AI routing rules."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "466_team_inbox_channel_ai_routes"
down_revision = "465_ont_reconcile_eligibility_holds"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "team_inbox_channel_routes",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("service_team_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("channel_type", sa.String(length=40), nullable=False),
        sa.Column("provider", sa.String(length=80), nullable=False),
        sa.Column("account_scope", sa.String(length=160), nullable=False),
        sa.Column("display_name", sa.String(length=160), nullable=True),
        sa.Column(
            "allow_ai_routing",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "priority",
            sa.Integer(),
            server_default=sa.text("100"),
            nullable=False,
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["service_team_id"], ["service_teams.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "channel_type",
            "provider",
            "account_scope",
            name="uq_team_inbox_channel_routes_identity",
        ),
    )
    op.create_index(
        "ix_team_inbox_channel_routes_channel_active",
        "team_inbox_channel_routes",
        ["channel_type", "is_active"],
    )
    op.create_index(
        "ix_team_inbox_channel_routes_team",
        "team_inbox_channel_routes",
        ["service_team_id"],
    )

    op.create_table(
        "team_inbox_ai_routes",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("service_team_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("channel_type", sa.String(length=40), nullable=False),
        sa.Column("intent_key", sa.String(length=120), nullable=False),
        sa.Column("display_name", sa.String(length=160), nullable=True),
        sa.Column(
            "confidence_threshold",
            sa.Float(),
            server_default=sa.text("0.75"),
            nullable=False,
        ),
        sa.Column(
            "priority",
            sa.Integer(),
            server_default=sa.text("100"),
            nullable=False,
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["service_team_id"], ["service_teams.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "channel_type",
            "intent_key",
            name="uq_team_inbox_ai_routes_channel_intent",
        ),
    )
    op.create_index(
        "ix_team_inbox_ai_routes_active",
        "team_inbox_ai_routes",
        ["is_active", "priority"],
    )
    op.create_index(
        "ix_team_inbox_ai_routes_team",
        "team_inbox_ai_routes",
        ["service_team_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_team_inbox_ai_routes_team", table_name="team_inbox_ai_routes")
    op.drop_index("ix_team_inbox_ai_routes_active", table_name="team_inbox_ai_routes")
    op.drop_table("team_inbox_ai_routes")
    op.drop_index(
        "ix_team_inbox_channel_routes_team",
        table_name="team_inbox_channel_routes",
    )
    op.drop_index(
        "ix_team_inbox_channel_routes_channel_active",
        table_name="team_inbox_channel_routes",
    )
    op.drop_table("team_inbox_channel_routes")
