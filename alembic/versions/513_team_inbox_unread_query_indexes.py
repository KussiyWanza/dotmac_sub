"""Add covering indexes for grouped Team Inbox unread reads.

The unread cohort is derived from timestamped inbound messages and one
operator's monotonic conversation cursor. PostgreSQL builds both indexes
concurrently so the message chronology remains writable during the expand
step. The set-based query is compatible before, during, and after either build.

Revision ID: 513_team_inbox_unread_query_indexes
Revises: 512_open_setting_value_type_vocabulary
Create Date: 2026-08-09
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "513_team_inbox_unread_query_indexes"
down_revision: str | None = "512_open_setting_value_type_vocabulary"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MESSAGE_INDEX = "ix_inbox_messages_unread"
_READ_STATE_INDEX = "ix_inbox_read_states_person_conversation"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_MESSAGE_INDEX} "
                "ON inbox_messages (conversation_id, received_at) "
                "WHERE direction = 'inbound' AND received_at IS NOT NULL"
            )
            op.execute(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_READ_STATE_INDEX} "
                "ON inbox_conversation_read_states "
                "(person_id, conversation_id, last_read_at)"
            )
        return

    op.execute(
        f"CREATE INDEX IF NOT EXISTS {_MESSAGE_INDEX} "
        "ON inbox_messages (conversation_id, received_at) "
        "WHERE direction = 'inbound' AND received_at IS NOT NULL"
    )
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {_READ_STATE_INDEX} "
        "ON inbox_conversation_read_states "
        "(person_id, conversation_id, last_read_at)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_READ_STATE_INDEX}")
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_MESSAGE_INDEX}")
        return

    op.execute(f"DROP INDEX IF EXISTS {_READ_STATE_INDEX}")
    op.execute(f"DROP INDEX IF EXISTS {_MESSAGE_INDEX}")
