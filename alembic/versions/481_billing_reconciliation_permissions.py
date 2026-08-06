"""seed billing:reconciliation:read/write and grant them to billing document editors

The prepaid billing calendar reconciliation surface gates three mounted routes
on these keys (``app/web/admin/billing_calendar_reconciliation.py`` lines 56,
85 and 112), and the queue template hides its action behind
``can(request, 'billing:reconciliation:write')``. Both keys existed only in
``scripts/seed/seed_rbac.py``, which no deploy runs, so neither row was ever
created on a deployed database. A key that does not exist can be held by
nobody, so every principal failed the dependency except roles carrying the
``*`` wildcard -- the whole surface was unreachable for non-admin staff.

This is the same defect as ``477_quote_send_permission``. It escaped the
route/seed parity guard for an additional reason: these routes reference the
keys through module constants (``require_permission(service.READ_PERMISSION)``)
rather than string literals, and the guard only inspects literals.

Grants are copied from existing ``billing:invoice:update`` holders rather than
assigned to named roles. Production carries two role families -- the seeded
snake_case roles and operator-created display-named ones -- and
``seed_rbac.ROLE_PERMISSIONS`` knows only the first, so naming roles here would
leave the operator-created billing roles locked out. Reconciling billing dates
is part of the authority to amend billing documents; copying grants it to
exactly those principals and to nobody who could not already edit an invoice.

Revision ID: 481_billing_reconciliation_permissions
Revises: 480_quote_discount_history
Create Date: 2026-08-06
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision: str = "481_billing_reconciliation_permissions"
down_revision: str | None = "480_quote_discount_history"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SOURCE_KEY = "billing:invoice:update"
TARGET_KEYS: tuple[tuple[str, str], ...] = (
    ("billing:reconciliation:read", "View billing reconciliation queues"),
    ("billing:reconciliation:write", "Action billing reconciliation items"),
)

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
    target_ids: list,
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
        for target_id in target_ids:
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

    target_ids: list = []
    for key, description in TARGET_KEYS:
        pid = _permission_id(bind, key)
        if not pid:
            pid = str(uuid4())
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
                {"id": pid, "key": key, "description": description, "now": now},
            )
        target_ids.append(pid)

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
            target_ids=target_ids,
            now=now,
        )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "permissions" not in tables:
        return
    for key, _description in TARGET_KEYS:
        target_id = _permission_id(bind, key)
        if not target_id:
            continue
        for table, _holder_column, _granted_by_column in _GRANT_TABLES:
            if table not in tables:
                continue
            bind.execute(
                sa.text(f"DELETE FROM {table} WHERE permission_id = :p"),
                {"p": target_id},
            )
        bind.execute(sa.text("DELETE FROM permissions WHERE key = :key"), {"key": key})
