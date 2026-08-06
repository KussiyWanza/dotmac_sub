"""backfill project numbers created during the numbering repair rollout

Revision ID: 496_backfill_project_numbers_8_10
Revises: 495_plan_family_catalogues
Create Date: 2026-08-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "496_backfill_project_numbers_8_10"
down_revision: str | None = "495_plan_family_catalogues"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Allocate collision-free canonical numbers to rollout-window projects."""

    op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.execute(sa.text("SET LOCAL statement_timeout = '60s'"))
    op.execute(sa.text("LOCK TABLE document_sequences IN SHARE ROW EXCLUSIVE MODE"))
    op.execute(sa.text("LOCK TABLE projects IN SHARE ROW EXCLUSIVE MODE"))
    op.execute(
        sa.text(
            """
            WITH project_number_floor AS (
                SELECT GREATEST(
                    COALESCE((
                        SELECT MAX(substring(number FROM 6)::integer)
                        FROM projects
                        WHERE number ~ '^PROJ-[0-9]+$'
                    ), 0),
                    COALESCE((
                        SELECT next_value - 1
                        FROM document_sequences
                        WHERE key = 'project_number'
                    ), 0)
                ) AS value
            ),
            rollout_window_projects AS (
                SELECT id, row_number() OVER (ORDER BY number::integer) AS offset
                FROM projects
                WHERE number IN ('8', '9', '10')
            )
            UPDATE projects AS project
            SET number = 'PROJ-' || (
                project_number_floor.value + rollout_window_projects.offset
            )::text
            FROM project_number_floor, rollout_window_projects
            WHERE project.id = rollout_window_projects.id
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO document_sequences (id, key, next_value, created_at, updated_at)
            SELECT gen_random_uuid(), 'project_number',
                COALESCE(MAX(substring(number FROM 6)::integer) + 1, 1), now(), now()
            FROM projects
            WHERE number ~ '^PROJ-[0-9]+$'
            ON CONFLICT (key) DO UPDATE
            SET next_value = GREATEST(
                    document_sequences.next_value, EXCLUDED.next_value
                ),
                updated_at = now()
            """
        )
    )


def downgrade() -> None:
    """The production data repair is intentionally forward-only."""
