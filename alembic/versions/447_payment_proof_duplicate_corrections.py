"""add durable duplicate payment-proof correction evidence

Revision ID: 447_payment_proof_corrections
Revises: 446_ont_commissioning_intents
Create Date: 2026-07-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "447_payment_proof_corrections"
down_revision: str | None = "446_ont_commissioning_intents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "ALTER TYPE paymentreversalorigin ADD VALUE IF NOT EXISTS "
            "'administrative_correction'"
        )
    op.create_table(
        "payment_proof_corrections",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("duplicate_proof_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("original_proof_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "duplicate_payment_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("payment_reversal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ledger_entry_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", sa.String(length=120), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("preview_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "duplicate_proof_id <> original_proof_id",
            name="ck_payment_proof_corrections_distinct_proofs",
        ),
        sa.ForeignKeyConstraint(
            ["duplicate_payment_id"],
            ["payments.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["duplicate_proof_id"],
            ["payment_proofs.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["ledger_entry_id"],
            ["ledger_entries.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["original_proof_id"],
            ["payment_proofs.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["payment_reversal_id"],
            ["payment_reversals.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_payment_proof_corrections_duplicate_proof_id",
        "payment_proof_corrections",
        ["duplicate_proof_id"],
        unique=True,
    )
    op.create_index(
        "uq_payment_proof_corrections_payment_reversal_id",
        "payment_proof_corrections",
        ["payment_reversal_id"],
        unique=True,
    )
    op.create_index(
        "uq_payment_proof_corrections_idempotency_key",
        "payment_proof_corrections",
        ["idempotency_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_payment_proof_corrections_idempotency_key",
        table_name="payment_proof_corrections",
    )
    op.drop_index(
        "uq_payment_proof_corrections_payment_reversal_id",
        table_name="payment_proof_corrections",
    )
    op.drop_index(
        "uq_payment_proof_corrections_duplicate_proof_id",
        table_name="payment_proof_corrections",
    )
    op.drop_table("payment_proof_corrections")
    # PostgreSQL enum labels are additive. Removing a label requires rebuilding
    # every dependent column, so the unused value remains after downgrade.
