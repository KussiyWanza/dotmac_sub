"""Retire the obsolete allowance-level throttle-rate column.

Revision ID: 501_retire_allowance_throttle_rate
Revises: 500_reconcile_staff_notification_inbox
Create Date: 2026-08-08

This revision reached staging before its file was removed. Alembic revisions
are durable database history: deleting the file made that database's recorded
revision impossible to resolve. The restored revision keeps the same identity
and makes the intended column removal idempotent for databases that have not
applied it yet.

``fup_rules.speed_reduction_percent`` is the authoritative FUP throttle-depth
decision. ``usage_allowances.throttle_rate_mbps`` was the retired duplicate;
the application no longer reads or writes it. Existing legacy values are
therefore deleted rather than copied into a second decision path.

The conditional drop is safe to retry. Lock acquisition is bounded to five
seconds and total statement time to sixty seconds. Operators may retry a
lock-timeout failure. Downgrade moves only the revision marker and does not
recreate the retired column or invent its discarded values.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "501_retire_allowance_throttle_rate"
down_revision: str | None = "500_reconcile_staff_notification_inbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "usage_allowances"
_COLUMN = "throttle_rate_mbps"


def _column_exists(inspector: sa.Inspector) -> bool:
    return any(column["name"] == _COLUMN for column in inspector.get_columns(_TABLE))


def upgrade() -> None:
    op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.execute(sa.text("SET LOCAL statement_timeout = '60s'"))

    if _column_exists(sa.inspect(op.get_bind())):
        op.drop_column(_TABLE, _COLUMN)


def downgrade() -> None:
    """Move the marker only; the retired values cannot be reconstructed."""
