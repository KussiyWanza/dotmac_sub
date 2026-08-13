"""Retire the dead scheduler.refresh_minutes control

`scheduler.refresh_minutes` had a spec, a seed row and an entry in the settings
API's private key set — and no reader anywhere. It survived
`tests/architecture/test_no_orphan_settings.py` only because that guard counts a
quoted key literal anywhere under `app/`, and the API's `_SCHEDULER_SETTING_KEYS`
supplied one. Removing that private key set is what exposed it.

The spec and seed are gone; this deletes the stored row so an operator cannot
edit a control that changes nothing. `scheduler.beat_refresh_seconds` is the
live control and is untouched.

Revision ID: 505_retire_scheduler_refresh_minutes
Revises: 504_customer_search_trigram_indexes
Create Date: 2026-08-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "505_retire_scheduler_refresh_minutes"
down_revision: str | None = "504_customer_search_trigram_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DELETE = sa.text("DELETE FROM domain_settings WHERE domain = :domain AND key = :key")


def upgrade() -> None:
    op.get_bind().execute(_DELETE, {"domain": "scheduler", "key": "refresh_minutes"})


def downgrade() -> None:
    """Deliberately empty.

    Restoring the row would recreate a control with no reader, and the value it
    held cannot be recovered — nothing consumed it, so nothing depended on it.
    """
