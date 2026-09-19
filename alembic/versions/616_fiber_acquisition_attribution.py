"""Add immutable Fiber journey and customer-reference attribution.

Revision ID: 616_fiber_acquisition_attribution
Revises: 615_inbox_identity_guard
Create Date: 2026-09-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "616_fiber_acquisition_attribution"
down_revision: str | None = "615_inbox_identity_guard"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "lead_origin_captures",
        sa.Column("journey_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "lead_origin_captures",
        sa.Column("customer_reference", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "lead_origin_captures",
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "uq_lead_origin_captures_customer_reference",
        "lead_origin_captures",
        ["customer_reference"],
        unique=True,
        postgresql_where=sa.text("customer_reference IS NOT NULL"),
    )
    op.create_index(
        "ix_lead_origin_captures_journey_id",
        "lead_origin_captures",
        ["journey_id"],
    )
    op.create_table(
        "lead_conversion_milestones",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False
        ),
        sa.Column("origin_capture_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("external_event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("stage", sa.String(length=40), nullable=False),
        sa.Column("subject_key", sa.String(length=64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "stage IN ('visitor','coverage_check','lead','qualified_lead',"
            "'payment','installation','activated_subscriber')",
            name="ck_lead_conversion_milestones_stage",
        ),
        sa.ForeignKeyConstraint(
            ["origin_capture_id"],
            ["lead_origin_captures.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "origin_capture_id",
            "stage",
            name="uq_lead_conversion_milestones_origin_stage",
        ),
        sa.UniqueConstraint(
            "external_event_id",
            name="uq_lead_conversion_milestones_external_event",
        ),
    )
    op.create_index(
        "ix_lead_conversion_milestones_origin",
        "lead_conversion_milestones",
        ["origin_capture_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_lead_conversion_milestones_origin",
        table_name="lead_conversion_milestones",
    )
    op.drop_table("lead_conversion_milestones")
    op.drop_index(
        "ix_lead_origin_captures_journey_id",
        table_name="lead_origin_captures",
    )
    op.drop_index(
        "uq_lead_origin_captures_customer_reference",
        table_name="lead_origin_captures",
    )
    op.drop_column("lead_origin_captures", "submitted_at")
    op.drop_column("lead_origin_captures", "customer_reference")
    op.drop_column("lead_origin_captures", "journey_id")
