"""Scope Inbox identities and audit expired conversation resolution.

Revision ID: 607_inbox_lead_identity_expiry
Revises: 606_project_task_subtasks
Create Date: 2026-09-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "607_inbox_lead_identity_expiry"
down_revision: str | None = "606_project_task_subtasks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_SOCIAL_CHANNELS = "('facebook_messenger', 'instagram_dm')"


def upgrade() -> None:
    op.add_column("inbox_contact_links", sa.Column("provider", sa.String(length=80)))
    op.add_column(
        "inbox_contact_links",
        sa.Column("provider_account_id", sa.String(length=200)),
    )
    op.add_column(
        "inbox_contact_links",
        sa.Column("external_subject_id", sa.String(length=200)),
    )
    op.execute(
        """
        UPDATE inbox_contact_links AS link
        SET provider = point.provider,
            provider_account_id = point.provider_account_id,
            external_subject_id = point.external_subject_id
        FROM party_contact_points AS point
        WHERE link.party_contact_point_id = point.id
          AND link.channel_type IN ('facebook_messenger', 'instagram_dm')
          AND point.provider IS NOT NULL
          AND point.provider_account_id IS NOT NULL
          AND point.external_subject_id IS NOT NULL
        """
    )
    op.create_check_constraint(
        "ck_inbox_contact_links_provider_identity_scope",
        "inbox_contact_links",
        "channel_type NOT IN ('facebook_messenger', 'instagram_dm') OR "
        "((provider IS NULL AND provider_account_id IS NULL AND "
        "external_subject_id IS NULL) OR "
        "(provider IS NOT NULL AND provider_account_id IS NOT NULL AND "
        "external_subject_id IS NOT NULL))",
    )
    op.drop_index(
        "uq_inbox_contact_links_active_contact", table_name="inbox_contact_links"
    )
    op.create_index(
        "uq_inbox_contact_links_active_unscoped_contact",
        "inbox_contact_links",
        ["channel_type", "normalized_contact"],
        unique=True,
        postgresql_where=sa.text(
            f"is_active IS TRUE AND channel_type NOT IN {_SOCIAL_CHANNELS}"
        ),
    )
    op.create_index(
        "uq_inbox_contact_links_active_provider_identity",
        "inbox_contact_links",
        [
            "channel_type",
            "provider",
            "provider_account_id",
            "external_subject_id",
        ],
        unique=True,
        postgresql_where=sa.text(
            f"is_active IS TRUE AND channel_type IN {_SOCIAL_CHANNELS} "
            "AND provider IS NOT NULL AND provider_account_id IS NOT NULL "
            "AND external_subject_id IS NOT NULL"
        ),
    )
    op.create_index(
        "uq_inbox_contact_links_active_legacy_social_contact",
        "inbox_contact_links",
        ["channel_type", "normalized_contact"],
        unique=True,
        postgresql_where=sa.text(
            f"is_active IS TRUE AND channel_type IN {_SOCIAL_CHANNELS} "
            "AND provider IS NULL AND provider_account_id IS NULL "
            "AND external_subject_id IS NULL"
        ),
    )

    op.add_column(
        "inbox_status_transition_events",
        sa.Column("resolution_reason", sa.String(length=80)),
    )
    op.add_column(
        "inbox_status_transition_events",
        sa.Column("channel_state_at_resolution", sa.String(length=40)),
    )
    op.create_check_constraint(
        "ck_inbox_status_event_resolution_reason",
        "inbox_status_transition_events",
        "resolution_reason IS NULL OR resolution_reason IN ("
        "'customer_stopped_responding', 'whatsapp_window_expired', "
        "'issue_completed_before_expiry', 'duplicate_conversation', "
        "'no_further_action_required', 'spam_irrelevant', 'other')",
    )
    op.create_check_constraint(
        "ck_inbox_status_event_resolution_channel_state",
        "inbox_status_transition_events",
        "channel_state_at_resolution IS NULL OR "
        "channel_state_at_resolution IN ("
        "'active_window', 'expired', 'unavailable', 'not_applicable')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_inbox_status_event_resolution_channel_state",
        "inbox_status_transition_events",
        type_="check",
    )
    op.drop_constraint(
        "ck_inbox_status_event_resolution_reason",
        "inbox_status_transition_events",
        type_="check",
    )
    op.drop_column("inbox_status_transition_events", "channel_state_at_resolution")
    op.drop_column("inbox_status_transition_events", "resolution_reason")

    op.drop_index(
        "uq_inbox_contact_links_active_legacy_social_contact",
        table_name="inbox_contact_links",
    )
    op.drop_index(
        "uq_inbox_contact_links_active_provider_identity",
        table_name="inbox_contact_links",
    )
    op.drop_index(
        "uq_inbox_contact_links_active_unscoped_contact",
        table_name="inbox_contact_links",
    )
    op.create_index(
        "uq_inbox_contact_links_active_contact",
        "inbox_contact_links",
        ["channel_type", "normalized_contact"],
        unique=True,
        postgresql_where=sa.text("is_active IS TRUE"),
    )
    op.drop_constraint(
        "ck_inbox_contact_links_provider_identity_scope",
        "inbox_contact_links",
        type_="check",
    )
    op.drop_column("inbox_contact_links", "external_subject_id")
    op.drop_column("inbox_contact_links", "provider_account_id")
    op.drop_column("inbox_contact_links", "provider")
