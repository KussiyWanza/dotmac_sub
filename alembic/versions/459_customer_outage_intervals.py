"""Per-subscription customer outage interval ledger.

OUTAGE_SLA_SPINE §2/§7: ``network.customer_outage_accrual`` stores one
service-impact interval per incident and subscription. ``ended_at`` stays a
provisional first-healthy-observation stamp until ``finalized_at`` marks the
sustained-recovery hold passed; the partial unique index enforces at most one
open interval per (incident, subscription) so duplicate or overlapping
accrual is impossible at the database. No FKs: downtime history must outlive
deleted incidents/subscriptions (same rationale as the dispatch audit rows).

Expand-only: one new table, no backfill — historical incidents predate the
ledger and any later backfill labels its intervals estimated/unavailable
instead of manufacturing exact history. Lock budget: trivial CREATE TABLE.
Downgrade drops the table.

Revision ID: 459_customer_outage_intervals
Revises: 458_outage_scope_revisions
Create Date: 2026-08-02
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "459_customer_outage_intervals"
down_revision = "458_outage_scope_revisions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "customer_outage_intervals",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False
        ),
        sa.Column("incident_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subscription_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("state", sa.String(length=30), nullable=False),
        sa.Column("quality", sa.String(length=12), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_evidence_ref", sa.String(length=200), nullable=True),
        sa.Column("recovery_evidence_ref", sa.String(length=200), nullable=True),
        sa.Column("scope_revision_sequence", sa.Integer(), nullable=False),
        sa.Column("exclusion_candidate", sa.String(length=60), nullable=True),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_customer_outage_intervals_idempotency"
        ),
    )
    op.create_index(
        "uq_customer_outage_intervals_open",
        "customer_outage_intervals",
        ["incident_id", "subscription_id"],
        unique=True,
        postgresql_where=sa.text("ended_at IS NULL"),
        sqlite_where=sa.text("ended_at IS NULL"),
    )
    op.create_index(
        "ix_customer_outage_intervals_subscription",
        "customer_outage_intervals",
        ["subscription_id", "started_at"],
    )
    op.create_index(
        "ix_customer_outage_intervals_incident",
        "customer_outage_intervals",
        ["incident_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_customer_outage_intervals_incident",
        table_name="customer_outage_intervals",
    )
    op.drop_index(
        "ix_customer_outage_intervals_subscription",
        table_name="customer_outage_intervals",
    )
    op.drop_index(
        "uq_customer_outage_intervals_open",
        table_name="customer_outage_intervals",
    )
    op.drop_table("customer_outage_intervals")
