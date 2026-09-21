"""Add native Inbox SLA policies, clocks, and audit evidence.

legacy external inbox SLA settings are not imported here.  The separate configuration importer
requires explicit mappings and is dry-run by default.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "586_inbox_sla_rules"
down_revision: str | None = "585_quote_payment_review"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "inbox_sla_policies",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column(
            "timezone", sa.String(64), nullable=False, server_default="Africa/Lagos"
        ),
        sa.Column(
            "working_days", sa.JSON(), nullable=False, server_default="[0,1,2,3,4]"
        ),
        sa.Column(
            "workday_start", sa.Time(), nullable=False, server_default="09:00:00"
        ),
        sa.Column("workday_end", sa.Time(), nullable=False, server_default="17:00:00"),
        sa.Column("holidays", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column(
            "is_default", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("source_reference", sa.String(160)),
        sa.Column("metadata", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_inbox_sla_policies_name"),
    )
    op.create_index(
        "ix_inbox_sla_policies_active_default",
        "inbox_sla_policies",
        ["is_active", "is_default"],
    )
    op.create_index(
        "uq_inbox_sla_policies_default_active",
        "inbox_sla_policies",
        ["is_default"],
        unique=True,
        postgresql_where=sa.text("is_default IS TRUE AND is_active IS TRUE"),
    )

    op.create_table(
        "inbox_sla_rules",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("policy_id", sa.UUID(), nullable=False),
        sa.Column("service_team_id", sa.UUID()),
        sa.Column("channel_type", sa.String(40)),
        sa.Column("priority", sa.Integer()),
        sa.Column("first_response_minutes", sa.Integer(), nullable=False),
        sa.Column("next_response_minutes", sa.Integer()),
        sa.Column("resolution_minutes", sa.Integer(), nullable=False),
        sa.Column("warning_minutes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("source_reference", sa.String(160)),
        sa.Column("metadata", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "first_response_minutes > 0", name="ck_inbox_sla_rules_first_positive"
        ),
        sa.CheckConstraint(
            "next_response_minutes IS NULL OR next_response_minutes > 0",
            name="ck_inbox_sla_rules_next_positive",
        ),
        sa.CheckConstraint(
            "resolution_minutes > 0", name="ck_inbox_sla_rules_resolution_positive"
        ),
        sa.CheckConstraint(
            "warning_minutes >= 0 AND warning_minutes < first_response_minutes",
            name="ck_inbox_sla_rules_warning_bounds",
        ),
        sa.ForeignKeyConstraint(
            ["policy_id"], ["inbox_sla_policies.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["service_team_id"], ["service_teams.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "policy_id",
            "service_team_id",
            "channel_type",
            "priority",
            name="uq_inbox_sla_rules_match",
        ),
    )
    op.create_index(
        "ix_inbox_sla_rules_match",
        "inbox_sla_rules",
        ["policy_id", "is_active", "service_team_id", "channel_type", "priority"],
    )

    op.create_table(
        "inbox_sla_clocks",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("conversation_id", sa.UUID(), nullable=False),
        sa.Column("policy_id", sa.UUID(), nullable=False),
        sa.Column("rule_id", sa.UUID(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_response_due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_response_at", sa.DateTime(timezone=True)),
        sa.Column("next_response_due_at", sa.DateTime(timezone=True)),
        sa.Column("next_response_at", sa.DateTime(timezone=True)),
        sa.Column("resolution_due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("paused_at", sa.DateTime(timezone=True)),
        sa.Column(
            "total_paused_seconds", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("status", sa.String(24), nullable=False, server_default="running"),
        sa.Column("warning_sent_at", sa.DateTime(timezone=True)),
        sa.Column("breach_notified_at", sa.DateTime(timezone=True)),
        sa.Column("last_evaluated_at", sa.DateTime(timezone=True)),
        sa.Column("metadata", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["inbox_conversations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["policy_id"], ["inbox_sla_policies.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["rule_id"], ["inbox_sla_rules.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("conversation_id", name="uq_inbox_sla_clocks_conversation"),
    )
    op.create_index(
        "ix_inbox_sla_clocks_due",
        "inbox_sla_clocks",
        ["status", "first_response_due_at", "resolution_due_at"],
    )
    op.create_index(
        "ix_inbox_sla_clocks_policy", "inbox_sla_clocks", ["policy_id", "status"]
    )

    op.create_table(
        "inbox_sla_events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("clock_id", sa.UUID(), nullable=False),
        sa.Column("conversation_id", sa.UUID(), nullable=False),
        sa.Column("event_key", sa.String(160), nullable=False),
        sa.Column("event_type", sa.String(48), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metadata", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["clock_id"], ["inbox_sla_clocks.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["inbox_conversations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("clock_id", "event_key", name="uq_inbox_sla_events_key"),
    )
    op.create_index(
        "ix_inbox_sla_events_conversation",
        "inbox_sla_events",
        ["conversation_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_inbox_sla_events_conversation", table_name="inbox_sla_events")
    op.drop_table("inbox_sla_events")
    op.drop_index("ix_inbox_sla_clocks_policy", table_name="inbox_sla_clocks")
    op.drop_index("ix_inbox_sla_clocks_due", table_name="inbox_sla_clocks")
    op.drop_table("inbox_sla_clocks")
    op.drop_index("ix_inbox_sla_rules_match", table_name="inbox_sla_rules")
    op.drop_table("inbox_sla_rules")
    op.drop_index(
        "uq_inbox_sla_policies_default_active", table_name="inbox_sla_policies"
    )
    op.drop_index(
        "ix_inbox_sla_policies_active_default", table_name="inbox_sla_policies"
    )
    op.drop_table("inbox_sla_policies")
