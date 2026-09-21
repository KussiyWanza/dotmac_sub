"""Add a reclaimable processing lease to the integration inbox.

`claim_for_processing` treated `processing` as permanently un-claimable: a
worker that claimed a receipt and then died/crashed/timed out before calling
`mark_processed`/`mark_failed` left the row stuck forever, and the provider's
retry silently got an empty-consequence 200 (see
`app/services/api_billing_webhooks.py`). `IntegrationDelivery` (the outbound
half of this subsystem) already carries a `leased_until` column with a
2-minute lease; this mirrors that column onto the inbound side so a claim can
expire and be reclaimed instead of leaking a receipt forever.

`attempt_count` already exists on `integration_inbox` (added in
`379_integration_inbox`) and is reused as the race-safety fence: a claimant
captures it at claim time and compares it again at completion, so a reclaimed
row's original (zombie) claimant can never complete a receipt someone else
has since reclaimed.

Revision ID: 589_payment_inbox_lease
Revises: 588_team_inbox_queue_correctness
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision: str = "589_payment_inbox_lease"
down_revision: str | None = "588_team_inbox_queue_correctness"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "ix_integration_inbox_processing_lease"


def _has_column(table_name: str, column_name: str) -> bool:
    return column_name in {
        column["name"] for column in inspect(op.get_bind()).get_columns(table_name)
    }


def _has_index(table_name: str, index_name: str) -> bool:
    return index_name in {
        index["name"] for index in inspect(op.get_bind()).get_indexes(table_name)
    }


def upgrade() -> None:
    if not _has_column("integration_inbox", "lease_expires_at"):
        op.add_column(
            "integration_inbox",
            sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        )

    bind = op.get_bind()
    if _has_index("integration_inbox", _INDEX):
        return
    if bind.dialect.name == "postgresql":
        # `integration_inbox` serves all 8 inbound webhook domains (payments,
        # customer-relationship contacts, leads, ERP material, integrator
        # settlement, ...). A plain CREATE INDEX takes a ShareLock that
        # blocks writes to the whole table -- including live payment webhook
        # ingestion -- for the build's duration. CONCURRENTLY avoids that;
        # matches `581_inbox_delivery_status_index` and
        # `563_topup_reconcile_attempt_leases`.
        with op.get_context().autocommit_block():
            op.execute("SET lock_timeout = '5s'")
            op.execute("SET statement_timeout = '15min'")
            op.execute(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX} "
                "ON integration_inbox (lease_expires_at) "
                "WHERE state = 'processing'"
            )
            op.execute("RESET statement_timeout")
            op.execute("RESET lock_timeout")
    else:
        op.create_index(_INDEX, "integration_inbox", ["lease_expires_at"])


def downgrade() -> None:
    bind = op.get_bind()
    if _has_index("integration_inbox", _INDEX):
        if bind.dialect.name == "postgresql":
            with op.get_context().autocommit_block():
                op.execute("SET lock_timeout = '5s'")
                op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}")
                op.execute("RESET lock_timeout")
        else:
            op.drop_index(_INDEX, table_name="integration_inbox")
    if _has_column("integration_inbox", "lease_expires_at"):
        op.drop_column("integration_inbox", "lease_expires_at")
