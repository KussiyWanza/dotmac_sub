"""Mark external contractor principals with their own user type.

Vendor logins live in ``system_users`` alongside staff. Without a marker an
external contractor is indistinguishable from an employee, so staff screens,
grants, and audits cannot separate them and nothing structural stops a vendor
principal being handed staff permissions.

``usertype`` is shared with ``subscribers``; adding a value there is inert for
subscriber rows, which never use it.

Revision ID: 427_vendor_principal_user_type
Revises: 426_service_team_lifecycle
"""

from __future__ import annotations

from alembic import op

revision = "427_vendor_principal_user_type"
down_revision = "426_service_team_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TYPE usertype ADD VALUE IF NOT EXISTS 'vendor'")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    # Postgres cannot drop an enum value. Reclassify any vendor principal back
    # to the generic type and rebuild the type without 'vendor'. The vendor
    # membership rows are untouched, so re-upgrading restores the marker.
    op.execute(
        "UPDATE system_users SET user_type = 'system_user' WHERE user_type = 'vendor'"
    )
    op.execute("ALTER TYPE usertype RENAME TO usertype_old")
    op.execute("CREATE TYPE usertype AS ENUM ('system_user', 'customer', 'reseller')")
    op.execute(
        "ALTER TABLE system_users ALTER COLUMN user_type DROP DEFAULT, "
        "ALTER COLUMN user_type TYPE usertype USING user_type::text::usertype, "
        "ALTER COLUMN user_type SET DEFAULT 'system_user'"
    )
    # subscribers.user_type also defaults to 'system_user' (migration
    # 7a1c9e2d4f55), not 'customer'.
    op.execute(
        "ALTER TABLE subscribers ALTER COLUMN user_type DROP DEFAULT, "
        "ALTER COLUMN user_type TYPE usertype USING user_type::text::usertype, "
        "ALTER COLUMN user_type SET DEFAULT 'system_user'"
    )
    op.execute("DROP TYPE usertype_old")
