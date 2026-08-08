"""Keep the previously applied allowance-throttle revision resolvable.

Revision ID: 501_retire_allowance_throttle_rate
Revises: 500_reconcile_staff_notification_inbox
Create Date: 2026-08-08

An earlier form of this revision dropped
``usage_allowances.throttle_rate_mbps`` and reached staging before the
destructive change was deferred. Removing the revision file made Alembic
unable to load that database's recorded revision at all.

This compatibility marker deliberately performs no DDL. Revision 502 repairs
the missing nullable column without inventing the values already lost in an
environment that ran the original 501. Both directions are safe to retry and
need no table lock or statement-time budget because they execute no SQL.
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "501_retire_allowance_throttle_rate"
down_revision: str | None = "500_reconcile_staff_notification_inbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Preserve revision continuity without repeating the deferred drop."""


def downgrade() -> None:
    """Move the revision marker only; this compatibility step owns no DDL."""
