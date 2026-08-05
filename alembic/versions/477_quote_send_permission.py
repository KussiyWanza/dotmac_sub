"""seed crm:quote:send and grant it to established crm:quote:write holders

Migration 471 shipped the branded Quote email surface, but its permission key
lived only in ``scripts/seed/seed_rbac.py``. That seed is a standalone script
which no deploy runs, so ``crm:quote:send`` never existed on deployed
databases: the admin "Send Email" button is hidden for every role and the route
dependency fails closed. The feature was dark in production with green CI.

``crm:quote:*`` is deliberately granted to no seeded role (sub has no seeded
sales role), so the live grants are operator policy authored through the role
builder. This migration therefore seeds the permission and copies every
existing ``crm:quote:write`` grant onto it, across role, subscriber and
system-user grant tables. Sending is treated as part of the established quote
management authority rather than new policy invented here.

Revision ID: 477_quote_send_permission
Revises: 476_reconcile_project_number_series
Create Date: 2026-08-05
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision: str = "477_quote_send_permission"
down_revision: str | None = "476_reconcile_project_number_series"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SOURCE_KEY = "crm:quote:write"
TARGET_KEY = "crm:quote:send"
TARGET_DESCRIPTION = "Send quotes to customers"

# (table, holder column, granted_by column or None)
_GRANT_TABLES: tuple[tuple[str, str, str | None], ...] = (
    ("role_permissions", "role_id", None),
    ("subscriber_permissions", "subscriber_id", "granted_by_subscriber_id"),
    ("system_user_permissions", "system_user_id", "granted_by_system_user_id"),
)


def _permission_id(bind, key: str):
    return bind.execute(
        sa.text("SELECT id FROM permissions WHERE key = :key"), {"key": key}
    ).scalar()


def _copy_holder_grants(
    bind,
    *,
    tables: set[str],
    table: str,
    holder_column: str,
    granted_by_column: str | None,
    source_id,
    target_id,
    now: datetime,
) -> None:
    if table not in tables:
        return
    extra_columns = f", granted_at, {granted_by_column}" if granted_by_column else ""
    rows = bind.execute(
        sa.text(
            f"SELECT {holder_column}{extra_columns} "
            f"FROM {table} WHERE permission_id = :permission_id"
        ),
        {"permission_id": source_id},
    ).fetchall()
    for row in rows:
        holder_id = row[0]
        already = bind.execute(
            sa.text(
                f"SELECT 1 FROM {table} "
                f"WHERE {holder_column} = :holder_id "
                "AND permission_id = :permission_id"
            ),
            {"holder_id": holder_id, "permission_id": target_id},
        ).scalar()
        if already:
            continue
        if granted_by_column:
            bind.execute(
                sa.text(
                    f"INSERT INTO {table} "
                    f"(id, {holder_column}, permission_id, granted_at, "
                    f"{granted_by_column}) "
                    "VALUES (:id, :holder_id, :permission_id, :granted_at, "
                    ":granted_by)"
                ),
                {
                    "id": str(uuid4()),
                    "holder_id": holder_id,
                    "permission_id": target_id,
                    "granted_at": row[1] or now,
                    "granted_by": row[2],
                },
            )
        else:
            bind.execute(
                sa.text(
                    f"INSERT INTO {table} "
                    f"(id, {holder_column}, permission_id) "
                    "VALUES (:id, :holder_id, :permission_id)"
                ),
                {
                    "id": str(uuid4()),
                    "holder_id": holder_id,
                    "permission_id": target_id,
                },
            )


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "permissions" not in tables:
        return
    now = datetime.now(UTC)

    target_id = _permission_id(bind, TARGET_KEY)
    if not target_id:
        target_id = str(uuid4())
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
                "id": target_id,
                "key": TARGET_KEY,
                "description": TARGET_DESCRIPTION,
                "now": now,
            },
        )

    source_id = _permission_id(bind, SOURCE_KEY)
    if not source_id:
        return
    for table, holder_column, granted_by_column in _GRANT_TABLES:
        _copy_holder_grants(
            bind,
            tables=tables,
            table=table,
            holder_column=holder_column,
            granted_by_column=granted_by_column,
            source_id=source_id,
            target_id=target_id,
            now=now,
        )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "permissions" not in tables:
        return
    target_id = _permission_id(bind, TARGET_KEY)
    if not target_id:
        return
    for table, _holder_column, _granted_by_column in _GRANT_TABLES:
        if table not in tables:
            continue
        bind.execute(
            sa.text(f"DELETE FROM {table} WHERE permission_id = :p"),
            {"p": target_id},
        )
    bind.execute(
        sa.text("DELETE FROM permissions WHERE key = :key"), {"key": TARGET_KEY}
    )
