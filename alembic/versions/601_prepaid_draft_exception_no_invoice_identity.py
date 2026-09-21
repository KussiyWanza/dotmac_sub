"""Add a DB-level uniqueness guard for the no-invoice prepaid review item.

``record_prepaid_draft_reconciliation_exception`` already de-duplicates a
no-invoice (pre-mutation ambiguous classification) review item in
application code, select-then-insert, keyed on
``(account_id, subscription_id, period_start, period_end)`` when
``invoice_id IS NULL`` -- but nothing at the database level backed that key,
so two genuinely concurrent out-of-band writers (each opening its own
independent session/connection, by design -- see
``_record_review_item_out_of_band``) could both pass the SELECT before
either commits its INSERT, producing two rows for what should be exactly
one case (2026-09, round 7).

Scope: this closes the race for the case that actually carries a real
subscription/period identity (every current no-invoice writer that supplies
one) via a partial unique index mirroring the existing with-invoice pattern
(``uq_prepaid_draft_exception_invoice``). It deliberately does NOT attempt a
NULLS-NOT-DISTINCT-style guard for the rarer, fully-degenerate case where
``subscription_id``/``period_start``/``period_end`` are ALL null (the
receipt fingerprint-mismatch path, keyed only on ``account_id`` today) --
ordinary Postgres/SQLite unique-index semantics treat every NULL as
distinct, so a composite index cannot close that narrower race without a
sentinel-value or ``NULLS NOT DISTINCT`` technique with no precedent
elsewhere in this codebase; that race is additionally narrowed upstream by
`prepaid_funding_trigger_executions`'s own event-level uniqueness
(``uq_prepaid_funding_trigger_event_store``), which makes two concurrent
fingerprint-mismatch writes for the exact same durable event structurally
unreachable. Left as a named, reported gap rather than guessed at.

Revision ID: 601_prepaid_draft_exception_no_invoice_identity
Revises: 600_prepaid_funding_trigger_execution
Create Date: 2026-09-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "601_prepaid_draft_exception_no_invoice_identity"
down_revision: str | None = "600_prepaid_funding_trigger_execution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EXCEPTIONS_TABLE = "prepaid_draft_reconciliation_exceptions"
_INDEX_NAME = "uq_prepaid_draft_exception_no_invoice_identity"


def upgrade() -> None:
    op.create_index(
        _INDEX_NAME,
        _EXCEPTIONS_TABLE,
        ["account_id", "subscription_id", "period_start", "period_end"],
        unique=True,
        postgresql_where=sa.text(
            "invoice_id IS NULL AND subscription_id IS NOT NULL "
            "AND period_start IS NOT NULL AND period_end IS NOT NULL"
        ),
        sqlite_where=sa.text(
            "invoice_id IS NULL AND subscription_id IS NOT NULL "
            "AND period_start IS NOT NULL AND period_end IS NOT NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index(_INDEX_NAME, table_name=_EXCEPTIONS_TABLE)
