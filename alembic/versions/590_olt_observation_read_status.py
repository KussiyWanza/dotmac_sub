"""Add OLT observation freshness/status columns.

Astra audit finding (Bug 3): an unavailable OLT read (transport failure or a
reachable-but-unparseable/rejected reply) previously collapsed into a
confident "device absent" observation, which drove a live re-authorization
and overwrote the last real evidence with a fabricated absence. The fix keeps
the previous ``olt_*`` columns untouched on an unavailable read; these two
columns are what let an operator tell "unchanged because still good" apart
from "unchanged because we couldn't check."

Expand-only: both columns are nullable, no backfill. Existing rows carry no
history to backfill from — they simply have no read-status until the next
reconcile pass runs and stamps one.

Revision ID: 590_olt_observation_read_status
Revises: 589_payment_inbox_lease
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "590_olt_observation_read_status"
down_revision = "589_payment_inbox_lease"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ont_observations",
        sa.Column("olt_read_status", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "ont_observations",
        sa.Column("olt_observed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ont_observations", "olt_observed_at")
    op.drop_column("ont_observations", "olt_read_status")
