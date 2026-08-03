"""Customer-facing outage communication decisions.

OUTAGE_SLA_SPINE communications slice: one append-only row per (incident,
customer, stage) message the communications owner decided to make — including
dry-run plans and suppressed recipients, because the row set IS the recovery
cohort and the duplicate-suppression key. ``dedupe_key`` is unique so a
replayed lifecycle event or a double-confirmed console converges on one
message. No FK on subscriber/subscription: the communication audit must
outlive a deleted customer, exactly like outage_notification_dispatches.

Expand-only: one new table, no backfill — historical incidents are not
retro-notified. Lock budget: trivial CREATE TABLE. Downgrade drops the table.

Revision ID: 463_outage_customer_notices
Revises: 462_quote_acceptance_sales_conversion
Create Date: 2026-08-02
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "463_outage_customer_notices"
down_revision = "462_quote_acceptance_sales_conversion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "outage_customer_notices",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False
        ),
        sa.Column(
            "incident_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("outage_incidents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("subscriber_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("stage", sa.String(length=20), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("subscription_ids", sa.JSON(), nullable=True),
        sa.Column("impact_state", sa.String(length=30), nullable=True),
        sa.Column("scope_revision_sequence", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("reason_code", sa.String(length=60), nullable=True),
        sa.Column(
            "communication_intent_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("recipient", sa.String(length=255), nullable=True),
        sa.Column("subject", sa.String(length=255), nullable=True),
        sa.Column("dedupe_key", sa.String(length=200), nullable=False),
        sa.Column("actor", sa.String(length=120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("dedupe_key", name="uq_outage_customer_notices_dedupe"),
    )
    op.create_index(
        "ix_outage_customer_notices_incident_stage",
        "outage_customer_notices",
        ["incident_id", "subscriber_id", "stage"],
    )
    op.create_index(
        "ix_outage_customer_notices_subscriber",
        "outage_customer_notices",
        ["subscriber_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_outage_customer_notices_subscriber",
        table_name="outage_customer_notices",
    )
    op.drop_index(
        "ix_outage_customer_notices_incident_stage",
        table_name="outage_customer_notices",
    )
    op.drop_table("outage_customer_notices")
