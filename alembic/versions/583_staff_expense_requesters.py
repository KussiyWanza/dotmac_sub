"""Allow authenticated staff requesters and receipt uploaders.

Existing rows remain unchanged. Technician links become optional while the
canonical person and system-user evidence continues to identify new staff
submissions.

Revision ID: 583_staff_expense_requesters
Revises: 582_ai_intake_resolved_state
"""

import sqlalchemy as sa

from alembic import op

revision: str = "583_staff_expense_requesters"
down_revision: str | None = "582_ai_intake_resolved_state"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.alter_column(
        "field_expense_requests",
        "requested_by_technician_id",
        existing_type=sa.UUID(),
        nullable=True,
    )
    op.alter_column(
        "field_attachments",
        "uploaded_by_technician_id",
        existing_type=sa.UUID(),
        nullable=True,
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    bind = op.get_bind()
    expense_missing = bind.execute(
        sa.text(
            "SELECT count(*) FROM field_expense_requests "
            "WHERE requested_by_technician_id IS NULL"
        )
    ).scalar_one()
    attachment_missing = bind.execute(
        sa.text(
            "SELECT count(*) FROM field_attachments "
            "WHERE uploaded_by_technician_id IS NULL"
        )
    ).scalar_one()
    if expense_missing or attachment_missing:
        raise RuntimeError(
            "Cannot restore technician-only expense columns while staff-owned "
            "expense or receipt rows exist"
        )
    op.alter_column(
        "field_attachments",
        "uploaded_by_technician_id",
        existing_type=sa.UUID(),
        nullable=False,
    )
    op.alter_column(
        "field_expense_requests",
        "requested_by_technician_id",
        existing_type=sa.UUID(),
        nullable=False,
    )
