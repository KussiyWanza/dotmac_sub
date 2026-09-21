"""Provision the separately grantable field expense payment permission.

Revision ID: 592_field_expense_payment_permission
Revises: 591_field_note_delivery_idempotency
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision: str = "592_field_expense_payment_permission"
down_revision: str | None = "591_field_note_delivery_idempotency"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERMISSION_KEY = "operations:expense_request:pay"
PERMISSION_DESCRIPTION = "Initiate approved expense reimbursements"
SOURCE_PERMISSION_KEY = "operations:expense_request:write"


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
    source_id = _permission_id(bind, SOURCE_PERMISSION_KEY)
    if source_id is None:
        return
    role_ids = bind.execute(
        sa.text(
            "SELECT DISTINCT role_id FROM role_permissions "
            "WHERE permission_id = :permission_id"
        ),
        {"permission_id": source_id},
    ).scalars()
    for role_id in role_ids:
        exists = bind.execute(
            sa.text(
                "SELECT 1 FROM role_permissions "
                "WHERE role_id = :role_id AND permission_id = :permission_id"
            ),
            {"role_id": role_id, "permission_id": permission_id},
        ).scalar()
        if not exists:
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
