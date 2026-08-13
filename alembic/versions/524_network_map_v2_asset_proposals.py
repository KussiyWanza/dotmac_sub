"""Add governed Network Map V2 asset proposal evidence.

The legacy FiberChangeRequest JSON payload cannot bind an exact V2 movement,
independent reviewer, stale-source digest, and two idempotency fingerprints.
This expand-only migration adds a dedicated evidence table owned by
network.map_asset_change_governance and a separately assignable review
permission. It changes no canonical network row and performs no backfill.

Revision ID: 524_network_map_v2_asset_proposals
Revises: 523_domain_settings_tenant_fk
Create Date: 2026-08-12
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision: str = "524_network_map_v2_asset_proposals"
down_revision: str | None = "523_domain_settings_tenant_fk"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "network_map_asset_change_proposals"
REVIEW_PERMISSION = "network:fiber:review"
REVIEW_DESCRIPTION = "Independently review governed fiber asset proposals"


def _permission_id(bind: sa.Connection) -> object | None:
    return bind.execute(
        sa.text("SELECT id FROM permissions WHERE key = :key"),
        {"key": REVIEW_PERMISSION},
    ).scalar()


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("asset_type", sa.String(length=40), nullable=False),
        sa.Column("operation", sa.String(length=20), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("target_asset_id", sa.Uuid(), nullable=True),
        sa.Column("result_asset_id", sa.Uuid(), nullable=True),
        sa.Column("before_values", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("after_values", sa.JSON(), nullable=False),
        sa.Column("source_asset_sha256", sa.String(length=64), nullable=True),
        sa.Column("proposal_sha256", sa.String(length=64), nullable=False),
        sa.Column("submit_key_sha256", sa.String(length=64), nullable=False),
        sa.Column("submit_fingerprint_sha256", sa.String(length=64), nullable=False),
        sa.Column("review_key_sha256", sa.String(length=64), nullable=True),
        sa.Column("review_fingerprint_sha256", sa.String(length=64), nullable=True),
        sa.Column("requested_by_actor_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by_actor_type", sa.String(length=30), nullable=False),
        sa.Column("requested_by_actor_label", sa.String(length=160), nullable=False),
        sa.Column("request_reason", sa.Text(), nullable=False),
        sa.Column("reviewed_by_actor_id", sa.Uuid(), nullable=True),
        sa.Column("reviewed_by_actor_type", sa.String(length=30), nullable=True),
        sa.Column("reviewed_by_actor_label", sa.String(length=160), nullable=True),
        sa.Column("review_notes", sa.Text(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "asset_type IN ('fdh_cabinet', 'splice_closure', "
            "'access_point', 'support_structure')",
            name="ck_network_map_asset_proposals_asset_type",
        ),
        sa.CheckConstraint(
            "operation IN ('create', 'edit', 'move')",
            name="ck_network_map_asset_proposals_operation",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'applied', 'rejected')",
            name="ck_network_map_asset_proposals_status",
        ),
        sa.CheckConstraint(
            "(operation = 'create' AND target_asset_id IS NULL "
            "AND before_values IS NULL AND source_asset_sha256 IS NULL) OR "
            "(operation IN ('edit', 'move') AND target_asset_id IS NOT NULL "
            "AND before_values IS NOT NULL AND source_asset_sha256 IS NOT NULL)",
            name="ck_network_map_asset_proposals_target_shape",
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND reviewed_by_actor_id IS NULL "
            "AND reviewed_at IS NULL AND review_notes IS NULL "
            "AND applied_at IS NULL AND result_asset_id IS NULL) OR "
            "(status = 'rejected' AND reviewed_by_actor_id IS NOT NULL "
            "AND reviewed_at IS NOT NULL AND review_notes IS NOT NULL "
            "AND applied_at IS NULL AND result_asset_id IS NULL) OR "
            "(status = 'applied' AND reviewed_by_actor_id IS NOT NULL "
            "AND reviewed_at IS NOT NULL AND review_notes IS NOT NULL "
            "AND applied_at IS NOT NULL AND result_asset_id IS NOT NULL)",
            name="ck_network_map_asset_proposals_review_shape",
        ),
        sa.CheckConstraint(
            "reviewed_by_actor_id IS NULL "
            "OR reviewed_by_actor_id <> requested_by_actor_id",
            name="ck_network_map_asset_proposals_review_separation",
        ),
        sa.CheckConstraint(
            "length(proposal_sha256) = 64 "
            "AND length(submit_key_sha256) = 64 "
            "AND length(submit_fingerprint_sha256) = 64 "
            "AND (source_asset_sha256 IS NULL "
            "OR length(source_asset_sha256) = 64) "
            "AND (review_key_sha256 IS NULL OR length(review_key_sha256) = 64) "
            "AND (review_fingerprint_sha256 IS NULL "
            "OR length(review_fingerprint_sha256) = 64)",
            name="ck_network_map_asset_proposals_digests",
        ),
        sa.UniqueConstraint(
            "submit_key_sha256",
            name="uq_network_map_asset_proposals_submit_key",
        ),
        sa.UniqueConstraint(
            "review_key_sha256",
            name="uq_network_map_asset_proposals_review_key",
        ),
    )
    op.create_index(
        "ix_network_map_asset_proposals_status",
        TABLE,
        ["status", "created_at"],
    )
    op.create_index(
        "ix_network_map_asset_proposals_target",
        TABLE,
        ["asset_type", "target_asset_id"],
    )

    bind = op.get_bind()
    if "permissions" not in sa.inspect(bind).get_table_names():
        return
    if _permission_id(bind) is None:
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
                "id": str(uuid4()),
                "key": REVIEW_PERMISSION,
                "description": REVIEW_DESCRIPTION,
                "now": now,
            },
        )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "permissions" in tables:
        permission_id = _permission_id(bind)
        if permission_id is not None:
            for table in (
                "role_permissions",
                "subscriber_permissions",
                "system_user_permissions",
            ):
                if table in tables:
                    bind.execute(
                        sa.text(f"DELETE FROM {table} WHERE permission_id = :id"),
                        {"id": permission_id},
                    )
            bind.execute(
                sa.text("DELETE FROM permissions WHERE key = :key"),
                {"key": REVIEW_PERMISSION},
            )
    op.drop_index("ix_network_map_asset_proposals_target", table_name=TABLE)
    op.drop_index("ix_network_map_asset_proposals_status", table_name=TABLE)
    op.drop_table(TABLE)
