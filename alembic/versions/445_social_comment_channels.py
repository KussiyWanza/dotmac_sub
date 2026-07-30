"""add social comment notification channels

Revision ID: 445_social_comment_channels
Revises: 444_crm_lead_delete_permission
Create Date: 2026-07-30
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "445_social_comment_channels"
down_revision: str | None = "444_crm_lead_delete_permission"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        "ALTER TYPE notificationchannel ADD VALUE IF NOT EXISTS 'facebook_comment'"
    )
    op.execute(
        "ALTER TYPE notificationchannel ADD VALUE IF NOT EXISTS 'instagram_comment'"
    )


def downgrade() -> None:
    # PostgreSQL cannot remove an enum label safely without rebuilding every
    # dependent column. Leaving an unused additive value is the safe rollback.
    pass
