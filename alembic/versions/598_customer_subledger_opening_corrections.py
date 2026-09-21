"""Add append-only customer-subledger opening corrections.

Revision ID: 598_opening_corrections
Revises: 597_prepaid_draft_repair_permission
Create Date: 2026-09-12
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "598_opening_corrections"
down_revision: str | None = "597_prepaid_draft_repair_permission"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PERMISSION_KEY = "billing:customer_subledger_opening:correct"


def _seed_permission() -> None:
    bind = op.get_bind()
    if not {"permissions", "roles", "role_permissions"}.issubset(
        sa.inspect(bind).get_table_names()
    ):
        return
    metadata = sa.MetaData()
    permissions = sa.Table("permissions", metadata, autoload_with=bind)
    roles = sa.Table("roles", metadata, autoload_with=bind)
    role_permissions = sa.Table("role_permissions", metadata, autoload_with=bind)
    permission_id = bind.execute(
        sa.select(permissions.c.id).where(permissions.c.key == _PERMISSION_KEY)
    ).scalar_one_or_none()
    if permission_id is None:
        permission_id = uuid4()
        now = datetime.now(UTC)
        bind.execute(
            permissions.insert().values(
                id=permission_id,
                key=_PERMISSION_KEY,
                description="Correct one reviewed immutable customer opening balance",
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
    linked = bind.execute(
        sa.select(role_permissions.c.id).where(
            role_permissions.c.role_id == admin_id,
            role_permissions.c.permission_id == permission_id,
        )
    ).scalar_one_or_none()
    if linked is None:
        bind.execute(
            role_permissions.insert().values(
                id=uuid4(), role_id=admin_id, permission_id=permission_id
            )
        )


def _unseed_permission() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "permissions" not in tables:
        return
    permission_id = bind.execute(
        sa.text("SELECT id FROM permissions WHERE key = :key"),
        {"key": _PERMISSION_KEY},
    ).scalar_one_or_none()
    if permission_id is None:
        return
    if "role_permissions" in tables:
        bind.execute(
            sa.text("DELETE FROM role_permissions WHERE permission_id = :id"),
            {"id": permission_id},
        )
    bind.execute(
        sa.text("DELETE FROM permissions WHERE id = :id"), {"id": permission_id}
    )


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "ALTER TYPE postingcommandkind ADD VALUE IF NOT EXISTS "
            "'opening_position_correction'"
        )
    op.create_table(
        "customer_subledger_opening_corrections",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("opening_position_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("previous_opening_amount", sa.Numeric(18, 4), nullable=False),
        sa.Column("corrected_opening_amount", sa.Numeric(18, 4), nullable=False),
        sa.Column("delta", sa.Numeric(18, 4), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("review_reference", sa.Text(), nullable=False),
        sa.Column("preview_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column("applied_by", sa.String(length=160), nullable=False),
        sa.Column(
            "authorized_system_user_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("command_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(currency) = 3 AND currency = upper(currency)",
            name="ck_subledger_opening_correction_currency",
        ),
        sa.CheckConstraint(
            "delta <> 0", name="ck_subledger_opening_correction_nonzero"
        ),
        sa.CheckConstraint(
            "corrected_opening_amount = previous_opening_amount + delta",
            name="ck_subledger_opening_correction_exact_delta",
        ),
        sa.CheckConstraint(
            "length(preview_fingerprint) = 64",
            name="ck_subledger_opening_correction_hash",
        ),
        sa.ForeignKeyConstraint(
            ["opening_position_id"],
            ["customer_subledger_opening_positions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["subscribers.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["authorized_system_user_id"], ["system_users.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_subledger_opening_correction_key"
        ),
    )
    op.create_index(
        "ix_subledger_opening_correction_opening",
        "customer_subledger_opening_corrections",
        ["opening_position_id"],
    )
    op.create_index(
        "ix_subledger_opening_correction_account",
        "customer_subledger_opening_corrections",
        ["account_id", "currency"],
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION subledger_opening_correction_append_only()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'Customer subledger opening corrections are append-only';
            END;
            $$ LANGUAGE plpgsql;

            CREATE TRIGGER customer_subledger_opening_corrections_append_only
            BEFORE UPDATE OR DELETE ON customer_subledger_opening_corrections
            FOR EACH ROW EXECUTE FUNCTION subledger_opening_correction_append_only();
            """
        )
    _seed_permission()


def downgrade() -> None:
    _unseed_permission()
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            DROP TRIGGER IF EXISTS customer_subledger_opening_corrections_append_only
                ON customer_subledger_opening_corrections;
            DROP FUNCTION IF EXISTS subledger_opening_correction_append_only();
            """
        )
    op.drop_index(
        "ix_subledger_opening_correction_account",
        table_name="customer_subledger_opening_corrections",
    )
    op.drop_index(
        "ix_subledger_opening_correction_opening",
        table_name="customer_subledger_opening_corrections",
    )
    op.drop_table("customer_subledger_opening_corrections")
