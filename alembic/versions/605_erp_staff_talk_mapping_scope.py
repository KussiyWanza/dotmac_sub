"""Seed the ERP staff-to-Nextcloud Talk mapping machine scope.

The ERP workforce adapter uses this scope to bind and disable the explicit
Selfcare-to-Nextcloud identity mapping. It is deliberately not granted to a
role: machine principals carry it directly in their API-key scope array.

Revision ID: 605_erp_staff_talk_mapping_scope
Revises: 604_material_cancel_pending
Create Date: 2026-09-13
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision: str = "605_erp_staff_talk_mapping_scope"
down_revision: str | None = "604_material_cancel_pending"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCOPE = "communications:nextcloud_talk_staff:manage"
DESCRIPTION = "Manage ERP staff-to-Nextcloud Talk identity mappings"
_GRANT_TABLES = (
    "role_permissions",
    "subscriber_permissions",
    "system_user_permissions",
)


def upgrade() -> None:
    bind = op.get_bind()
    if "permissions" not in set(sa.inspect(bind).get_table_names()):
        return
    existing = bind.execute(
        sa.text("SELECT id FROM permissions WHERE key = :key"), {"key": SCOPE}
    ).scalar()
    if existing:
        return
    now = datetime.now(UTC)
    bind.execute(
        sa.text(
            """
            INSERT INTO permissions (
                id, key, description, is_active, is_ui_assignable,
                created_at, updated_at
            )
            VALUES (:id, :key, :description, true, false, :now, :now)
            """
        ),
        {
            "id": str(uuid4()),
            "key": SCOPE,
            "description": DESCRIPTION,
            "now": now,
        },
    )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "permissions" not in tables:
        return
    permission_id = bind.execute(
        sa.text("SELECT id FROM permissions WHERE key = :key"), {"key": SCOPE}
    ).scalar()
    if not permission_id:
        return
    for table in _GRANT_TABLES:
        if table in tables:
            bind.execute(
                sa.text(f"DELETE FROM {table} WHERE permission_id = :permission_id"),
                {"permission_id": permission_id},
            )
    bind.execute(sa.text("DELETE FROM permissions WHERE key = :key"), {"key": SCOPE})
