"""Register catalog:offer_access_requirement:classify. Not seeded into any role.

Idempotent: reruns update the existing row rather than duplicating it. This
migration deliberately does NOT insert into role_permissions — the reviewed
classification CLI is the only intended caller, gated by a real staff
principal: its ONLY identity argument is ``--actor-system-user-id``, resolved
to an authenticated ``SystemUser`` (see
scripts/catalog/classify_offer_access_requirement.py) — there is no
machine-credential argument or path through this CLI today. Normal
wildcard/admin RBAC access (``*`` or the ``admin`` role) continues to satisfy
this permission exactly as it does every other permission in this system;
that is existing RBAC behavior, not a grant this migration adds.

Revision ID: 609_offer_access_requirement_classify_permission
Revises: 608_offer_access_requirement
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision: str = "609_offer_access_requirement_classify_permission"
down_revision: str | None = "608_offer_access_requirement"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERMISSION_KEY = "catalog:offer_access_requirement:classify"
PERMISSION_DESCRIPTION = (
    "Apply the reviewed access-requirement classification command to an "
    "unclassified offer version"
)


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
                    updated_at = :now
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
    # Deliberately no role_permissions insert: this permission is not seeded
    # into any role by default.


class DowngradeRefused(RuntimeError):
    """Raised when downgrading would silently orphan a real operator grant."""


def _direct_grant_count(bind, table_names: set[str], table: str, key: str) -> int:
    """Rows in a direct-grant table (``system_user_permissions``/
    ``subscriber_permissions``) that FK-reference this permission.

    These are legitimate, UI-created grants, not corrupt data: this
    permission is UI-assignable (``is_ui_assignable=true`` above), so an
    operator may have granted it directly to a principal without going
    through a role. Deleting the permission row while such a grant still
    exists would either cascade (destroying real operator-created state) or
    fail on the FK with an opaque database error; counting first lets the
    caller refuse cleanly instead.
    """

    if table not in table_names:
        return 0
    return int(
        bind.execute(
            sa.text(
                f"""
                SELECT count(*) FROM {table} g
                JOIN permissions p ON g.permission_id = p.id
                WHERE p.key = :key
                """
            ),
            {"key": key},
        ).scalar()
        or 0
    )


def downgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"
    table_names = set(sa.inspect(bind).get_table_names())
    if "permissions" not in table_names:
        return

    if is_postgres:
        # Round 14 finding 5: without a lock here, a grant inserted into
        # any of these three tables AFTER the zero-count checks below but
        # BEFORE the DELETE produces an opaque FK integrity failure
        # instead of the promised DowngradeRefused — data stays safe
        # either way (the FK still blocks the delete), but the documented
        # failure semantics did not hold. Locking all three grant tables
        # BEFORE counting anything closes the window the same way 608's
        # own downgrade already locks its target tables before counting.
        op.execute("SET LOCAL lock_timeout = '5s'")
        op.execute("SET LOCAL statement_timeout = '15min'")
        for grant_table in (
            "system_user_permissions",
            "subscriber_permissions",
            "role_permissions",
        ):
            if grant_table in table_names:
                op.execute(f"LOCK TABLE {grant_table} IN ACCESS EXCLUSIVE MODE")

    direct_grants = _direct_grant_count(
        bind, table_names, "system_user_permissions", PERMISSION_KEY
    ) + _direct_grant_count(bind, table_names, "subscriber_permissions", PERMISSION_KEY)
    if direct_grants:
        raise DowngradeRefused(
            f"{direct_grants} direct grant(s) of {PERMISSION_KEY!r} still "
            "exist (system_user_permissions/subscriber_permissions); "
            "downgrading would either cascade-delete a real operator-created "
            "grant or fail on the FK. Remove the direct grant(s) first, then "
            "re-run the downgrade."
        )

    # A role grant (created through the RBAC admin UI, post-deployment) is
    # just as real as a direct grant — this permission's is_ui_assignable
    # flag makes both shapes possible, and this migration seeds neither.
    role_grants = _direct_grant_count(
        bind, table_names, "role_permissions", PERMISSION_KEY
    )
    if role_grants:
        raise DowngradeRefused(
            f"{role_grants} role_permissions grant(s) of {PERMISSION_KEY!r} "
            "still exist; this permission is UI-assignable to a role as well "
            "as directly, and this migration never seeded one itself, so any "
            "such row is real post-deployment operator configuration. "
            "Downgrading would silently delete it. Remove the role grant(s) "
            "first, then re-run the downgrade."
        )

    bind.execute(
        sa.text("DELETE FROM permissions WHERE key = :key"), {"key": PERMISSION_KEY}
    )
