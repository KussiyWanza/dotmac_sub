"""Typed incident-to-ticket links.

OUTAGE_SLA_SPINE ticket/watcher slice: exactly one canonical infrastructure
ticket per incident (partial unique index on role='infrastructure'), any
number of complaint links (unique per pair), with provenance, external CRM
identity, reconciliation state, and scope-revision context. Supersedes the
OutageIncident.crm_ticket_id placeholder, which stays untouched and unused.
Network recovery emits evidence only; nothing transitions tickets here.

Expand-only: one new table, no backfill. Lock budget: trivial CREATE TABLE.
Downgrade drops the table.

Revision ID: 461_outage_incident_ticket_links
Revises: 460_network_maintenance_windows
Create Date: 2026-08-02
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "461_outage_incident_ticket_links"
down_revision = "460_network_maintenance_windows"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "outage_incident_ticket_links",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False
        ),
        sa.Column(
            "incident_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("outage_incidents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "ticket_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("support_tickets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("linked_by", sa.String(length=120), nullable=True),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("external_ref", sa.String(length=120), nullable=True),
        sa.Column("reconciliation_state", sa.String(length=20), nullable=False),
        sa.Column("scope_revision_sequence", sa.Integer(), nullable=True),
        sa.UniqueConstraint(
            "incident_id",
            "ticket_id",
            name="uq_outage_incident_ticket_links_pair",
        ),
    )
    op.create_index(
        "uq_outage_incident_ticket_links_infrastructure",
        "outage_incident_ticket_links",
        ["incident_id"],
        unique=True,
        postgresql_where=sa.text("role = 'infrastructure'"),
        sqlite_where=sa.text("role = 'infrastructure'"),
    )
    op.create_index(
        "ix_outage_incident_ticket_links_ticket",
        "outage_incident_ticket_links",
        ["ticket_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_outage_incident_ticket_links_ticket",
        table_name="outage_incident_ticket_links",
    )
    op.drop_index(
        "uq_outage_incident_ticket_links_infrastructure",
        table_name="outage_incident_ticket_links",
    )
    op.drop_table("outage_incident_ticket_links")
