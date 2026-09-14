"""Enforce one active TR-069 identity per CPE inventory record.

The CPE-detail WiFi owner resolves an exact active ``Tr069CpeDevice`` by
``cpe_device_id``. Application-level ambiguity checks are insufficient under
concurrency: an inactive sibling could be activated after the owner's read.
This partial unique index makes the invariant atomic at the database boundary.

The migration deliberately does not choose a winner for existing duplicates.
It reports a bounded UUID-only sample and refuses until an operator adjudicates
which identity is authoritative.

``tr069_cpe_devices`` is written on essentially every live TR-069 Inform
across the fleet (``app/services/tr069.py``'s ``receive_inform`` and its
batched bulk-sync loop), so the index is built ``CONCURRENTLY`` on PostgreSQL
-- the repo's established pattern for a unique partial index on a high-write
table (see ``581_inbox_delivery_status_index.py``,
``591_field_note_delivery_idempotency.py``) -- so live Inform writes are not
blocked for the duration of the build. A ``CREATE UNIQUE INDEX CONCURRENTLY``
that fails (e.g. a duplicate slipped in after the pre-check above) leaves an
INVALID index of the same name behind rather than retrying automatically;
``_has_index`` treats any index with this name, valid or not, as "already
attempted" and skips on rerun, so an operator must ``DROP INDEX
CONCURRENTLY`` the invalid one after adjudicating the duplicate, exactly as
this migration's own duplicate-refusal message instructs, before rerunning.

Revision ID: 595_active_cpe_identity
Revises: 594_field_expense_destination
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "595_active_cpe_identity"
down_revision = "594_field_expense_destination"
branch_labels = None
depends_on = None

_TABLE = "tr069_cpe_devices"
_INDEX = "uq_tr069_cpe_devices_active_cpe_device_id"
_WHERE = "is_active AND cpe_device_id IS NOT NULL"


def _has_index(name: str) -> bool:
    return name in {
        index["name"] for index in inspect(op.get_bind()).get_indexes(_TABLE)
    }


def upgrade() -> None:
    if _has_index(_INDEX):
        return

    bind = op.get_bind()
    duplicates = bind.execute(
        sa.text(
            "SELECT cpe_device_id, count(*) AS active_count "
            f"FROM {_TABLE} "
            f"WHERE {_WHERE} "
            "GROUP BY cpe_device_id HAVING count(*) > 1 "
            "ORDER BY cpe_device_id LIMIT 10"
        )
    ).fetchall()
    if duplicates:
        sample = ", ".join(f"{row[0]} ({row[1]})" for row in duplicates)
        raise RuntimeError(
            "Cannot enforce one active TR-069 identity per CPE while duplicate "
            f"active links exist: {sample}. Deactivate the non-authoritative "
            "rows after reviewed identity adjudication, then re-run."
        )

    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("SET lock_timeout = '5s'")
            op.execute("SET statement_timeout = '15min'")
            op.execute(
                f"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX} "
                f"ON {_TABLE} (cpe_device_id) WHERE {_WHERE}"
            )
            op.execute("RESET statement_timeout")
            op.execute("RESET lock_timeout")
    else:
        op.create_index(
            _INDEX,
            _TABLE,
            ["cpe_device_id"],
            unique=True,
            sqlite_where=sa.text(_WHERE),
        )


def downgrade() -> None:
    if not _has_index(_INDEX):
        return

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("SET lock_timeout = '5s'")
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}")
            op.execute("RESET lock_timeout")
    else:
        op.drop_index(_INDEX, table_name=_TABLE)
