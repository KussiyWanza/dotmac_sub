"""link staff delivery records to the canonical in-app inbox

Revision ID: 497_unify_staff_notification_inbox
Revises: 496_backfill_project_numbers_8_10
Create Date: 2026-08-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "497_unify_staff_notification_inbox"
down_revision: str | None = "496_backfill_project_numbers_8_10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("admin_notifications", "alert_id", nullable=True)
    op.add_column(
        "admin_notifications",
        sa.Column(
            "source_notification_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_admin_notifications_source_notification",
        "admin_notifications",
        "notifications",
        ["source_notification_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "uq_admin_notifications_source_notification",
        "admin_notifications",
        ["source_notification_id"],
        unique=True,
    )


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM admin_notifications WHERE alert_id IS NULL"))
    op.drop_index(
        "uq_admin_notifications_source_notification",
        table_name="admin_notifications",
    )
    op.drop_constraint(
        "fk_admin_notifications_source_notification",
        "admin_notifications",
        type_="foreignkey",
    )
    op.drop_column("admin_notifications", "source_notification_id")
    op.alter_column("admin_notifications", "alert_id", nullable=False)
