"""Immutable outage incident scope/audience revisions.

OUTAGE_SLA_SPINE §3: the incident row's root remains the mutable latest
projection, but the downtime ledger needs history — which scope an incident
pointed at, exactly which subscriptions were in its audience, and when that
changed. ``outage_scope_revisions`` is append-only with a monotonic
``sequence`` unique per incident so concurrent writers cannot fork history;
``outage_scope_revision_members`` stores the exact audience per revision as
entered/retained/left deltas. Member rows carry no FK to subscriptions:
history must outlive deleted subscriptions (same rationale as
outage_notification_dispatches and availability_snapshots).

Expand-only: two new tables, no backfill (historical incidents predate the
ledger and stay without revisions — the ledger backfill policy labels those
periods estimated/unavailable rather than manufacturing exact history). Lock
budget: trivial CREATE TABLE. Downgrade drops both tables.

Revision ID: 458_outage_scope_revisions
Revises: 457_customer_subledger_opening_positions
Create Date: 2026-08-02
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "458_outage_scope_revisions"
down_revision = "457_customer_subledger_opening_positions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "outage_scope_revisions",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False
        ),
        sa.Column(
            "incident_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("outage_incidents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.String(length=40), nullable=False),
        sa.Column("actor", sa.String(length=120), nullable=True),
        sa.Column("old_scope_type", sa.String(length=20), nullable=True),
        sa.Column("old_scope_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("new_scope_type", sa.String(length=20), nullable=False),
        sa.Column("new_scope_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("membership_token", sa.String(length=64), nullable=False),
        sa.Column("member_count", sa.Integer(), nullable=False),
        sa.Column("entered_count", sa.Integer(), nullable=False),
        sa.Column("left_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "incident_id",
            "sequence",
            name="uq_outage_scope_revisions_incident_sequence",
        ),
    )
    op.create_index(
        "ix_outage_scope_revisions_incident",
        "outage_scope_revisions",
        ["incident_id", "sequence"],
    )
    op.create_table(
        "outage_scope_revision_members",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False
        ),
        sa.Column(
            "revision_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("outage_scope_revisions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("subscription_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("membership", sa.String(length=10), nullable=False),
        sa.UniqueConstraint(
            "revision_id",
            "subscription_id",
            name="uq_outage_scope_revision_members_row",
        ),
    )
    op.create_index(
        "ix_outage_scope_revision_members_subscription",
        "outage_scope_revision_members",
        ["subscription_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_outage_scope_revision_members_subscription",
        table_name="outage_scope_revision_members",
    )
    op.drop_table("outage_scope_revision_members")
    op.drop_index(
        "ix_outage_scope_revisions_incident", table_name="outage_scope_revisions"
    )
    op.drop_table("outage_scope_revisions")
