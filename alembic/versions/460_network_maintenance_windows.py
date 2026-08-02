"""Planned maintenance windows.

OUTAGE_SLA_SPINE §5: ``network.maintenance_lifecycle`` owns the planned
maintenance lifecycle (draft/approved/announced/in_progress/completed/
canceled/overrun) with the seven-day notice rule, audience tokens resolved at
announce and re-resolved at begin, and the overrun-to-outage handoff. Only a
properly announced window is SLA-excludable, and only inside its planned
bounds.

Expand-only: one new table, no backfill. Lock budget: trivial CREATE TABLE.
Downgrade drops the table.

Revision ID: 460_network_maintenance_windows
Revises: 459_customer_outage_intervals
Create Date: 2026-08-02
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "460_network_maintenance_windows"
down_revision = "459_customer_outage_intervals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "network_maintenance_windows",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False
        ),
        sa.Column("scope_type", sa.String(length=20), nullable=False),
        sa.Column("scope_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("planned_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("planned_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("announced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("actual_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("actual_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.String(length=200), nullable=False),
        sa.Column("owner", sa.String(length=120), nullable=False),
        sa.Column("approved_by", sa.String(length=120), nullable=True),
        sa.Column("expected_impact", sa.Text(), nullable=True),
        sa.Column("customer_message", sa.Text(), nullable=True),
        sa.Column("backout_plan", sa.Text(), nullable=True),
        sa.Column("audience_token", sa.String(length=64), nullable=True),
        sa.Column("audience_count", sa.Integer(), nullable=False),
        sa.Column(
            "linked_outage_incident_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_network_maintenance_windows_scope",
        "network_maintenance_windows",
        ["scope_type", "scope_id", "planned_start"],
    )
    op.create_index(
        "ix_network_maintenance_windows_status",
        "network_maintenance_windows",
        ["status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_network_maintenance_windows_status",
        table_name="network_maintenance_windows",
    )
    op.drop_index(
        "ix_network_maintenance_windows_scope",
        table_name="network_maintenance_windows",
    )
    op.drop_table("network_maintenance_windows")
