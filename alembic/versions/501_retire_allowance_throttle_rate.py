"""Retire usage_allowances.throttle_rate_mbps — FUP has one owner.

``fup_policies``/``fup_rules`` owns every FUP decision, including throttle
depth (docs/PLAN_FAMILY_ARCHITECTURE.md §1, §12). ``usage_allowances`` owns
billing — ``included_gb``, ``overage_rate``, ``overage_cap_gb`` — and those
stay.

``throttle_rate_mbps`` stated an enforcement decision inside a billing object.
It was set on five of six allowances (10, 10, 10, 10 and 1 Mbps) and read by
nothing that enforces: an admin form field, a CSV column, and a projection in
the FUP calculator. Production throttled all of them to a flat 1 Mbps, and now
throttles to a percentage of the subscriber's own rate — so the column was
wrong for four of the five and no code would have noticed.

An unread column holding a wrong answer is worse than a missing one: it is
evidence, and someone eventually acts on it.

The down-migration restores the column but not the values. They were never
authoritative, and re-materialising them would recreate the second answer this
removes.

Revision ID: 501_retire_allowance_throttle_rate
Revises: 500_reconcile_staff_notification_inbox
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "501_retire_allowance_throttle_rate"
down_revision = "500_reconcile_staff_notification_inbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("usage_allowances", "throttle_rate_mbps")


def downgrade() -> None:
    op.add_column(
        "usage_allowances",
        sa.Column("throttle_rate_mbps", sa.Integer(), nullable=True),
    )
