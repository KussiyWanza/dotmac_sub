"""Add client_observation_id to field_tech_location_pings.

A mobile retry after an ambiguous network failure (a timeout that happened
AFTER the server already committed the ping) has no stable identifier to
recognize the earlier attempt with, so it reliably produces a duplicate row.
This column gives the client something to name its own attempt with; old/
already-shipped app builds omit it, so the column is nullable and dedup is
opt-in per ping.

The composite (technician_id, client_observation_id) unique index is a
deliberate deviation from field_job_events.client_event_id's bare
global-unique index: a client-supplied UUID that is unique only within its
own technician cannot collide with an unrelated technician's row. Postgres
unique indexes never treat two NULLs as equal, so old pings with no
client_observation_id are unaffected.

Revision ID: 593_field_location_ping_client_observation_id
Revises: 592_field_expense_payment_permission
Create Date: 2026-09-09
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "593_field_location_ping_client_observation_id"
down_revision = "592_field_expense_payment_permission"
branch_labels = None
depends_on = None

_TABLE = "field_tech_location_pings"
_COLUMN = "client_observation_id"
_INDEX = "ix_field_tech_location_pings_technician_client_observation"


def _has_table(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def _has_column(table: str, column: str) -> bool:
    return column in {col["name"] for col in inspect(op.get_bind()).get_columns(table)}


def _has_index(table: str, index: str) -> bool:
    return index in {idx["name"] for idx in inspect(op.get_bind()).get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    if not _has_table(_TABLE):
        return
    if not _has_column(_TABLE, _COLUMN):
        op.add_column(
            _TABLE,
            sa.Column(_COLUMN, postgresql.UUID(as_uuid=True), nullable=True),
        )
    if not _has_index(_TABLE, _INDEX):
        op.create_index(
            _INDEX,
            _TABLE,
            ["technician_id", _COLUMN],
            unique=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    if not _has_table(_TABLE):
        return
    if _has_index(_TABLE, _INDEX):
        op.drop_index(_INDEX, table_name=_TABLE)
    if _has_column(_TABLE, _COLUMN):
        op.drop_column(_TABLE, _COLUMN)
