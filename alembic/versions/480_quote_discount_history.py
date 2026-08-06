"""Add Quote-level discounts and append-only history.

Revision ID: 480_quote_discount_history
Revises: 479_inbox_lifecycle_audit
Create Date: 2026-08-05

This additive cutover does not rewrite previous Quotes or their historical
Line Item discounts. New writers stop authoring Line Item discounts and use
the Quote-level fields and evidence table instead.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "480_quote_discount_history"
down_revision: str | None = "479_inbox_lifecycle_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _install_append_only_trigger() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        """
        CREATE FUNCTION quote_discount_history_append_only() RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'quote_discount_history is append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_quote_discount_history_append_only
        BEFORE UPDATE OR DELETE ON quote_discount_history
        FOR EACH ROW EXECUTE FUNCTION quote_discount_history_append_only()
        """
    )


def upgrade() -> None:
    op.add_column("quotes", sa.Column("discount_type", sa.String(24)))
    op.add_column("quotes", sa.Column("discount_value", sa.Numeric(12, 2)))
    op.add_column(
        "quotes",
        sa.Column(
            "discount_amount",
            sa.Numeric(12, 2),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column("quotes", sa.Column("discount_reason", sa.Text()))
    op.add_column(
        "quotes",
        sa.Column(
            "discount_applied_by_system_user_id",
            postgresql.UUID(as_uuid=True),
        ),
    )
    op.add_column(
        "quotes", sa.Column("discount_applied_at", sa.DateTime(timezone=True))
    )
    op.add_column(
        "quotes",
        sa.Column(
            "discount_revision", sa.Integer(), nullable=False, server_default="0"
        ),
    )
    op.create_foreign_key(
        "fk_quotes_discount_applied_by_system_user",
        "quotes",
        "system_users",
        ["discount_applied_by_system_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_quotes_discount_revision_nonnegative",
        "quotes",
        "discount_revision >= 0",
    )
    op.create_check_constraint(
        "ck_quotes_discount_current_state",
        "quotes",
        "(discount_type IS NULL AND discount_value IS NULL AND "
        "discount_amount = 0 AND discount_reason IS NULL AND "
        "discount_applied_by_system_user_id IS NULL "
        "AND discount_applied_at IS NULL) OR "
        "(discount_type IN ('percentage', 'fixed_amount') AND "
        "discount_value > 0 AND discount_amount > 0 AND "
        "discount_amount <= subtotal AND "
        "discount_revision > 0 AND "
        "discount_applied_by_system_user_id IS NOT NULL AND "
        "discount_applied_at IS NOT NULL)",
    )

    op.create_table(
        "quote_discount_history",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False
        ),
        sa.Column(
            "quote_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("quotes.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(20), nullable=False),
        sa.Column("discount_type", sa.String(24), nullable=False),
        sa.Column("discount_value", sa.Numeric(12, 2), nullable=False),
        sa.Column("discount_amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("original_subtotal", sa.Numeric(12, 2), nullable=False),
        sa.Column("discounted_subtotal", sa.Numeric(12, 2), nullable=False),
        sa.Column("tax_total", sa.Numeric(12, 2), nullable=False),
        sa.Column("total_after_discount", sa.Numeric(12, 2), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column(
            "actor_system_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("system_users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("command_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("command_fingerprint", sa.String(64), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "quote_id", "revision", name="uq_quote_discount_history_revision"
        ),
        sa.UniqueConstraint("command_id", name="uq_quote_discount_history_command_id"),
        sa.CheckConstraint(
            "revision > 0", name="ck_quote_discount_history_revision_positive"
        ),
        sa.CheckConstraint(
            "action IN ('applied', 'changed', 'removed')",
            name="ck_quote_discount_history_action",
        ),
        sa.CheckConstraint(
            "discount_type IN ('percentage', 'fixed_amount')",
            name="ck_quote_discount_history_type",
        ),
        sa.CheckConstraint(
            "discount_value > 0 AND discount_amount > 0 AND "
            "discount_amount <= original_subtotal AND discounted_subtotal >= 0",
            name="ck_quote_discount_history_amounts",
        ),
    )
    op.create_index(
        "ix_quote_discount_history_applied_at",
        "quote_discount_history",
        ["applied_at"],
    )
    op.create_index(
        "ix_quote_discount_history_actor",
        "quote_discount_history",
        ["actor_system_user_id"],
    )
    op.create_index(
        "ix_quote_discount_history_type",
        "quote_discount_history",
        ["discount_type"],
    )
    _install_append_only_trigger()

    op.add_column("sales_orders", sa.Column("discount_type", sa.String(24)))
    op.add_column("sales_orders", sa.Column("discount_value", sa.Numeric(12, 2)))
    op.add_column(
        "sales_orders",
        sa.Column(
            "discount_amount",
            sa.Numeric(12, 2),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("sales_orders", "discount_amount")
    op.drop_column("sales_orders", "discount_value")
    op.drop_column("sales_orders", "discount_type")

    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_quote_discount_history_append_only "
            "ON quote_discount_history"
        )
        op.execute("DROP FUNCTION IF EXISTS quote_discount_history_append_only()")
    op.drop_index("ix_quote_discount_history_type", table_name="quote_discount_history")
    op.drop_index(
        "ix_quote_discount_history_actor", table_name="quote_discount_history"
    )
    op.drop_index(
        "ix_quote_discount_history_applied_at",
        table_name="quote_discount_history",
    )
    op.drop_table("quote_discount_history")

    op.drop_constraint("ck_quotes_discount_current_state", "quotes", type_="check")
    op.drop_constraint(
        "ck_quotes_discount_revision_nonnegative", "quotes", type_="check"
    )
    op.drop_constraint(
        "fk_quotes_discount_applied_by_system_user", "quotes", type_="foreignkey"
    )
    op.drop_column("quotes", "discount_revision")
    op.drop_column("quotes", "discount_applied_at")
    op.drop_column("quotes", "discount_applied_by_system_user_id")
    op.drop_column("quotes", "discount_reason")
    op.drop_column("quotes", "discount_amount")
    op.drop_column("quotes", "discount_value")
    op.drop_column("quotes", "discount_type")
