"""Give a conversation participants, endpoint-first, as a shadow projection.

A conversation carried a single `contact_address`, so the internal side of a
thread was a set (teams, assignments) and the customer side was a scalar.
Nothing could answer "is this sender part of this thread?" or "who may receive
this transcript?".

The Party binding is nullable on purpose. Inbox owns the fact that an endpoint
participated; Party owns who that endpoint belongs to. A mandatory FK would
leave an unknown colleague, a new vendor or an unreviewed address
unrepresentable, which is the problem this table removes. Same shape as
`inbox_contact_links`, and consistent with the shadow-only Party position in
docs/PARTY_CONTACT_INBOX_PROJECTION.md.

Additive only: no backfill here, no reader changes. The projection is
populated by `communications.team_inbox_participants` and rebuilt from stored
message headers by its maintenance command.

Revision ID: 429_inbox_conversation_participants
Revises: 428_vendor_material_release_and_advances
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "429_inbox_conversation_participants"
down_revision = "428_vendor_material_release_and_advances"
branch_labels = None
depends_on = None

_TABLE = "inbox_conversation_participants"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "conversation_id",
            UUID(as_uuid=True),
            sa.ForeignKey("inbox_conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("channel_type", sa.String(40), nullable=False),
        sa.Column("normalized_endpoint", sa.String(320), nullable=False),
        sa.Column(
            "provider_account_scope",
            sa.String(200),
            nullable=False,
            server_default="default",
        ),
        sa.Column(
            "party_contact_point_id",
            UUID(as_uuid=True),
            sa.ForeignKey("party_contact_points.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("party_contact_point_bound_at", sa.DateTime(timezone=True)),
        sa.Column("party_contact_point_binding_source", sa.String(80)),
        sa.Column("party_contact_point_binding_reason", sa.Text()),
        sa.Column(
            "relationship_type", sa.String(24), nullable=False, server_default="unknown"
        ),
        sa.Column("admission_source", sa.String(32), nullable=False),
        sa.Column(
            "admission_message_id",
            UUID(as_uuid=True),
            sa.ForeignKey("inbox_messages.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("admitted_by_person_id", UUID(as_uuid=True)),
        sa.Column(
            "admitted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column("removed_at", sa.DateTime(timezone=True)),
        sa.Column("removed_reason", sa.Text()),
        sa.Column("display_name", sa.String(200)),
        sa.Column("metadata", sa.JSON()),
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
        # Party evidence is all-or-nothing, exactly as on inbox_contact_links.
        sa.CheckConstraint(
            "(party_contact_point_id IS NULL AND "
            "party_contact_point_bound_at IS NULL AND "
            "party_contact_point_binding_source IS NULL AND "
            "party_contact_point_binding_reason IS NULL) OR "
            "(party_contact_point_id IS NOT NULL AND "
            "party_contact_point_bound_at IS NOT NULL AND "
            "party_contact_point_binding_source IS NOT NULL AND "
            "party_contact_point_binding_reason IS NOT NULL AND "
            "length(trim(party_contact_point_binding_source)) > 0 AND "
            "length(trim(party_contact_point_binding_reason)) > 0)",
            name="ck_inbox_participants_party_contact_point_evidence",
        ),
        # A removed participant records when. Removal is a decision with a
        # date, not a flag flip.
        sa.CheckConstraint(
            "(is_active IS TRUE AND removed_at IS NULL)"
            " OR (is_active IS FALSE AND removed_at IS NOT NULL)",
            name="ck_inbox_participants_removal_evidence",
        ),
    )
    op.create_index(
        "ix_inbox_participants_conversation", _TABLE, ["conversation_id", "is_active"]
    )
    op.create_index(
        "ix_inbox_participants_endpoint",
        _TABLE,
        ["channel_type", "normalized_endpoint", "is_active"],
    )
    op.create_index(
        "ix_inbox_participants_party_contact_point",
        _TABLE,
        ["party_contact_point_id", "is_active"],
    )
    # One active row per endpoint per conversation, so re-observing the same
    # header on every message in a thread cannot duplicate a participant.
    op.create_index(
        "uq_inbox_participants_active_endpoint",
        _TABLE,
        [
            "conversation_id",
            "channel_type",
            "normalized_endpoint",
            "provider_account_scope",
        ],
        unique=True,
        postgresql_where=sa.text("is_active IS TRUE"),
        sqlite_where=sa.text("is_active IS TRUE"),
    )


def downgrade() -> None:
    op.drop_index("uq_inbox_participants_active_endpoint", table_name=_TABLE)
    op.drop_index("ix_inbox_participants_party_contact_point", table_name=_TABLE)
    op.drop_index("ix_inbox_participants_endpoint", table_name=_TABLE)
    op.drop_index("ix_inbox_participants_conversation", table_name=_TABLE)
    op.drop_table(_TABLE)
