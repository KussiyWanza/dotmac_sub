"""Conversational AI intake sessions and queue notification evidence.

Revision ID: 528_conversational_ai_intake
Revises: 527_credential_party_binding_additive
Create Date: 2026-08-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "528_conversational_ai_intake"
down_revision: str | None = "527_credential_party_binding_additive"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_intake_policies",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("legacy_config_id", sa.Uuid(), nullable=True),
        sa.Column("scope_key", sa.String(length=160), nullable=False),
        sa.Column("channel_type", sa.String(length=40), nullable=False),
        sa.Column(
            "provider", sa.String(length=80), nullable=False, server_default="any"
        ),
        sa.Column(
            "account_scope", sa.String(length=160), nullable=False, server_default="any"
        ),
        sa.Column(
            "display_name",
            sa.String(length=120),
            nullable=False,
            server_default="Dotmac Virtual Assistant",
        ),
        sa.Column(
            "is_enabled", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("active_version_id", sa.Uuid(), nullable=True),
        sa.Column("fallback_team_id", sa.Uuid(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["legacy_config_id"], ["ai_intake_configs.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint(
            "scope_key",
            "channel_type",
            "provider",
            "account_scope",
            name="uq_ai_intake_policies_scope_channel_provider",
        ),
    )
    op.create_index(
        "ix_ai_intake_policies_active",
        "ai_intake_policies",
        ["is_enabled", "channel_type"],
    )

    op.create_table(
        "ai_intake_policy_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("policy_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=24),
            nullable=False,
            server_default="draft",
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("activated_by_person_id", sa.Uuid(), nullable=True),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_by_version_id", sa.Uuid(), nullable=True),
        sa.Column(
            "display_name",
            sa.String(length=120),
            nullable=False,
            server_default="Dotmac Virtual Assistant",
        ),
        sa.Column("welcome_message", sa.Text(), nullable=False),
        sa.Column("business_tone", sa.Text(), nullable=True),
        sa.Column("business_instructions", sa.Text(), nullable=True),
        sa.Column("approved_isp_information", sa.Text(), nullable=True),
        sa.Column(
            "protected_system_instructions_version",
            sa.String(length=40),
            nullable=False,
            server_default="2026-08-12",
        ),
        sa.Column("intent_definitions", sa.JSON(), nullable=True),
        sa.Column("clarification_questions", sa.JSON(), nullable=True),
        sa.Column("intent_team_mappings", sa.JSON(), nullable=True),
        sa.Column("queue_templates", sa.JSON(), nullable=True),
        sa.Column("escalation_rules", sa.JSON(), nullable=True),
        sa.Column("data_cleanup_policy", sa.JSON(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("created_by_person_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["policy_id"], ["ai_intake_policies.id"], ondelete="CASCADE"
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'activated', 'superseded')",
            name="ck_ai_intake_policy_versions_status",
        ),
        sa.UniqueConstraint(
            "policy_id",
            "version_number",
            name="uq_ai_intake_policy_versions_policy_number",
        ),
    )
    op.create_index(
        "ix_ai_intake_policy_versions_policy",
        "ai_intake_policy_versions",
        ["policy_id", "created_at"],
    )

    op.create_table(
        "ai_intake_sessions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("policy_id", sa.Uuid(), nullable=True),
        sa.Column("policy_version_id", sa.Uuid(), nullable=True),
        sa.Column("legacy_config_id", sa.Uuid(), nullable=True),
        sa.Column("state", sa.String(length=40), nullable=False),
        sa.Column("channel_type", sa.String(length=40), nullable=False),
        sa.Column("provider", sa.String(length=80), nullable=False),
        sa.Column("account_scope", sa.String(length=160), nullable=False),
        sa.Column(
            "display_name",
            sa.String(length=120),
            nullable=False,
            server_default="Dotmac Virtual Assistant",
        ),
        sa.Column("turn_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_turns", sa.Integer(), nullable=False, server_default="2"),
        sa.Column(
            "confidence_threshold", sa.Float(), nullable=False, server_default="0.75"
        ),
        sa.Column("fallback_team_id", sa.Uuid(), nullable=True),
        sa.Column("final_intent", sa.String(length=120), nullable=True),
        sa.Column("final_category", sa.String(length=120), nullable=True),
        sa.Column("final_confidence", sa.Float(), nullable=True),
        sa.Column("handoff_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("takeover_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "state IN ('eligible', 'welcome_pending', 'collecting_intent', "
            "'awaiting_customer', 'classified', 'handoff_requested', 'completed', "
            "'stopped_human_takeover', 'fallback_escalated', 'expired', 'failed', "
            "'ineligible')",
            name="ck_ai_intake_sessions_state",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["inbox_conversations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["legacy_config_id"], ["ai_intake_configs.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["policy_id"], ["ai_intake_policies.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["policy_version_id"], ["ai_intake_policy_versions.id"], ondelete="SET NULL"
        ),
    )
    op.create_index(
        "uq_ai_intake_sessions_active_conversation",
        "ai_intake_sessions",
        ["conversation_id"],
        unique=True,
        postgresql_where=sa.text("completed_at IS NULL"),
        sqlite_where=sa.text("completed_at IS NULL"),
    )
    op.create_index(
        "ix_ai_intake_sessions_state", "ai_intake_sessions", ["state", "expires_at"]
    )

    op.create_table(
        "ai_intake_generation_attempts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("inbound_message_id", sa.Uuid(), nullable=True),
        sa.Column("outbound_message_id", sa.Uuid(), nullable=True),
        sa.Column("turn_number", sa.Integer(), nullable=False),
        sa.Column("message_purpose", sa.String(length=40), nullable=False),
        sa.Column("provider", sa.String(length=80), nullable=True),
        sa.Column("model", sa.String(length=160), nullable=True),
        sa.Column("provider_request_id", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("error_code", sa.String(length=120), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["session_id"], ["ai_intake_sessions.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_ai_intake_generation_attempt_idempotency"
        ),
    )
    op.create_index(
        "ix_ai_intake_generation_attempts_session",
        "ai_intake_generation_attempts",
        ["session_id", "created_at"],
    )

    op.create_table(
        "inbox_team_round_robin_cursors",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("service_team_id", sa.Uuid(), nullable=False),
        sa.Column("last_assigned_person_id", sa.Uuid(), nullable=True),
        sa.Column("rotation_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["service_team_id"], ["service_teams.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("service_team_id", name="uq_inbox_rr_cursor_team"),
    )
    op.create_index(
        "ix_inbox_rr_cursor_updated", "inbox_team_round_robin_cursors", ["updated_at"]
    )

    op.create_table(
        "inbox_queue_notifications",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("queue_entry_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("notification_kind", sa.String(length=40), nullable=False),
        sa.Column("queue_position", sa.Integer(), nullable=True),
        sa.Column("outbound_message_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("dedupe_key", sa.String(length=255), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["inbox_conversations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["queue_entry_id"],
            ["inbox_conversation_queue_entries.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("dedupe_key", name="uq_inbox_queue_notification_dedupe"),
    )
    op.create_index(
        "ix_inbox_queue_notifications_entry",
        "inbox_queue_notifications",
        ["queue_entry_id", "sent_at"],
    )
    op.create_index(
        "ix_inbox_queue_notifications_due",
        "inbox_queue_notifications",
        ["status", "next_due_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_inbox_queue_notifications_due", table_name="inbox_queue_notifications"
    )
    op.drop_index(
        "ix_inbox_queue_notifications_entry", table_name="inbox_queue_notifications"
    )
    op.drop_table("inbox_queue_notifications")
    op.drop_index(
        "ix_inbox_rr_cursor_updated", table_name="inbox_team_round_robin_cursors"
    )
    op.drop_table("inbox_team_round_robin_cursors")
    op.drop_index(
        "ix_ai_intake_generation_attempts_session",
        table_name="ai_intake_generation_attempts",
    )
    op.drop_table("ai_intake_generation_attempts")
    op.drop_index("ix_ai_intake_sessions_state", table_name="ai_intake_sessions")
    op.drop_index(
        "uq_ai_intake_sessions_active_conversation", table_name="ai_intake_sessions"
    )
    op.drop_table("ai_intake_sessions")
    op.drop_index(
        "ix_ai_intake_policy_versions_policy", table_name="ai_intake_policy_versions"
    )
    op.drop_table("ai_intake_policy_versions")
    op.drop_index("ix_ai_intake_policies_active", table_name="ai_intake_policies")
    op.drop_table("ai_intake_policies")
