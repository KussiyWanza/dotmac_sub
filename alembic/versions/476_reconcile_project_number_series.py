"""reconcile native project numbering with the imported series

Revision ID: 476_reconcile_project_number_series
Revises: 475_inbox_conversation_lead_links
Create Date: 2026-08-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "476_reconcile_project_number_series"
down_revision: str | None = "475_inbox_conversation_lead_links"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Repair the four native numbers and advance without ever rewinding."""

    op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.execute(sa.text("SET LOCAL statement_timeout = '60s'"))
    op.execute(
        sa.text(
            """
            UPDATE projects
            SET number = CASE number
                WHEN '4' THEN 'PROJ-1104'
                WHEN '5' THEN 'PROJ-1105'
                WHEN '6' THEN 'PROJ-1106'
                WHEN '7' THEN 'PROJ-1107'
            END
            WHERE number IN ('4', '5', '6', '7')
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO document_sequences (id, key, next_value, created_at, updated_at)
            SELECT
                gen_random_uuid(),
                'project_number',
                GREATEST(
                    1108,
                    COALESCE(MAX(substring(number FROM 6)::integer) + 1, 1108)
                ),
                now(),
                now()
            FROM projects
            WHERE number ~ '^PROJ-[0-9]+$'
            ON CONFLICT (key) DO UPDATE
            SET next_value = GREATEST(
                    document_sequences.next_value,
                    EXCLUDED.next_value
                ),
                updated_at = now()
            """
        )
    )


def downgrade() -> None:
    """The production data repair is intentionally forward-only."""
