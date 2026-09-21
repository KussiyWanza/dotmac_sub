"""Add technician-scoped idempotency to native field notes.

Existing notes remain valid with a null client reference. New mobile clients
send one stable UUID with every queued note, and the owner command uses the
unique technician/reference pair to replay retries without creating a second
note.

Revision ID: 591_field_note_delivery_idempotency
Revises: 590_olt_observation_read_status
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision: str = "591_field_note_delivery_idempotency"
down_revision: str | None = "590_olt_observation_read_status"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "field_work_order_notes"
_INDEX = "uq_field_work_order_notes_author_client_ref"


def _has_column(name: str) -> bool:
    return name in {
        column["name"] for column in inspect(op.get_bind()).get_columns(_TABLE)
    }


def _has_index(name: str) -> bool:
    return name in {
        index["name"] for index in inspect(op.get_bind()).get_indexes(_TABLE)
    }


def upgrade() -> None:
    if not _has_column("client_ref"):
        op.add_column(
            _TABLE,
            sa.Column("client_ref", sa.UUID(), nullable=True),
        )

    if _has_index(_INDEX):
        return
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("SET lock_timeout = '5s'")
            op.execute("SET statement_timeout = '15min'")
            op.execute(
                f"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX} "
                f"ON {_TABLE} (author_system_user_id, client_ref) "
                "WHERE client_ref IS NOT NULL"
            )
            op.execute("RESET statement_timeout")
            op.execute("RESET lock_timeout")
    else:
        op.create_index(
            _INDEX,
            _TABLE,
            ["author_system_user_id", "client_ref"],
            unique=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_index(_INDEX):
        if bind.dialect.name == "postgresql":
            with op.get_context().autocommit_block():
                op.execute("SET lock_timeout = '5s'")
                op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}")
                op.execute("RESET lock_timeout")
        else:
            op.drop_index(_INDEX, table_name=_TABLE)
    if _has_column("client_ref"):
        op.drop_column(_TABLE, "client_ref")
