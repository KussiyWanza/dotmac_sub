"""Add finite sales-order funding gates and ERP billing export evidence.

ADR 0007 Phases 6 and 7 (expand). Tables only: SalesOrder.amount_paid and
current fulfillment reads are untouched, and export rows are transport
evidence with no accounting authority.

Revision ID: 434_sales_funding_erp_exports
Revises: 433_durable_timers_collections_cases
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "434_sales_funding_erp_exports"
down_revision = "433_durable_timers_collections_cases"
branch_labels = None
depends_on = None

_AUTHORITY = sa.Enum("shadow", "authoritative", name="billingrecordauthority")
_GATE_STATE = sa.Enum("pending", "funded", name="fundinggatestate")
_ERP_FLOW = sa.Enum(
    "invoice",
    "credit_note",
    "payment",
    "refund_or_reversal",
    "tax_withholding",
    "correction",
    name="erpbillingflow",
)
_ERP_STATUS = sa.Enum(
    "pending", "delivered", "acknowledged", "rejected", name="erpexportstatus"
)


def upgrade() -> None:
    bind = op.get_bind()
    _GATE_STATE.create(bind, checkfirst=True)
    _ERP_FLOW.create(bind, checkfirst=True)
    _ERP_STATUS.create(bind, checkfirst=True)

    op.create_table(
        "sales_order_funding_gates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "sales_order_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sales_orders.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("authority", _AUTHORITY, nullable=False, server_default="shadow"),
        sa.Column("state", _GATE_STATE, nullable=False),
        sa.Column("funded_at", sa.DateTime(timezone=True)),
        sa.Column("funded_event_id", postgresql.UUID(as_uuid=True)),
        sa.Column("command_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("sales_order_id", name="uq_sales_order_funding_gate"),
    )
    op.create_index(
        "ix_sales_order_funding_gate_state", "sales_order_funding_gates", ["state"]
    )

    op.create_table(
        "sales_order_funding_obligations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "gate_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sales_order_funding_gates.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "obligation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("billing_obligations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("resolved", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("resolution_kind", sa.String(60)),
        sa.Column("resolved_event_id", postgresql.UUID(as_uuid=True)),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "gate_id", "obligation_id", name="uq_sales_order_funding_obligation"
        ),
    )
    op.create_index(
        "ix_sales_order_funding_obligation_gate",
        "sales_order_funding_obligations",
        ["gate_id", "resolved"],
    )

    op.create_table(
        "erp_billing_exports",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("flow", _ERP_FLOW, nullable=False),
        sa.Column("source_kind", sa.String(80), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_event_id", postgresql.UUID(as_uuid=True)),
        sa.Column("payload_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("status", _ERP_STATUS, nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text()),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True)),
        sa.Column("erp_reference", sa.String(160)),
        sa.Column("command_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_erp_billing_export_idempotency"
        ),
    )
    op.create_index("ix_erp_billing_export_status", "erp_billing_exports", ["status"])
    op.create_index(
        "ix_erp_billing_export_source",
        "erp_billing_exports",
        ["source_kind", "source_id"],
    )


def downgrade() -> None:
    op.drop_table("erp_billing_exports")
    op.drop_table("sales_order_funding_obligations")
    op.drop_table("sales_order_funding_gates")
    bind = op.get_bind()
    _ERP_STATUS.drop(bind, checkfirst=True)
    _ERP_FLOW.drop(bind, checkfirst=True)
    _GATE_STATE.drop(bind, checkfirst=True)
