"""Register catalog:offer_version:admission. Not seeded into any role.

Idempotent: reruns update the existing row rather than duplicating it. This
mirrors 609_offer_access_requirement_classify_permission's exact pattern:
this migration seeds the permission row only.

Admission authorization has ONE decision owner
(``service_intent.offer_access_requirement.authorize_offer_version_
admission``) — see that module's docstring. TWO callers delegate to it: the
route (``app/api/catalog.py``'s ``_require_offer_version_admission``, on
``admission_router``, which deliberately carries NO blanket router-level
gate of its own) and a fresh, in-transaction re-check inside
``service_intent.offer_access_requirement``'s own command
(``verify_admission_authorization``). The effective requirement either
delegate enforces is ``catalog:write AND (catalog:billing_write OR
catalog:offer_version:admission)`` -- never a pure OR/standalone-narrower-
permission alternative. A caller holding the existing ``catalog:billing_write``
continues to admit an offer version exactly as before, and this permission is
a genuine, narrower, OPT-IN future delegation path, not a hard requirement
layered on top of existing access. There is therefore nothing to copy from
``catalog:billing_write``'s existing grants: no role loses or gains admission
ability as a side effect of this migration, so (unlike an earlier version of
this migration) there is no grant-copying logic here, and no risk of an
incomplete copy regressing an API-key or direct-permission-grant principal.

Revision ID: 610_offer_version_admission_permission
Revises: 609_offer_access_requirement_classify_permission
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision: str = "610_offer_version_admission_permission"
down_revision: str | None = "609_offer_access_requirement_classify_permission"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERMISSION_KEY = "catalog:offer_version:admission"
PERMISSION_DESCRIPTION = (
    "Admit or update an offer version via "
    "service_intent.offer_access_requirement (an alternative to "
    "catalog:billing_write, not a replacement for it)"
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
