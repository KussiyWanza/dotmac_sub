"""Scope Team Inbox and Party social identities to provider accounts.

Revision ID: 595_team_inbox_provider_identity_scope
Revises: 594_inbox_customer_completion_policy
Create Date: 2026-09-14

The migration is additive apart from replacing the unsafe active Inbox-link
index. It backfills scope only from an already reviewed PartyContactPoint
binding; unreviewed legacy social links remain explicitly unscoped and are not
eligible for automatic provider-identity resolution.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "595_team_inbox_provider_identity_scope"
down_revision = "594_inbox_customer_completion_policy"
branch_labels = None
depends_on = None

_SOCIAL = "('facebook_messenger', 'instagram_dm')"


def upgrade() -> None:
    op.add_column(
        "inbox_contact_links",
        sa.Column("provider", sa.String(length=80), nullable=True),
    )
    op.add_column(
        "inbox_contact_links",
        sa.Column("provider_account_id", sa.String(length=200), nullable=True),
    )
    op.add_column(
        "inbox_contact_links",
        sa.Column("external_subject_id", sa.String(length=200), nullable=True),
    )
    op.execute(
        sa.text(
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
    )
    op.create_check_constraint(
        "ck_inbox_contact_links_provider_identity_scope",
        "inbox_contact_links",
        f"channel_type NOT IN {_SOCIAL} OR "
        "((provider IS NULL AND provider_account_id IS NULL AND "
        "external_subject_id IS NULL) OR "
        "(provider IS NOT NULL AND provider_account_id IS NOT NULL AND "
        "external_subject_id IS NOT NULL))",
    )
    op.drop_index(
        "uq_inbox_contact_links_active_contact",
        table_name="inbox_contact_links",
    )
    op.create_index(
        "uq_inbox_contact_links_active_unscoped_contact",
        "inbox_contact_links",
        ["channel_type", "normalized_contact"],
        unique=True,
        postgresql_where=sa.text(
            f"is_active IS TRUE AND channel_type NOT IN {_SOCIAL}"
        ),
    )
    op.create_index(
        "uq_inbox_contact_links_active_provider_identity",
        "inbox_contact_links",
        ["channel_type", "provider", "provider_account_id", "external_subject_id"],
        unique=True,
        postgresql_where=sa.text(
            f"is_active IS TRUE AND channel_type IN {_SOCIAL} "
            "AND provider IS NOT NULL AND provider_account_id IS NOT NULL "
            "AND external_subject_id IS NOT NULL"
        ),
    )
    op.create_index(
        "uq_party_contact_points_active_provider_identity",
        "party_contact_points",
        ["channel_type", "provider", "provider_account_id", "external_subject_id"],
        unique=True,
        postgresql_where=sa.text(
            f"is_active IS TRUE AND channel_type IN {_SOCIAL} "
            "AND provider IS NOT NULL AND provider_account_id IS NOT NULL "
            "AND external_subject_id IS NOT NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_party_contact_points_active_provider_identity",
        table_name="party_contact_points",
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
