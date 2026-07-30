"""Add expiring, management-only ONT commissioning intents.

Revision ID: 446_ont_commissioning_intents
Revises: 445_social_comment_channels
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "446_ont_commissioning_intents"
down_revision = "445_social_comment_channels"
branch_labels = None
depends_on = None

_PERMISSION_KEY = "network:ont:commission"
_PERMISSION_DESCRIPTION = "Commission unassigned ONTs for management access"
_ROLE_NAMES = ("admin", "operator")
_ACTIVE_STATES = (
    "'commissioning', 'authorizing', 'awaiting_acs', 'management_ready', "
    "'failed', 'cleanup_pending', 'cleanup_running'"
)


def _add_permission() -> None:
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
    for role_name in _ROLE_NAMES:
        role_id = bind.execute(
            sa.select(roles.c.id).where(
                roles.c.name == role_name,
                roles.c.is_active.is_(True),
            )
        ).scalar_one_or_none()
        if role_id is None:
            continue
        exists = bind.execute(
            sa.select(role_permissions.c.id).where(
                role_permissions.c.role_id == role_id,
                role_permissions.c.permission_id == permission_id,
            )
        ).scalar_one_or_none()
        if exists is None:
            bind.execute(
                role_permissions.insert().values(
                    id=uuid4(),
                    role_id=role_id,
                    permission_id=permission_id,
                )
            )


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "ALTER TYPE networkoperationtype ADD VALUE IF NOT EXISTS 'ont_commission'"
        )
        op.execute(
            "ALTER TYPE networkoperationtype "
            "ADD VALUE IF NOT EXISTS 'ont_commission_cleanup'"
        )

    op.create_table(
        "ont_commissioning_intents",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "autofind_candidate_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("ont_unit_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("olt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("latest_operation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("cleanup_operation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("canonical_serial", sa.String(length=120), nullable=False),
        sa.Column("fsp", sa.String(length=32), nullable=False),
        sa.Column(
            "state",
            sa.String(length=32),
            nullable=False,
            server_default="commissioning",
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("reference", sa.String(length=160), nullable=True),
        sa.Column("requested_by", sa.String(length=160), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("device_authorized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("management_ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provisioned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cleanup_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cleanup_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("terminal_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_reconciled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(length=120), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(trim(reason)) > 0",
            name="ck_ont_commissioning_reason_required",
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name="ck_ont_commissioning_expiry_after_creation",
        ),
        sa.CheckConstraint(
            "state IN ("
            "'commissioning', 'authorizing', 'awaiting_acs', 'management_ready', "
            "'assigned', 'provisioned', 'failed', 'cleanup_pending', "
            "'cleanup_running', 'expired', 'canceled'"
            ")",
            name="ontcommissioningstate",
        ),
        sa.ForeignKeyConstraint(
            ["autofind_candidate_id"],
            ["olt_autofind_candidates.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(["ont_unit_id"], ["ont_units.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["olt_id"], ["olt_devices.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["latest_operation_id"],
            ["network_operations.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["cleanup_operation_id"],
            ["network_operations.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ont_commissioning_state_expires",
        "ont_commissioning_intents",
        ["state", "expires_at"],
    )
    op.create_index(
        "uq_ont_commissioning_active_serial",
        "ont_commissioning_intents",
        ["canonical_serial"],
        unique=True,
        postgresql_where=sa.text(f"state IN ({_ACTIVE_STATES})"),
        sqlite_where=sa.text(f"state IN ({_ACTIVE_STATES})"),
    )
    _add_permission()


def downgrade() -> None:
    bind = op.get_bind()
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
    op.drop_index(
        "uq_ont_commissioning_active_serial",
        table_name="ont_commissioning_intents",
    )
    op.drop_index(
        "ix_ont_commissioning_state_expires",
        table_name="ont_commissioning_intents",
    )
    op.drop_table("ont_commissioning_intents")
    # PostgreSQL enum values are additive. Removing them safely would require
    # rebuilding every networkoperationtype column, so rollback leaves them idle.
