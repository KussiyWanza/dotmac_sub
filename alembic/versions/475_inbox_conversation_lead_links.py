"""add durable Inbox conversation-to-Lead provenance

Revision ID: 475_inbox_conversation_lead_links
Revises: 474_lifecycle_evidence_authority
Create Date: 2026-08-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "475_inbox_conversation_lead_links"
down_revision: str | None = "474_lifecycle_evidence_authority"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_lead_origin_captures_platform_source",
        "lead_origin_captures",
        type_="check",
    )
    op.create_check_constraint(
        "ck_lead_origin_captures_platform_source",
        "lead_origin_captures",
        "(source_platform <> 'meta' OR lead_source IN "
        "('Facebook Ads', 'Instagram Ads')) AND "
        "(source_platform <> 'google' OR lead_source = 'Google') AND "
        "(source_platform <> 'website' OR lead_source = 'Website') AND "
        "(source_platform <> 'portal' OR lead_source = 'Portal') AND "
        "(source_platform <> 'referral' OR lead_source = 'Referrer') AND "
        "(source_platform <> 'team_inbox' OR lead_source IN "
        "('Whatsapp', 'Facebook', 'Instagram', 'Email', 'Website'))",
    )
    op.create_table(
        "inbox_conversation_lead_links",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("lead_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("party_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("link_source", sa.String(80), nullable=False),
        sa.Column("link_reason", sa.Text(), nullable=False),
        sa.Column("linked_by_person_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("command_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column(
            "linked_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("deactivated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deactivation_reason", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "(is_active IS TRUE AND deactivated_at IS NULL) OR "
            "(is_active IS FALSE AND deactivated_at IS NOT NULL)",
            name="ck_inbox_conversation_lead_links_active_evidence",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["inbox_conversations.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["party_id"], ["parties.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "command_id", name="uq_inbox_conversation_lead_links_command"
        ),
    )
    op.create_index(
        "uq_inbox_conversation_lead_links_active_conversation",
        "inbox_conversation_lead_links",
        ["conversation_id"],
        unique=True,
        postgresql_where=sa.text("is_active IS TRUE"),
        sqlite_where=sa.text("is_active IS TRUE"),
    )
    op.create_index(
        "ix_inbox_conversation_lead_links_lead_active",
        "inbox_conversation_lead_links",
        ["lead_id", "is_active"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_inbox_conversation_lead_links_lead_active",
        table_name="inbox_conversation_lead_links",
    )
    op.drop_index(
        "uq_inbox_conversation_lead_links_active_conversation",
        table_name="inbox_conversation_lead_links",
    )
    op.drop_table("inbox_conversation_lead_links")
    op.drop_constraint(
        "ck_lead_origin_captures_platform_source",
        "lead_origin_captures",
        type_="check",
    )
    op.create_check_constraint(
        "ck_lead_origin_captures_platform_source",
        "lead_origin_captures",
        "(source_platform <> 'meta' OR lead_source IN "
        "('Facebook Ads', 'Instagram Ads')) AND "
        "(source_platform <> 'google' OR lead_source = 'Google') AND "
        "(source_platform <> 'website' OR lead_source = 'Website') AND "
        "(source_platform <> 'portal' OR lead_source = 'Portal') AND "
        "(source_platform <> 'referral' OR lead_source = 'Referrer') AND "
        "(source_platform <> 'team_inbox' OR lead_source IN "
        "('Whatsapp', 'Facebook', 'Instagram'))",
    )
