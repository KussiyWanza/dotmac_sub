"""Index Team Inbox delivery-status reads and tighten table maintenance.

The Inbox sidebar and outbox-failure report filter an operational status held
in message metadata. Keep that read compatible while adding the exact
PostgreSQL expression index used by both the count and ordered list. The index
is additive and built concurrently, so inbound message writes remain available.

The table previously needed roughly sixty-four thousand dead rows before the
default autovacuum threshold was crossed. Per-table settings make maintenance
run at a bounded threshold appropriate for this high-write message ledger.

Revision ID: 581_inbox_delivery_status_index
Revises: 580_invoice_line_tax_snapshots
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "581_inbox_delivery_status_index"
down_revision: str | None = "580_invoice_line_tax_snapshots"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "ix_inbox_messages_delivery_status"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "ALTER TABLE inbox_messages SET ("
            "autovacuum_vacuum_scale_factor = 0.02, "
            "autovacuum_vacuum_threshold = 1000, "
            "autovacuum_analyze_scale_factor = 0.02, "
            "autovacuum_analyze_threshold = 1000"
            ")"
        )
        with op.get_context().autocommit_block():
            op.execute("SET lock_timeout = '5s'")
            op.execute("SET statement_timeout = '15min'")
            op.execute(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX} "
                "ON inbox_messages "
                "(direction, (metadata ->> 'delivery_status'), created_at DESC) "
                "INCLUDE (id)"
            )
            op.execute("RESET statement_timeout")
            op.execute("RESET lock_timeout")
        return

    op.execute(
        f"CREATE INDEX IF NOT EXISTS {_INDEX} ON inbox_messages "
        "(direction, json_extract(metadata, '$.delivery_status'), created_at DESC)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("SET lock_timeout = '5s'")
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}")
            op.execute("RESET lock_timeout")
        op.execute(
            "ALTER TABLE inbox_messages RESET ("
            "autovacuum_vacuum_scale_factor, "
            "autovacuum_vacuum_threshold, "
            "autovacuum_analyze_scale_factor, "
            "autovacuum_analyze_threshold"
            ")"
        )
        return

    op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
