"""Add forward-only project template subtasks and plan revisions.

Revision ID: 606_project_task_subtasks
Revises: 605_erp_staff_talk_mapping_scope
Create Date: 2026-09-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "606_project_task_subtasks"
down_revision: str | None = "605_erp_staff_talk_mapping_scope"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "project_templates",
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
    )
    op.add_column(
        "projects", sa.Column("applied_template_revision", sa.Integer(), nullable=True)
    )
    op.add_column(
        "project_template_tasks",
        sa.Column(
            "parent_template_task_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
    )
    op.create_foreign_key(
        "fk_project_template_tasks_parent_template_task_id",
        "project_template_tasks",
        "project_template_tasks",
        ["parent_template_task_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_project_template_tasks_no_self_parent",
        "project_template_tasks",
        "parent_template_task_id IS NULL OR parent_template_task_id <> id",
    )
    op.create_index(
        "ix_project_template_tasks_parent_template_task_id",
        "project_template_tasks",
        ["parent_template_task_id"],
    )
    op.add_column(
        "project_tasks", sa.Column("template_revision", sa.Integer(), nullable=True)
    )
    op.add_column(
        "project_tasks",
        sa.Column("template_plan_state", sa.String(length=20), nullable=True),
    )
    op.create_check_constraint(
        "ck_project_tasks_template_plan_state",
        "project_tasks",
        "template_plan_state IS NULL OR template_plan_state IN ('current','superseded')",
    )
    op.create_index(
        "ix_project_tasks_project_template_plan_state",
        "project_tasks",
        ["project_id", "template_plan_state"],
    )

    # Existing concrete tasks stay exactly as they are: they are marked as the
    # legacy current snapshot but no template hierarchy is retrofitted.
    op.execute(
        "UPDATE projects SET applied_template_revision = 1 "
        "WHERE project_template_id IS NOT NULL "
        "AND applied_template_revision IS NULL"
    )
    op.execute(
        "UPDATE project_tasks SET template_revision = 1, "
        "template_plan_state = 'current' "
        "WHERE template_task_id IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_index(
        "ix_project_tasks_project_template_plan_state", table_name="project_tasks"
    )
    op.drop_constraint(
        "ck_project_tasks_template_plan_state", "project_tasks", type_="check"
    )
    op.drop_column("project_tasks", "template_plan_state")
    op.drop_column("project_tasks", "template_revision")
    op.drop_index(
        "ix_project_template_tasks_parent_template_task_id",
        table_name="project_template_tasks",
    )
    op.drop_constraint(
        "ck_project_template_tasks_no_self_parent",
        "project_template_tasks",
        type_="check",
    )
    op.drop_constraint(
        "fk_project_template_tasks_parent_template_task_id",
        "project_template_tasks",
        type_="foreignkey",
    )
    op.drop_column("project_template_tasks", "parent_template_task_id")
    op.drop_column("projects", "applied_template_revision")
    op.drop_column("project_templates", "revision")
