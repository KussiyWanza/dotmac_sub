"""add Meta direct-message notification channels

Revision ID: 469_meta_direct_message_channels
Revises: 468_immutable_lifecycle_transition_evidence
Create Date: 2026-08-04
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "469_meta_direct_message_channels"
down_revision: str | None = "468_immutable_lifecycle_transition_evidence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        "ALTER TYPE notificationchannel ADD VALUE IF NOT EXISTS 'facebook_messenger'"
    )
    op.execute("ALTER TYPE notificationchannel ADD VALUE IF NOT EXISTS 'instagram_dm'")


def downgrade() -> None:
    # PostgreSQL cannot remove enum labels safely without rebuilding every
    # dependent column. Leaving unused additive labels is the safe rollback.
    pass
