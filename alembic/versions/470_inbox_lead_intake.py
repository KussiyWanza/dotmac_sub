"""Add sales-owned Inbox lead intake forms and invitations.

Revision ID: 470_inbox_lead_intake
Revises: 469_meta_direct_message_channels
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "470_inbox_lead_intake"
down_revision: str | None = "469_meta_direct_message_channels"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _replace_lead_origin_constraints(*, expanded: bool) -> None:
    for name in (
        "ck_lead_origin_captures_method",
        "ck_lead_origin_captures_platform",
        "ck_lead_origin_captures_method_platform",
        "ck_lead_origin_captures_platform_source",
    ):
        op.drop_constraint(name, "lead_origin_captures", type_="check")
    inbox_method = ", 'inbox_form'" if expanded else ""
    inbox_platform = ", 'team_inbox'" if expanded else ""
    op.create_check_constraint(
        "ck_lead_origin_captures_method",
        "lead_origin_captures",
        "capture_method IN ('ad_lead_form_webhook', 'landing_page', 'portal', "
        "'agent_declared', 'campaign_response', 'referral', 'reviewed_import'"
        + inbox_method
        + ")",
    )
    op.create_check_constraint(
        "ck_lead_origin_captures_platform",
        "lead_origin_captures",
        "source_platform IN ('meta', 'google', 'website', 'portal', 'agent', "
        "'referral', 'sub_campaign', 'legacy_import'" + inbox_platform + ")",
    )
    method_platform = (
        "(capture_method <> 'landing_page' OR source_platform = 'website') AND "
        "(capture_method <> 'portal' OR source_platform = 'portal') AND "
        "(capture_method <> 'agent_declared' OR source_platform = 'agent') AND "
        "(capture_method <> 'referral' OR source_platform = 'referral') AND "
        "(capture_method <> 'reviewed_import' OR source_platform = 'legacy_import')"
    )
    platform_source = (
        "(source_platform <> 'meta' OR lead_source IN "
        "('Facebook Ads', 'Instagram Ads')) AND "
        "(source_platform <> 'google' OR lead_source = 'Google') AND "
        "(source_platform <> 'website' OR lead_source = 'Website') AND "
        "(source_platform <> 'portal' OR lead_source = 'Portal') AND "
        "(source_platform <> 'referral' OR lead_source = 'Referrer')"
    )
    if expanded:
        method_platform += (
            " AND (capture_method <> 'inbox_form' OR source_platform = 'team_inbox')"
        )
        platform_source += (
            " AND (source_platform <> 'team_inbox' OR lead_source IN "
            "('Whatsapp', 'Facebook', 'Instagram'))"
        )
    op.create_check_constraint(
        "ck_lead_origin_captures_method_platform",
        "lead_origin_captures",
        method_platform,
    )
    op.create_check_constraint(
        "ck_lead_origin_captures_platform_source",
        "lead_origin_captures",
        platform_source,
    )


def upgrade() -> None:
    _replace_lead_origin_constraints(expanded=True)
    op.create_table(
        "lead_intake_templates",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("party_type", sa.String(24), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("heading", sa.String(200), nullable=False),
        sa.Column("introduction", sa.Text()),
        sa.Column("privacy_notice", sa.Text(), nullable=False),
        sa.Column("invitation_message", sa.Text(), nullable=False),
        sa.Column("confirmation_message", sa.Text(), nullable=False),
        sa.Column("thank_you_message", sa.Text(), nullable=False),
        sa.Column(
            "target_service_team_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("owner_system_user_id", postgresql.UUID(as_uuid=True)),
        sa.Column("pipeline_id", postgresql.UUID(as_uuid=True)),
        sa.Column("stage_id", postgresql.UUID(as_uuid=True)),
        sa.Column(
            "created_by_system_user_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("retired_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "party_type IN ('individual', 'organization')",
            name="ck_lead_intake_templates_party_type",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'published', 'retired')",
            name="ck_lead_intake_templates_status",
        ),
        sa.CheckConstraint("version > 0", name="ck_lead_intake_templates_version"),
        sa.ForeignKeyConstraint(["target_service_team_id"], ["service_teams.id"]),
        sa.ForeignKeyConstraint(["owner_system_user_id"], ["system_users.id"]),
        sa.ForeignKeyConstraint(["created_by_system_user_id"], ["system_users.id"]),
        sa.ForeignKeyConstraint(["pipeline_id"], ["pipelines.id"]),
        sa.ForeignKeyConstraint(["stage_id"], ["pipeline_stages.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "party_type", "version", name="uq_lead_intake_templates_type_version"
        ),
    )
    op.create_index(
        "uq_lead_intake_templates_published_type",
        "lead_intake_templates",
        ["party_type"],
        unique=True,
        postgresql_where=sa.text("status = 'published'"),
    )
    op.create_table(
        "lead_intake_assessments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("intent_key", sa.String(80), nullable=False),
        sa.Column("intent_confidence", sa.Float(), nullable=False),
        sa.Column("party_type", sa.String(24), nullable=False),
        sa.Column("party_type_confidence", sa.Float(), nullable=False),
        sa.Column("decision", sa.String(32), nullable=False),
        sa.Column("provider_label", sa.String(80)),
        sa.Column("model_label", sa.String(160)),
        sa.Column("clarification_question", sa.String(240)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "party_type IN ('individual', 'organization', 'unknown')",
            name="ck_lead_intake_assessments_party_type",
        ),
        sa.CheckConstraint(
            "decision IN ('not_eligible', 'clarification_required', 'invite_issued', 'staff_review', 'provider_failed')",
            name="ck_lead_intake_assessments_decision",
        ),
        sa.CheckConstraint(
            "intent_confidence >= 0 AND intent_confidence <= 1 AND party_type_confidence >= 0 AND party_type_confidence <= 1",
            name="ck_lead_intake_assessments_confidence",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["inbox_conversations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["message_id"], ["inbox_messages.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("message_id", name="uq_lead_intake_assessments_message"),
    )
    op.create_index(
        "ix_lead_intake_assessments_conversation",
        "lead_intake_assessments",
        ["conversation_id", "created_at"],
    )
    op.create_table(
        "lead_intake_invitations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("template_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("assessment_id", postgresql.UUID(as_uuid=True)),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("trigger_message_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("auto_issued", sa.Boolean(), nullable=False),
        sa.Column("channel_type", sa.String(40), nullable=False),
        sa.Column("provider", sa.String(80), nullable=False),
        sa.Column("provider_account_scope", sa.String(200), nullable=False),
        sa.Column("normalized_endpoint", sa.String(320), nullable=False),
        sa.Column("intent_key", sa.String(80)),
        sa.Column("intent_confidence", sa.Float()),
        sa.Column("party_type_confidence", sa.Float()),
        sa.Column("issued_by_system_user_id", postgresql.UUID(as_uuid=True)),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_reason", sa.String(240)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("outbound_message_id", postgresql.UUID(as_uuid=True)),
        sa.Column("delivery_status", sa.String(40)),
        sa.Column("delivery_error_code", sa.String(120)),
        sa.Column("lead_id", postgresql.UUID(as_uuid=True)),
        sa.Column("party_id", postgresql.UUID(as_uuid=True)),
        sa.Column("representative_party_id", postgresql.UUID(as_uuid=True)),
        sa.Column("party_contact_point_id", postgresql.UUID(as_uuid=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('issued', 'completed', 'expired', 'revoked')",
            name="ck_lead_intake_invitations_status",
        ),
        sa.CheckConstraint(
            "channel_type IN ('whatsapp', 'facebook_messenger', 'instagram_dm')",
            name="ck_lead_intake_invitations_channel",
        ),
        sa.CheckConstraint(
            "expires_at > issued_at", name="ck_lead_intake_invitations_expiry"
        ),
        sa.CheckConstraint(
            "(status = 'completed' AND completed_at IS NOT NULL AND lead_id IS NOT NULL AND party_id IS NOT NULL) OR (status <> 'completed' AND completed_at IS NULL)",
            name="ck_lead_intake_invitations_completion",
        ),
        sa.ForeignKeyConstraint(["template_id"], ["lead_intake_templates.id"]),
        sa.ForeignKeyConstraint(["assessment_id"], ["lead_intake_assessments.id"]),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["inbox_conversations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["trigger_message_id"], ["inbox_messages.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["outbound_message_id"], ["inbox_messages.id"]),
        sa.ForeignKeyConstraint(["issued_by_system_user_id"], ["system_users.id"]),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["party_id"], ["parties.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["representative_party_id"], ["parties.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["party_contact_point_id"], ["party_contact_points.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_lead_intake_invitations_token_hash"),
    )
    op.create_index(
        "uq_lead_intake_invitations_auto_conversation",
        "lead_intake_invitations",
        ["conversation_id"],
        unique=True,
        postgresql_where=sa.text("auto_issued IS TRUE"),
    )
    op.create_index(
        "ix_lead_intake_invitations_conversation",
        "lead_intake_invitations",
        ["conversation_id", "issued_at"],
    )
    op.create_index(
        "ix_lead_intake_invitations_lead", "lead_intake_invitations", ["lead_id"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_lead_intake_invitations_lead", table_name="lead_intake_invitations"
    )
    op.drop_index(
        "ix_lead_intake_invitations_conversation", table_name="lead_intake_invitations"
    )
    op.drop_index(
        "uq_lead_intake_invitations_auto_conversation",
        table_name="lead_intake_invitations",
    )
    op.drop_table("lead_intake_invitations")
    op.drop_index(
        "ix_lead_intake_assessments_conversation", table_name="lead_intake_assessments"
    )
    op.drop_table("lead_intake_assessments")
    op.drop_index(
        "uq_lead_intake_templates_published_type", table_name="lead_intake_templates"
    )
    op.drop_table("lead_intake_templates")
    _replace_lead_origin_constraints(expanded=False)
