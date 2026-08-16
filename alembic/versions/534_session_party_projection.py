"""Session identity projection: sessions.party_id.

A staff session is a BOUND PAIR after this change:

    party_id        the authenticated identity
    system_user_id  the Sub-owned staff context

Today session identity begins with ``system_user_id``, so a reader validating a
session starts from the legacy key and can only check the Party projection after
the fact. The column added here is what lets validation start from the identity
instead.

Nullable ON PURPOSE, and there is deliberately NO backfill in this migration.
1,240 live staff sessions predate the column. Filling them from inside Alembic
would hide an identity decision inside a schema step, with no digest, no
approval, no per-row disagreement report, and no way to refuse an unmappable
row. The backfill is a separate, approved, digest-bound operation; the reader
ratchet that makes ``party_id`` required comes after it, in a later deploy.

Revision ID: 534_session_party_projection
Revises: 533_ncc_weekly_report_delivery
Create Date: 2026-08-15
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "534_session_party_projection"
down_revision = "533_ncc_weekly_report_delivery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column("party_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_sessions_party_id_parties",
        "sessions",
        "parties",
        ["party_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    # Validation resolves a session by identity, so this index serves the read
    # path, not reporting.
    op.create_index("ix_sessions_party_id", "sessions", ["party_id"])


def downgrade() -> None:
    op.drop_index("ix_sessions_party_id", table_name="sessions")
    op.drop_constraint("fk_sessions_party_id_parties", "sessions", type_="foreignkey")
    op.drop_column("sessions", "party_id")
