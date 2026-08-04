"""add Quote PDF exports and email delivery requests

Revision ID: 471_quote_documents_and_delivery
Revises: 470_inbox_lead_intake
Create Date: 2026-08-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "471_quote_documents_and_delivery"
down_revision: str | None = "470_inbox_lead_intake"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "quote_pdf_exports",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("quote_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("stored_file_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("snapshot_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("requested_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["quote_id"], ["quotes.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["stored_file_id"], ["stored_files.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "quote_id",
            "snapshot_fingerprint",
            name="uq_quote_pdf_exports_snapshot",
        ),
        sa.UniqueConstraint("stored_file_id"),
    )
    op.create_index(
        "ix_quote_pdf_exports_quote_id",
        "quote_pdf_exports",
        ["quote_id"],
        unique=False,
    )

    op.create_table(
        "quote_delivery_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("quote_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("pdf_export_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "recipient_contact_point_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "communication_intent_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("requested_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("request_status", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["communication_intent_id"],
            ["communication_intents.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["pdf_export_id"], ["quote_pdf_exports.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["quote_id"], ["quotes.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["recipient_contact_point_id"],
            ["party_contact_points.id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "request_status IN ('queued', 'suppressed')",
            name="ck_quote_delivery_requests_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("communication_intent_id"),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_quote_delivery_requests_idempotency_key",
        ),
    )
    op.create_index(
        "ix_quote_delivery_requests_quote_id",
        "quote_delivery_requests",
        ["quote_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_quote_delivery_requests_quote_id",
        table_name="quote_delivery_requests",
    )
    op.drop_table("quote_delivery_requests")
    op.drop_index("ix_quote_pdf_exports_quote_id", table_name="quote_pdf_exports")
    op.drop_table("quote_pdf_exports")
