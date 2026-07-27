"""Add durable owner-output consumer receipts.

ADR 0007 Phase 4 (expand). The transactional outbox (event_store) already
proves the producer side; this table proves the consumer side: one committed
outcome per (consumer, event_id), succeeded or explicitly terminal-failed.

Revision ID: 432_owner_output_receipts
Revises: 431_customer_subledger_postings
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "432_owner_output_receipts"
down_revision = "431_customer_subledger_postings"
branch_labels = None
depends_on = None

_OUTCOME = sa.Enum("succeeded", "terminal_failure", name="receiptoutcome")


def upgrade() -> None:
    _OUTCOME.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "owner_output_receipts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("consumer", sa.String(120), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("producer_owner", sa.String(120), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("outcome", _OUTCOME, nullable=False),
        sa.Column("effect_idempotency_key", sa.String(200), nullable=False),
        sa.Column("failure_reason", sa.Text()),
        sa.Column("command_id", postgresql.UUID(as_uuid=True)),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True)),
        sa.Column("causation_id", postgresql.UUID(as_uuid=True)),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "consumer",
            "event_id",
            name="uq_owner_output_receipt_consumer_event",
        ),
    )
    op.create_index(
        "ix_owner_output_receipt_event", "owner_output_receipts", ["event_id"]
    )
    op.create_index(
        "ix_owner_output_receipt_outcome", "owner_output_receipts", ["outcome"]
    )


def downgrade() -> None:
    op.drop_table("owner_output_receipts")
    _OUTCOME.drop(op.get_bind(), checkfirst=True)
