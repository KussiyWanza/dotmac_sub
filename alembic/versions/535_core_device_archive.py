"""Add reversible core-device archival lifecycle.

Revision ID: 535_core_device_archive
Revises: 534_session_party_projection
Create Date: 2026-08-15
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision = "535_core_device_archive"
down_revision = "534_session_party_projection"
branch_labels = None
depends_on = None

_PERMISSION_KEY = "network:device:archive"
_PERMISSION_DESCRIPTION = "Archive and restore core network devices"


def _install_permission() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not {"roles", "permissions", "role_permissions"}.issubset(
        inspector.get_table_names()
    ):
        return
    metadata = sa.MetaData()
    permissions = sa.Table("permissions", metadata, autoload_with=bind)
    roles = sa.Table("roles", metadata, autoload_with=bind)
    role_permissions = sa.Table("role_permissions", metadata, autoload_with=bind)
    now = datetime.now(UTC)
    permission_id = bind.execute(
        sa.select(permissions.c.id).where(permissions.c.key == _PERMISSION_KEY)
    ).scalar_one_or_none()
    if permission_id is None:
        permission_id = uuid4()
        bind.execute(
            permissions.insert().values(
                id=permission_id,
                key=_PERMISSION_KEY,
                description=_PERMISSION_DESCRIPTION,
                is_active=True,
                is_ui_assignable=True,
                created_at=now,
                updated_at=now,
            )
        )
    admin_id = bind.execute(
        sa.select(roles.c.id).where(
            roles.c.name == "admin", roles.c.is_active.is_(True)
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
                id=uuid4(), role_id=admin_id, permission_id=permission_id
            )
        )


def upgrade() -> None:
    op.add_column(
        "network_devices",
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "network_devices",
        sa.Column("archived_by", sa.String(length=160), nullable=True),
    )
    op.add_column(
        "network_devices",
        sa.Column("archive_reason", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_network_devices_archived_at",
        "network_devices",
        ["archived_at"],
    )
    op.create_check_constraint(
        "ck_network_device_archive_state",
        "network_devices",
        "archived_at IS NULL OR "
        "(NOT is_active AND archived_by IS NOT NULL "
        "AND length(trim(archived_by)) > 0 "
        "AND archive_reason IS NOT NULL "
        "AND length(trim(archive_reason)) BETWEEN 3 AND 500)",
    )

    op.drop_constraint(
        "ck_device_projection_lifecycle_state",
        "device_projections",
        type_="check",
    )
    op.create_check_constraint(
        "ck_device_projection_lifecycle_state",
        "device_projections",
        "lifecycle_state IN ('active', 'inactive', 'archived')",
    )
    _install_permission()


def downgrade() -> None:
    # Downgrade is intentionally fail-closed if archived rows still exist.
    bind = op.get_bind()
    archived_count = bind.execute(
        sa.text("SELECT COUNT(*) FROM network_devices WHERE archived_at IS NOT NULL")
    ).scalar_one()
    if archived_count:
        raise RuntimeError(
            "Restore all archived core devices before downgrading "
            "535_core_device_archive"
        )
    op.execute(
        "UPDATE device_projections SET lifecycle_state = 'inactive' "
        "WHERE lifecycle_state = 'archived'"
    )
    op.drop_constraint(
        "ck_device_projection_lifecycle_state",
        "device_projections",
        type_="check",
    )
    op.create_check_constraint(
        "ck_device_projection_lifecycle_state",
        "device_projections",
        "lifecycle_state IN ('active', 'inactive')",
    )
    op.drop_constraint(
        "ck_network_device_archive_state",
        "network_devices",
        type_="check",
    )
    op.drop_index("ix_network_devices_archived_at", table_name="network_devices")
    op.drop_column("network_devices", "archive_reason")
    op.drop_column("network_devices", "archived_by")
    op.drop_column("network_devices", "archived_at")

    inspector = sa.inspect(bind)
    if {"permissions", "role_permissions"}.issubset(inspector.get_table_names()):
        metadata = sa.MetaData()
        permissions = sa.Table("permissions", metadata, autoload_with=bind)
        role_permissions = sa.Table("role_permissions", metadata, autoload_with=bind)
        permission_id = bind.execute(
            sa.select(permissions.c.id).where(permissions.c.key == _PERMISSION_KEY)
        ).scalar_one_or_none()
        if permission_id is not None:
            bind.execute(
                role_permissions.delete().where(
                    role_permissions.c.permission_id == permission_id
                )
            )
            bind.execute(permissions.delete().where(permissions.c.id == permission_id))
