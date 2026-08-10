"""Add manager AI permission for Team Inbox.

Revision ID: 510_inbox_manager_ai_permission
Revises: 507_domain_settings_scope_columns
Create Date: 2026-08-09
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision: str = "510_inbox_manager_ai_permission"
down_revision: str | None = "509_backfill_operator_tenant_scope"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERMISSION_KEY = "support:inbox_ai:read"
PERMISSION_DESCRIPTION = "Use manager AI for Team Inbox insight"
SOURCE_KEYS = ("support:ticket:update",)


def _permission_id(bind, key: str) -> str | None:
    return bind.execute(
        sa.text("SELECT id FROM permissions WHERE key = :key"), {"key": key}
    ).scalar()


def upgrade() -> None:
    bind = op.get_bind()
    tables = sa.inspect(bind).get_table_names()
    if "permissions" not in tables:
        return

    permission_id = _permission_id(bind, PERMISSION_KEY)
    if permission_id is None:
        permission_id = str(uuid4())
        now = datetime.now(UTC)
        bind.execute(
            sa.text(
                """
                INSERT INTO permissions (
                    id, key, description, is_active, is_ui_assignable,
                    created_at, updated_at
                )
                VALUES (:id, :key, :description, true, true, :now, :now)
                """
            ),
            {
                "id": permission_id,
                "key": PERMISSION_KEY,
                "description": PERMISSION_DESCRIPTION,
                "now": now,
            },
        )

    if "role_permissions" not in tables:
        return
    source_ids = [pid for key in SOURCE_KEYS if (pid := _permission_id(bind, key))]
    if not source_ids:
        return
    role_ids = {
        row[0]
        for row in bind.execute(
            sa.text(
                "SELECT DISTINCT role_id FROM role_permissions "
                "WHERE permission_id IN :ids"
            ).bindparams(sa.bindparam("ids", expanding=True)),
            {"ids": source_ids},
        ).fetchall()
    }
    for role_id in role_ids:
        already = bind.execute(
            sa.text(
                "SELECT 1 FROM role_permissions "
                "WHERE role_id = :role_id AND permission_id = :permission_id"
            ),
            {"role_id": role_id, "permission_id": permission_id},
        ).scalar()
        if not already:
            bind.execute(
                sa.text(
                    "INSERT INTO role_permissions (id, role_id, permission_id) "
                    "VALUES (:id, :role_id, :permission_id)"
                ),
                {
                    "id": str(uuid4()),
                    "role_id": role_id,
                    "permission_id": permission_id,
                },
            )


def downgrade() -> None:
    bind = op.get_bind()
    tables = sa.inspect(bind).get_table_names()
    if "permissions" not in tables:
        return
    permission_id = _permission_id(bind, PERMISSION_KEY)
    if permission_id is None:
        return
    if "role_permissions" in tables:
        bind.execute(
            sa.text("DELETE FROM role_permissions WHERE permission_id = :id"),
            {"id": permission_id},
        )
    bind.execute(
        sa.text("DELETE FROM permissions WHERE key = :key"),
        {"key": PERMISSION_KEY},
    )
