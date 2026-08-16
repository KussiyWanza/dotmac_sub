"""Order waiver evidence.

A waiver used to be ``sales_orders.payment_status = 'waived'`` written through
the generic order edit, with no actor, grounds or identity — and in the field
that means money arrived. This table is the decision; no payment field moves.
Historical ``waived`` values stay readable and are not backfilled here, because
inventing decision evidence that was never recorded would be a lie.

Revision ID: 532_sales_order_waivers
Revises: 531_consolidated_open_prs
Create Date: 2026-08-15
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "532_sales_order_waivers"
down_revision = "531_consolidated_open_prs"
branch_labels = None
depends_on = None

_STATE = "salesorderwaiverstate"


def upgrade() -> None:
    state = postgresql.ENUM("active", "revoked", name=_STATE)
    state.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "sales_order_waivers",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "sales_order_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sales_orders.id"),
            nullable=False,
        ),
        sa.Column(
            "state",
            postgresql.ENUM("active", "revoked", name=_STATE, create_type=False),
            nullable=False,
            server_default="active",
        ),
        # Exact money, matching the fleet NUMERIC(20,6) convention.
        sa.Column("waived_amount", sa.Numeric(20, 6), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("reason_code", sa.String(80), nullable=False),
        sa.Column("reason_text", sa.Text(), nullable=True),
        sa.Column("granted_by", sa.String(255), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("grant_idempotency_key", sa.String(255), nullable=False),
        sa.Column("grant_fingerprint", sa.String(64), nullable=False),
        sa.Column("revoked_by", sa.String(255), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoke_reason_code", sa.String(80), nullable=True),
        sa.Column("revoke_reason_text", sa.Text(), nullable=True),
        sa.Column("revoke_idempotency_key", sa.String(255), nullable=True),
        sa.Column("command_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "sales_order_id",
            "grant_idempotency_key",
            name="uq_sales_order_waivers_grant_idempotency",
        ),
    )
    op.create_index(
        "ix_sales_order_waivers_order", "sales_order_waivers", ["sales_order_id"]
    )
    op.create_index(
        "ix_sales_order_waivers_state",
        "sales_order_waivers",
        ["sales_order_id", "state"],
    )
    # At most one active waiver per order. Enforced by the database rather than
    # by the service alone, because two concurrent grants must not both win.
    op.create_index(
        "uq_sales_order_waivers_one_active",
        "sales_order_waivers",
        ["sales_order_id"],
        unique=True,
        postgresql_where=sa.text("state = 'active'"),
    )


def downgrade() -> None:
    op.drop_index("uq_sales_order_waivers_one_active", table_name="sales_order_waivers")
    op.drop_index("ix_sales_order_waivers_state", table_name="sales_order_waivers")
    op.drop_index("ix_sales_order_waivers_order", table_name="sales_order_waivers")
    op.drop_table("sales_order_waivers")
    postgresql.ENUM(name=_STATE).drop(op.get_bind(), checkfirst=True)
