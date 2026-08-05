"""add append-only service-extension reversal evidence

Revision ID: 472_service_extension_reversals
Revises: 471_quote_documents_and_delivery
Create Date: 2026-08-05
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "472_service_extension_reversals"
down_revision: str | None = "471_quote_documents_and_delivery"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_REVERSE_PERMISSION_KEY = "billing:extension:reverse"
_REVERSE_PERMISSION_DESCRIPTION = (
    "Reverse applied service extensions after reviewed impact preview"
)


def _seed_reverse_permission() -> None:
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
        sa.select(permissions.c.id).where(permissions.c.key == _REVERSE_PERMISSION_KEY)
    ).scalar_one_or_none()
    if permission_id is None:
        permission_id = uuid4()
        bind.execute(
            permissions.insert().values(
                id=permission_id,
                key=_REVERSE_PERMISSION_KEY,
                description=_REVERSE_PERMISSION_DESCRIPTION,
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


def upgrade() -> None:
    # PostgreSQL enum labels cannot be added through create_table metadata.
    # The aggregate status remains the single visible lifecycle state while
    # the linked reversal tables preserve the immutable correction evidence.
    op.execute("ALTER TYPE serviceextensionstatus ADD VALUE IF NOT EXISTS 'reversed'")
    op.create_table(
        "service_extension_reversals",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("extension_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("preview_fingerprint_sha256", sa.String(64), nullable=False),
        sa.Column("idempotency_key_sha256", sa.String(64), nullable=False),
        sa.Column("command_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reversed_by", sa.String(64), nullable=False),
        sa.Column("reversed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("inspected_count", sa.Integer(), nullable=False),
        sa.Column("restored_anchor_count", sa.Integer(), nullable=False),
        sa.Column("preserved_later_anchor_count", sa.Integer(), nullable=False),
        sa.Column("preserved_lower_anchor_count", sa.Integer(), nullable=False),
        sa.Column("preserved_terminal_count", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["extension_id"],
            ["service_extensions.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "extension_id",
            name="uq_service_extension_reversals_extension",
        ),
    )
    op.create_table(
        "service_extension_reversal_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reversal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "extension_entry_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("subscription_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "observed_next_billing_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "resulting_next_billing_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("disposition", sa.String(40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["extension_entry_id"],
            ["service_extension_entries.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reversal_id"],
            ["service_extension_reversals.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["subscriptions.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "extension_entry_id",
            name="uq_service_extension_reversal_entries_extension_entry",
        ),
        sa.UniqueConstraint(
            "reversal_id",
            "subscription_id",
            name="uq_service_extension_reversal_entries_subscription",
        ),
    )
    op.create_index(
        "ix_service_extension_reversal_entries_reversal",
        "service_extension_reversal_entries",
        ["reversal_id"],
        unique=False,
    )
    _seed_reverse_permission()


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if {"permissions", "role_permissions"}.issubset(tables):
        metadata = sa.MetaData()
        permissions = sa.Table("permissions", metadata, autoload_with=bind)
        role_permissions = sa.Table("role_permissions", metadata, autoload_with=bind)
        permission_id = bind.execute(
            sa.select(permissions.c.id).where(
                permissions.c.key == _REVERSE_PERMISSION_KEY
            )
        ).scalar_one_or_none()
        if permission_id is not None:
            bind.execute(
                role_permissions.delete().where(
                    role_permissions.c.permission_id == permission_id
                )
            )
            bind.execute(permissions.delete().where(permissions.c.id == permission_id))
    op.drop_index(
        "ix_service_extension_reversal_entries_reversal",
        table_name="service_extension_reversal_entries",
    )
    op.drop_table("service_extension_reversal_entries")
    op.drop_table("service_extension_reversals")
    # PostgreSQL cannot remove an enum label without rebuilding the type and
    # table. Leaving the unused additive label is the safe rollback posture.
