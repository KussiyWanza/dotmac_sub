"""Restore the deferred allowance-throttle compatibility column when absent.

Revision ID: 502_restore_deferred_allowance_throttle
Revises: 501_retire_allowance_throttle_rate
Create Date: 2026-08-08

Most databases still have ``usage_allowances.throttle_rate_mbps`` because the
drop was deferred. One staging database applied the original revision 501
before that revision was replaced by a no-op compatibility marker. This
forward repair makes both histories converge on the deferred schema shape.

The nullable integer column contains no authoritative FUP decision and remains
unmapped by the application. Existing values are preserved where the column
already exists. A database where the original drop ran receives the column
with NULL values; the migration does not invent the discarded legacy values.

The conditional additive DDL is idempotent. Lock acquisition is bounded to
five seconds and total statement time to sixty seconds. Operators may retry a
lock-timeout failure. Downgrade is forward-fix-only because dropping the
column would repeat the destructive operation this repair reverses.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "502_restore_deferred_allowance_throttle"
down_revision: str | None = "501_retire_allowance_throttle_rate"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "usage_allowances"
_COLUMN = "throttle_rate_mbps"


def _column_exists(inspector: sa.Inspector) -> bool:
    return any(column["name"] == _COLUMN for column in inspector.get_columns(_TABLE))


def upgrade() -> None:
    op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.execute(sa.text("SET LOCAL statement_timeout = '60s'"))

    if not _column_exists(sa.inspect(op.get_bind())):
        op.add_column(
            _TABLE,
            sa.Column(_COLUMN, sa.Integer(), nullable=True),
        )


def downgrade() -> None:
    raise RuntimeError(
        "502_restore_deferred_allowance_throttle is forward-fix only; "
        "dropping throttle_rate_mbps would repeat the deferred destructive change"
    )
