"""Add template-controlled vendor assignment scope creation.

Revision ID: 515_project_template_vendor_assignment_scope
Revises: 514_domain_settings_scope_invariants
Create Date: 2026-08-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "515_project_template_vendor_assignment_scope"
down_revision: str | None = "514_domain_settings_scope_invariants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "project_templates",
        sa.Column(
            "creates_vendor_assignment_scope",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.execute(
        sa.text(
            """
            update project_templates
            set creates_vendor_assignment_scope = true
            where project_type = 'cable_rerun'
            """
        )
    )
    op.alter_column(
        "project_templates",
        "creates_vendor_assignment_scope",
        server_default=None,
    )


def downgrade() -> None:
    op.drop_column("project_templates", "creates_vendor_assignment_scope")
