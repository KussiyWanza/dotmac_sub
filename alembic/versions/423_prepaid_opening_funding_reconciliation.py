"""Add typed prepaid opening-funding reconciliation evidence.

Revision ID: 423_prepaid_opening_funding_reconciliation
Revises: 422_conversation_ticket_handoff

The reviewed prepaid opening position is authoritative funding but is not a
Payment. These additive tables preserve its invoice consumption provenance and
make mixed-source draft reconciliation exceptions durable and operator-visible.
No historical rows are inferred or backfilled by this migration.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "423_prepaid_opening_funding_reconciliation"
down_revision = "422_conversation_ticket_handoff"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "prepaid_opening_funding_consumptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("baseline_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("invoice_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ledger_entry_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("approval_evidence_ref", sa.Text(), nullable=False),
        sa.Column("approval_actor", sa.String(length=120), nullable=False),
        sa.Column("reconciliation_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "amount > 0",
            name="ck_prepaid_opening_consumption_positive_amount",
        ),
        sa.CheckConstraint(
            "length(currency) = 3 AND currency = upper(currency)",
            name="ck_prepaid_opening_consumption_currency",
        ),
        sa.CheckConstraint(
            "length(reconciliation_fingerprint) = 64",
            name="ck_prepaid_opening_consumption_fingerprint",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["subscribers.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["baseline_id"],
            ["prepaid_funding_baselines.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["invoice_id"], ["invoices.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["ledger_entry_id"], ["ledger_entries.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_prepaid_opening_consumption_invoice",
        "prepaid_opening_funding_consumptions",
        ["invoice_id"],
        unique=True,
    )
    op.create_index(
        "uq_prepaid_opening_consumption_ledger",
        "prepaid_opening_funding_consumptions",
        ["ledger_entry_id"],
        unique=True,
    )
    op.create_index(
        "uq_prepaid_opening_consumption_idempotency",
        "prepaid_opening_funding_consumptions",
        ["idempotency_key"],
        unique=True,
    )
    op.create_index(
        "ix_prepaid_opening_consumption_baseline",
        "prepaid_opening_funding_consumptions",
        ["baseline_id", "created_at"],
    )

    op.create_table(
        "prepaid_draft_reconciliation_exceptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("invoice_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default="open",
            nullable=False,
        ),
        sa.Column("reason", sa.String(length=80), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("required_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("payment_backed_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("opening_funding_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("preview_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("alert_fingerprint", sa.String(length=160), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="1", nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "required_amount > 0",
            name="ck_prepaid_draft_exception_required_amount",
        ),
        sa.CheckConstraint(
            "payment_backed_amount >= 0 AND opening_funding_amount >= 0",
            name="ck_prepaid_draft_exception_nonnegative_sources",
        ),
        sa.CheckConstraint(
            "status IN ('open', 'resolved')",
            name="ck_prepaid_draft_exception_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 1",
            name="ck_prepaid_draft_exception_attempt_count",
        ),
        sa.CheckConstraint(
            "length(currency) = 3 AND currency = upper(currency)",
            name="ck_prepaid_draft_exception_currency",
        ),
        sa.CheckConstraint(
            "length(preview_fingerprint) = 64",
            name="ck_prepaid_draft_exception_fingerprint",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["subscribers.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["invoice_id"], ["invoices.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_prepaid_draft_exception_invoice",
        "prepaid_draft_reconciliation_exceptions",
        ["invoice_id"],
        unique=True,
    )
    op.create_index(
        "ix_prepaid_draft_exception_status_created",
        "prepaid_draft_reconciliation_exceptions",
        ["status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_prepaid_draft_exception_status_created",
        table_name="prepaid_draft_reconciliation_exceptions",
    )
    op.drop_index(
        "uq_prepaid_draft_exception_invoice",
        table_name="prepaid_draft_reconciliation_exceptions",
    )
    op.drop_table("prepaid_draft_reconciliation_exceptions")

    op.drop_index(
        "ix_prepaid_opening_consumption_baseline",
        table_name="prepaid_opening_funding_consumptions",
    )
    op.drop_index(
        "uq_prepaid_opening_consumption_idempotency",
        table_name="prepaid_opening_funding_consumptions",
    )
    op.drop_index(
        "uq_prepaid_opening_consumption_ledger",
        table_name="prepaid_opening_funding_consumptions",
    )
    op.drop_index(
        "uq_prepaid_opening_consumption_invoice",
        table_name="prepaid_opening_funding_consumptions",
    )
    op.drop_table("prepaid_opening_funding_consumptions")
