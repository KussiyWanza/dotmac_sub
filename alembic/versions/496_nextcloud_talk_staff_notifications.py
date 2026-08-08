"""add durable staff notifications through Nextcloud Talk

Revision ID: 496_nextcloud_talk_staff_notifications
Revises: 495_plan_family_catalogues
Create Date: 2026-08-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "496_nextcloud_talk_staff_notifications"
down_revision: str | None = "495_plan_family_catalogues"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "ALTER TYPE notificationchannel ADD VALUE IF NOT EXISTS 'nextcloud_talk'"
        )

    op.create_table(
        "nextcloud_talk_staff_accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("system_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "integration_installation_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("nextcloud_username", sa.String(length=255), nullable=False),
        sa.Column(
            "nextcloud_username_normalized", sa.String(length=255), nullable=False
        ),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_by", sa.String(length=160), nullable=True),
        sa.Column("updated_by", sa.String(length=160), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["system_user_id"], ["system_users.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["integration_installation_id"],
            ["integration_installations.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "system_user_id",
            "integration_installation_id",
            name="uq_nextcloud_talk_staff_account_user_installation",
        ),
        sa.UniqueConstraint(
            "integration_installation_id",
            "nextcloud_username_normalized",
            name="uq_nextcloud_talk_staff_account_installation_username",
        ),
    )
    op.create_index(
        "ix_nextcloud_talk_staff_accounts_installation_active",
        "nextcloud_talk_staff_accounts",
        ["integration_installation_id", "is_active"],
    )

    op.create_table(
        "nextcloud_talk_notification_rooms",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("system_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "integration_installation_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("invite_target", sa.String(length=255), nullable=False),
        sa.Column("room_token", sa.String(length=255), nullable=False),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_code", sa.String(length=120), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["system_user_id"], ["system_users.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["integration_installation_id"],
            ["integration_installations.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "system_user_id",
            "integration_installation_id",
            name="uq_nextcloud_talk_room_user_installation",
        ),
    )

    op.add_column(
        "notifications",
        sa.Column(
            "integration_capability_binding_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.add_column(
        "notifications",
        sa.Column("dedupe_key", sa.String(length=240), nullable=True),
    )
    op.create_foreign_key(
        "fk_notifications_integration_capability_binding_id",
        "notifications",
        "integration_capability_bindings",
        ["integration_capability_binding_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_notifications_integration_capability_binding_id",
        "notifications",
        ["integration_capability_binding_id"],
    )
    op.create_index(
        "uq_notifications_channel_dedupe_key",
        "notifications",
        ["channel", "dedupe_key"],
        unique=True,
        postgresql_where=sa.text("dedupe_key IS NOT NULL"),
    )
    op.create_index(
        "ix_notifications_talk_delivery_claim",
        "notifications",
        ["channel", "status", "send_at", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_notifications_talk_delivery_claim", table_name="notifications")
    op.drop_index("uq_notifications_channel_dedupe_key", table_name="notifications")
    op.drop_index(
        "ix_notifications_integration_capability_binding_id",
        table_name="notifications",
    )
    op.drop_constraint(
        "fk_notifications_integration_capability_binding_id",
        "notifications",
        type_="foreignkey",
    )
    op.drop_column("notifications", "dedupe_key")
    op.drop_column("notifications", "integration_capability_binding_id")
    op.drop_table("nextcloud_talk_notification_rooms")
    op.drop_index(
        "ix_nextcloud_talk_staff_accounts_installation_active",
        table_name="nextcloud_talk_staff_accounts",
    )
    op.drop_table("nextcloud_talk_staff_accounts")
    # PostgreSQL enum values remain additive on downgrade.
