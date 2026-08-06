"""Add Invoice-level discounts and append-only history.

Revision ID: 483_invoice_discount_history
Revises: 482_sla_period_score_revisions
Create Date: 2026-08-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "483_invoice_discount_history"
down_revision: str | None = "482_sla_period_score_revisions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _install_append_only_trigger() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        """
        CREATE FUNCTION invoice_discount_history_append_only() RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'invoice_discount_history is append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_invoice_discount_history_append_only
        BEFORE UPDATE OR DELETE ON invoice_discount_history
        FOR EACH ROW EXECUTE FUNCTION invoice_discount_history_append_only()
        """
    )


def upgrade() -> None:
    op.add_column("invoices", sa.Column("discount_type", sa.String(24)))
    op.add_column("invoices", sa.Column("discount_value", sa.Numeric(12, 2)))
    op.add_column(
        "invoices",
        sa.Column(
            "discount_amount",
            sa.Numeric(12, 2),
            nullable=False,
            server_default="0.00",
        ),
    )
    op.add_column("invoices", sa.Column("discount_reason", sa.Text()))
    op.add_column("invoices", sa.Column("discount_source", sa.String(20)))
    op.add_column(
        "invoices",
        sa.Column("discount_source_quote_id", postgresql.UUID(as_uuid=True)),
    )
    op.add_column(
        "invoices",
        sa.Column("discount_applied_by_system_user_id", postgresql.UUID(as_uuid=True)),
    )
    op.add_column(
        "invoices", sa.Column("discount_applied_at", sa.DateTime(timezone=True))
    )
    op.add_column(
        "invoices",
        sa.Column(
            "discount_revision", sa.Integer(), nullable=False, server_default="0"
        ),
    )
    op.create_foreign_key(
        "fk_invoices_discount_source_quote_id",
        "invoices",
        "quotes",
        ["discount_source_quote_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_invoices_discount_applied_by_system_user_id",
        "invoices",
        "system_users",
        ["discount_applied_by_system_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_invoices_discount_revision_nonnegative",
        "invoices",
        "discount_revision >= 0",
    )
    op.create_check_constraint(
        "ck_invoices_discount_current_state",
        "invoices",
        "(discount_type IS NULL AND discount_value IS NULL AND "
        "discount_amount = 0 AND discount_reason IS NULL AND "
        "discount_source IS NULL AND discount_source_quote_id IS NULL AND "
        "discount_applied_by_system_user_id IS NULL AND discount_applied_at IS NULL) "
        "OR (discount_type IN ('percentage', 'fixed_amount') AND "
        "discount_value > 0 AND discount_amount > 0 AND discount_amount <= subtotal "
        "AND discount_revision > 0 AND discount_source IN ('manual', 'quote') AND "
        "discount_applied_by_system_user_id IS NOT NULL AND "
        "discount_applied_at IS NOT NULL AND "
        "((discount_source = 'manual' AND discount_source_quote_id IS NULL) OR "
        "(discount_source = 'quote' AND discount_source_quote_id IS NOT NULL)))",
    )

    op.create_table(
        "invoice_discount_history",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False
        ),
        sa.Column("invoice_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(20), nullable=False),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("source_quote_id", postgresql.UUID(as_uuid=True)),
        sa.Column("discount_type", sa.String(24), nullable=False),
        sa.Column("discount_value", sa.Numeric(12, 2), nullable=False),
        sa.Column("discount_amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("original_subtotal", sa.Numeric(12, 2), nullable=False),
        sa.Column("discounted_subtotal", sa.Numeric(12, 2), nullable=False),
        sa.Column("tax_total", sa.Numeric(12, 2), nullable=False),
        sa.Column("total_after_discount", sa.Numeric(12, 2), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column(
            "actor_system_user_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("command_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("command_fingerprint", sa.String(64), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["invoice_id"], ["invoices.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["source_quote_id"], ["quotes.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["actor_system_user_id"], ["system_users.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "invoice_id", "revision", name="uq_invoice_discount_history_revision"
        ),
        sa.UniqueConstraint(
            "command_id", name="uq_invoice_discount_history_command_id"
        ),
        sa.CheckConstraint(
            "revision > 0", name="ck_invoice_discount_history_revision_positive"
        ),
        sa.CheckConstraint(
            "action IN ('applied', 'changed', 'removed', 'inherited')",
            name="ck_invoice_discount_history_action",
        ),
        sa.CheckConstraint(
            "discount_type IN ('percentage', 'fixed_amount')",
            name="ck_invoice_discount_history_type",
        ),
        sa.CheckConstraint(
            "source IN ('manual', 'quote')",
            name="ck_invoice_discount_history_source",
        ),
        sa.CheckConstraint(
            "(source = 'manual' AND source_quote_id IS NULL) OR "
            "(source = 'quote' AND source_quote_id IS NOT NULL)",
            name="ck_invoice_discount_history_source_quote",
        ),
        sa.CheckConstraint(
            "discount_value > 0 AND discount_amount > 0 AND "
            "discount_amount <= original_subtotal AND discounted_subtotal >= 0",
            name="ck_invoice_discount_history_amounts",
        ),
    )
    op.create_index(
        "ix_invoice_discount_history_applied_at",
        "invoice_discount_history",
        ["applied_at"],
    )
    op.create_index(
        "ix_invoice_discount_history_actor",
        "invoice_discount_history",
        ["actor_system_user_id"],
    )
    op.create_index(
        "ix_invoice_discount_history_type",
        "invoice_discount_history",
        ["discount_type"],
    )
    op.create_index(
        "ix_invoice_discount_history_source_quote",
        "invoice_discount_history",
        ["source_quote_id"],
    )
    _install_append_only_trigger()


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_invoice_discount_history_append_only "
            "ON invoice_discount_history"
        )
        op.execute("DROP FUNCTION IF EXISTS invoice_discount_history_append_only()")
    op.drop_table("invoice_discount_history")
    op.drop_constraint("ck_invoices_discount_current_state", "invoices", type_="check")
    op.drop_constraint(
        "ck_invoices_discount_revision_nonnegative", "invoices", type_="check"
    )
    op.drop_constraint(
        "fk_invoices_discount_applied_by_system_user_id",
        "invoices",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_invoices_discount_source_quote_id", "invoices", type_="foreignkey"
    )
    op.drop_column("invoices", "discount_revision")
    op.drop_column("invoices", "discount_applied_at")
    op.drop_column("invoices", "discount_applied_by_system_user_id")
    op.drop_column("invoices", "discount_source_quote_id")
    op.drop_column("invoices", "discount_source")
    op.drop_column("invoices", "discount_reason")
    op.drop_column("invoices", "discount_amount")
    op.drop_column("invoices", "discount_value")
    op.drop_column("invoices", "discount_type")
