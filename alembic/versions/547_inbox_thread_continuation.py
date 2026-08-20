"""Link a continued Inbox conversation to its resolved predecessor.

Revision ID: 547_inbox_thread_continuation
Revises: 546_module_database_roles_prerequisite
Create Date: 2026-08-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "547_inbox_thread_continuation"
down_revision: str | None = "546_module_database_roles_prerequisite"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "inbox_conversations",
        sa.Column(
            "continued_from_conversation_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_inbox_conversations_continued_from",
        "inbox_conversations",
        "inbox_conversations",
        ["continued_from_conversation_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "ck_inbox_conversations_not_self_continuation",
        "inbox_conversations",
        "continued_from_conversation_id IS NULL OR "
        "continued_from_conversation_id <> id",
    )
    op.create_index(
        "ix_inbox_conversations_continued_from",
        "inbox_conversations",
        ["continued_from_conversation_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_inbox_conversations_continued_from",
        table_name="inbox_conversations",
    )
    op.drop_constraint(
        "ck_inbox_conversations_not_self_continuation",
        "inbox_conversations",
        type_="check",
    )
    op.drop_constraint(
        "fk_inbox_conversations_continued_from",
        "inbox_conversations",
        type_="foreignkey",
    )
    op.drop_column("inbox_conversations", "continued_from_conversation_id")
