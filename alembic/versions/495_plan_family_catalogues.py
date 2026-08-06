"""add versioned plan-family catalogue PDFs

Revision ID: 495_plan_family_catalogues
Revises: 494_team_inbox_agent_introductions
Create Date: 2026-08-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "495_plan_family_catalogues"
down_revision: str | None = "494_team_inbox_agent_introductions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "plan_family_catalogues",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("plan_family", sa.String(length=40), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "status", sa.String(length=24), server_default="published", nullable=False
        ),
        sa.Column("display_name", sa.String(length=160), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("stored_file_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_by_system_user_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('published', 'superseded', 'withdrawn')",
            name="ck_plan_family_catalogues_status",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_system_user_id"],
            ["system_users.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["stored_file_id"], ["stored_files.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_plan_family_catalogues_family_version",
        "plan_family_catalogues",
        ["plan_family", "version"],
        unique=True,
    )
    op.create_index(
        "ix_plan_family_catalogues_family_status",
        "plan_family_catalogues",
        ["plan_family", "status", "published_at"],
        unique=False,
    )
    op.create_index(
        "uq_plan_family_catalogues_one_published_family",
        "plan_family_catalogues",
        ["plan_family"],
        unique=True,
        postgresql_where=sa.text("status = 'published'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_plan_family_catalogues_one_published_family",
        table_name="plan_family_catalogues",
    )
    op.drop_index(
        "ix_plan_family_catalogues_family_status",
        table_name="plan_family_catalogues",
    )
    op.drop_index(
        "uq_plan_family_catalogues_family_version",
        table_name="plan_family_catalogues",
    )
    op.drop_table("plan_family_catalogues")
