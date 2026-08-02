"""Add current-value RF signal columns to cpe_devices.

Wireless customer radios (cpe_devices with device_type=wireless_radio) store
UISP presence (``last_uisp_status``) but no RF/RSSI value, so the last-mile
link-signal rung is unobservable and Customer 360 cannot show signal strength.
Adds the current-value observation written by the UISP topology sync from the
AP-side station listing: value, source and observation timestamp. History is
deliberately not kept here (``ont_signal_observations`` is the precedent if a
trend table is ever needed).

Expand-only: three nullable columns, no backfill, no index — values arrive
naturally from the next sync run. Lock budget: trivial (nullable ADD COLUMN,
no rewrite on PostgreSQL). Downgrade drops the columns.

Revision ID: 455_add_cpe_rf_signal_columns
Revises: 454_clear_non_identifying_ont_macs
Create Date: 2026-08-01
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "455_add_cpe_rf_signal_columns"
down_revision = "454_clear_non_identifying_ont_macs"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("rf_signal_dbm", sa.Float()),
    ("rf_signal_source", sa.String(length=32)),
    ("rf_signal_observed_at", sa.DateTime(timezone=True)),
)


def _has_column(table: str, column: str) -> bool:
    inspector = inspect(op.get_bind())
    return any(col["name"] == column for col in inspector.get_columns(table))


def upgrade() -> None:
    for name, type_ in _COLUMNS:
        if not _has_column("cpe_devices", name):
            op.add_column("cpe_devices", sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    for name, _type in reversed(_COLUMNS):
        if _has_column("cpe_devices", name):
            op.drop_column("cpe_devices", name)
