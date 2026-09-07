"""Add durable support CSAT requests.

Revision ID: 576_support_csat_requests
Revises: 575_invoice_pdf_payment_presentment_paystack_default
Create Date: 2026-09-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "576_support_csat_requests"
down_revision: str | None = "575_invoice_pdf_payment_presentment_paystack_default"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "support_csat_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_reference", sa.String(length=120), nullable=True),
        sa.Column("resolution_cycle_key", sa.String(length=180), nullable=False),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("customer_account_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("customer_person_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("customer_display_name", sa.String(length=180), nullable=True),
        sa.Column("customer_email", sa.String(length=255), nullable=True),
        sa.Column("agent_person_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("agent_display_name", sa.String(length=180), nullable=True),
        sa.Column("service_team_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("service_team_name", sa.String(length=180), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("resolution_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("submitted_by", sa.String(length=180), nullable=True),
        sa.Column("submission_channel", sa.String(length=40), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source_type IN ('support_ticket', 'inbox_conversation')",
            name="ck_support_csat_source_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'submitted', 'expired')",
            name="ck_support_csat_status",
        ),
        sa.CheckConstraint(
            "rating IS NULL OR (rating >= 1 AND rating <= 5)",
            name="ck_support_csat_rating",
        ),
        sa.CheckConstraint(
            "(status = 'submitted' AND rating IS NOT NULL AND submitted_at IS NOT NULL) "
            "OR (status <> 'submitted' AND submitted_at IS NULL)",
            name="ck_support_csat_submission_state",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_type",
            "source_id",
            "resolution_cycle_key",
            name="uq_support_csat_resolution_cycle",
        ),
    )
    op.create_index(
        "ix_support_csat_source",
        "support_csat_requests",
        ["source_type", "source_id"],
    )
    op.create_index(
        "ix_support_csat_status_resolution",
        "support_csat_requests",
        ["status", "resolution_at"],
    )
    op.create_index(
        "ix_support_csat_submitted", "support_csat_requests", ["submitted_at"]
    )
    op.create_index(
        "ix_support_csat_customer", "support_csat_requests", ["customer_id"]
    )
    op.create_index(
        "ix_support_csat_agent_submitted",
        "support_csat_requests",
        ["agent_person_id", "submitted_at"],
    )
    op.create_index(
        "ix_support_csat_team_submitted",
        "support_csat_requests",
        ["service_team_id", "submitted_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_support_csat_team_submitted", table_name="support_csat_requests")
    op.drop_index("ix_support_csat_agent_submitted", table_name="support_csat_requests")
    op.drop_index("ix_support_csat_customer", table_name="support_csat_requests")
    op.drop_index("ix_support_csat_submitted", table_name="support_csat_requests")
    op.drop_index(
        "ix_support_csat_status_resolution", table_name="support_csat_requests"
    )
    op.drop_index("ix_support_csat_source", table_name="support_csat_requests")
    op.drop_table("support_csat_requests")
