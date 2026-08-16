"""seed the two Integrator observation scopes on deployed databases

``app/api/integrator_observations.py`` guards its write port with
``integration:observations:write`` and its shadow port with
``integration:observations:mirror``. Both keys live in
``scripts/seed/seed_rbac.py``, and that seed is a standalone script no deploy
runs — the exact failure migration 477 exists to record. Without this migration
the ports would be dark on every deployed database with green CI.

No grant is copied onto either key. These are machine-principal scopes carried
directly on an ``ApiKey.scopes`` array, and ``require_permission`` matches a key
scope before it looks at ``permissions`` at all. The rows exist here so the
catalogue is complete, the role builder can display them, and a human principal
can be granted them deliberately rather than by inheriting somebody else's
authority.

The write scope is admin-only in the role builder. A UI-assignable ingress
scope invites an operator to attach "accept inbound observations from anywhere"
to an ordinary role; the mirror scope is read-only evidence and stays
assignable so the shadow window can be operated without an admin.

Revision ID: 536_integrator_ingress_scopes
Revises: 535_core_device_archive
Create Date: 2026-08-16
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision: str = "536_integrator_ingress_scopes"
down_revision: str | None = "535_core_device_archive"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (key, description, is_ui_assignable)
_SCOPES: tuple[tuple[str, str, bool], ...] = (
    (
        "integration:observations:write",
        "Integrator inbound observation ingress (messaging.receive.v1)",
        False,
    ),
    (
        "integration:observations:mirror",
        "Integrator inbound observation parity evidence, read-only",
        True,
    ),
)

_GRANT_TABLES = (
    "role_permissions",
    "subscriber_permissions",
    "system_user_permissions",
)


def upgrade() -> None:
    bind = op.get_bind()
    if "permissions" not in set(sa.inspect(bind).get_table_names()):
        return
    now = datetime.now(UTC)
    for key, description, ui_assignable in _SCOPES:
        existing = bind.execute(
            sa.text("SELECT id FROM permissions WHERE key = :key"), {"key": key}
        ).scalar()
        if existing:
            continue
        bind.execute(
            sa.text(
                """
                INSERT INTO permissions (
                    id, key, description, is_active, is_ui_assignable,
                    created_at, updated_at
                )
                VALUES (:id, :key, :description, true, :assignable, :now, :now)
                """
            ),
            {
                "id": str(uuid4()),
                "key": key,
                "description": description,
                "assignable": ui_assignable,
                "now": now,
            },
        )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "permissions" not in tables:
        return
    for key, _description, _assignable in _SCOPES:
        permission_id = bind.execute(
            sa.text("SELECT id FROM permissions WHERE key = :key"), {"key": key}
        ).scalar()
        if not permission_id:
            continue
        for table in _GRANT_TABLES:
            if table not in tables:
                continue
            bind.execute(
                sa.text(f"DELETE FROM {table} WHERE permission_id = :p"),
                {"p": permission_id},
            )
        bind.execute(sa.text("DELETE FROM permissions WHERE key = :key"), {"key": key})
