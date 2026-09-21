"""seed billing:prepaid_reconciliation:repair and grant it to admin

The historical paid-invoice repair command
(``app.services.prepaid_draft_reconciliation.repair_historical_paid_prepaid_invoice``)
is a real-money, entitlement-creating capability invoked only through
``scripts/billing/reconcile_prepaid_drafts.py``. It previously carried no
application-level permission gate at all: the CLI passed a bare free-text
``actor`` label through ``CommandContext.system(...)``, so authorization was
effectively "whoever has shell and database credentials on the host running
the script" -- gated only by the owner's data-correctness checks, never by
role.

This is the same shape as ``472_service_extension_reversals``'s
``billing:extension:reverse`` seed: a brand-new capability's permission is
seeded here for the first time and granted only to ``admin`` initially, so an
operator role can be extended to it deliberately later rather than every
existing billing-document-editor role gaining it implicitly.

Revision ID: 597_prepaid_draft_repair_permission
Revises: 596_inbox_customer_completion_policy
Create Date: 2026-09-11
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision: str = "597_prepaid_draft_repair_permission"
down_revision: str | None = "596_inbox_customer_completion_policy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_REPAIR_PERMISSION_KEY = "billing:prepaid_reconciliation:repair"
_REPAIR_PERMISSION_DESCRIPTION = (
    "Repair one exact already-paid prepaid invoice's identity and coverage "
    "after reviewed evidence"
)


def _seed_repair_permission() -> None:
    bind = op.get_bind()
    if not {"permissions", "roles", "role_permissions"}.issubset(
        sa.inspect(bind).get_table_names()
    ):
        return
    metadata = sa.MetaData()
    permissions = sa.Table("permissions", metadata, autoload_with=bind)
    roles = sa.Table("roles", metadata, autoload_with=bind)
    role_permissions = sa.Table("role_permissions", metadata, autoload_with=bind)
    now = datetime.now(UTC)

    permission_id = bind.execute(
        sa.select(permissions.c.id).where(permissions.c.key == _REPAIR_PERMISSION_KEY)
    ).scalar_one_or_none()
    if permission_id is None:
        permission_id = uuid4()
        bind.execute(
            permissions.insert().values(
                id=permission_id,
                key=_REPAIR_PERMISSION_KEY,
                description=_REPAIR_PERMISSION_DESCRIPTION,
                is_active=True,
                is_ui_assignable=True,
                created_at=now,
                updated_at=now,
            )
        )

    admin_id = bind.execute(
        sa.select(roles.c.id).where(
            roles.c.name == "admin",
            roles.c.is_active.is_(True),
        )
    ).scalar_one_or_none()
    if admin_id is None:
        return
    existing = bind.execute(
        sa.select(role_permissions.c.id).where(
            role_permissions.c.role_id == admin_id,
            role_permissions.c.permission_id == permission_id,
        )
    ).scalar_one_or_none()
    if existing is None:
        bind.execute(
            role_permissions.insert().values(
                id=uuid4(),
                role_id=admin_id,
                permission_id=permission_id,
            )
        )


def _unseed_repair_permission() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "permissions" not in tables:
        return
    permission_id = bind.execute(
        sa.text("SELECT id FROM permissions WHERE key = :key"),
        {"key": _REPAIR_PERMISSION_KEY},
    ).scalar()
    if permission_id is None:
        return
    if "role_permissions" in tables:
        bind.execute(
            sa.text("DELETE FROM role_permissions WHERE permission_id = :id"),
            {"id": permission_id},
        )
    bind.execute(
        sa.text("DELETE FROM permissions WHERE id = :id"),
        {"id": permission_id},
    )


def upgrade() -> None:
    _seed_repair_permission()


def downgrade() -> None:
    _unseed_repair_permission()
