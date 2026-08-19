"""Add the UI-assignable additional subscription IP permission.

Revision ID: 542_subscription_additional_ip_permission
Revises: 541_staff_session_party_ratchet
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision = "542_subscription_additional_ip_permission"
down_revision = "541_staff_session_party_ratchet"
branch_labels = None
depends_on = None

PERMISSION_KEY = "subscription:additional_ip:write"
PERMISSION_DESCRIPTION = "Assign and remove additional subscription IP ranges"


def upgrade() -> None:
    bind = op.get_bind()
    table_names = set(sa.inspect(bind).get_table_names())
    if "permissions" not in table_names:
        return
    now = datetime.now(UTC)
    permission_id = bind.execute(
        sa.text("SELECT id FROM permissions WHERE key = :key"),
        {"key": PERMISSION_KEY},
    ).scalar()
    if permission_id:
        bind.execute(
            sa.text(
                """
                UPDATE permissions
                SET description = :description, is_active = true,
                    is_ui_assignable = true, updated_at = :now
                WHERE key = :key
                """
            ),
            {"key": PERMISSION_KEY, "description": PERMISSION_DESCRIPTION, "now": now},
        )
    else:
        bind.execute(
            sa.text(
                """
                INSERT INTO permissions (
                    id, key, description, is_active, is_ui_assignable,
                    created_at, updated_at
                ) VALUES (:id, :key, :description, true, true, :now, :now)
                """
            ),
            {
                "id": str(uuid4()),
                "key": PERMISSION_KEY,
                "description": PERMISSION_DESCRIPTION,
                "now": now,
            },
        )

    if {"roles", "role_permissions"}.issubset(table_names):
        bind.execute(
            sa.text(
                """
                INSERT INTO role_permissions (id, role_id, permission_id)
                SELECT (
                       substr(md5(r.id::text || p.id::text), 1, 8) || '-' ||
                       substr(md5(r.id::text || p.id::text), 9, 4) || '-' ||
                       substr(md5(r.id::text || p.id::text), 13, 4) || '-' ||
                       substr(md5(r.id::text || p.id::text), 17, 4) || '-' ||
                       substr(md5(r.id::text || p.id::text), 21, 12)
                       )::uuid,
                       r.id,
                       p.id
                FROM roles r
                JOIN permissions p ON p.key = :key
                WHERE lower(trim(r.name)) = 'noc'
                  AND r.is_active = true
                  AND p.is_active = true
                ON CONFLICT (role_id, permission_id) DO NOTHING
                """
            ),
            {"key": PERMISSION_KEY},
        )


def downgrade() -> None:
    bind = op.get_bind()
    table_names = set(sa.inspect(bind).get_table_names())
    if "permissions" not in table_names:
        return
    if "role_permissions" in table_names:
        bind.execute(
            sa.text(
                """
                DELETE FROM role_permissions rp
                USING permissions p
                WHERE rp.permission_id = p.id AND p.key = :key
                """
            ),
            {"key": PERMISSION_KEY},
        )
    bind.execute(
        sa.text("DELETE FROM permissions WHERE key = :key"), {"key": PERMISSION_KEY}
    )
