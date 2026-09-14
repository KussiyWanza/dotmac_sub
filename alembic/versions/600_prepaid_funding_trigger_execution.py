"""Add the prepaid funding-consequence trigger-execution receipt model.

Part of the single-owner funding-consequence cutover
(``app.services.prepaid_service_renewals``). Two new tables:

- ``prepaid_funding_trigger_executions`` — one durable receipt per durable
  event this owner has ever processed, unique on ``event_store_id``. Answers
  "have we already processed this EXACT event" (idempotent replay / mismatched
  -replay integrity-conflict semantics), a different question from the
  existing period-level protection (``invoice_lines.billing_line_key`` /
  ``_invoice_backed_renewal_evidence``), which answers "is this exact
  subscription+period already funded, regardless of which trigger did it."
  Both continue to exist; neither replaces the other.
- ``prepaid_funding_trigger_subscription_outcomes`` — one child decision row
  per subscription+period a receipt touched (a single funding event can
  legitimately fund more than one due subscription on the same account).

Also widens ``prepaid_draft_reconciliation_exceptions.invoice_id`` to
nullable: a pre-mutation ambiguous classification (the round-2 corrected
design) makes ZERO mutations before writing a review item, so there may be no
invoice yet to point at. The prior plain unique column becomes a partial
unique index (``WHERE invoice_id IS NOT NULL``) to make that explicit.

Revision ID: 600_prepaid_funding_trigger_execution
Revises: 599_prepaid_draft_exception_period_evidence
Create Date: 2026-09-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "600_prepaid_funding_trigger_execution"
down_revision: str | None = "599_prepaid_draft_exception_period_evidence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EXCEPTIONS_TABLE = "prepaid_draft_reconciliation_exceptions"
_TRIGGER_TABLE = "prepaid_funding_trigger_executions"
_OUTCOME_TABLE = "prepaid_funding_trigger_subscription_outcomes"


def upgrade() -> None:
    # --- widen prepaid_draft_reconciliation_exceptions.invoice_id ---------
    op.drop_index("uq_prepaid_draft_exception_invoice", table_name=_EXCEPTIONS_TABLE)
    op.alter_column(
        _EXCEPTIONS_TABLE,
        "invoice_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )
    op.create_index(
        "uq_prepaid_draft_exception_invoice",
        _EXCEPTIONS_TABLE,
        ["invoice_id"],
        unique=True,
        postgresql_where=sa.text("invoice_id IS NOT NULL"),
        sqlite_where=sa.text("invoice_id IS NOT NULL"),
    )

    # --- prepaid_funding_trigger_executions --------------------------------
    op.create_table(
        _TRIGGER_TABLE,
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False
        ),
        sa.Column(
            "event_store_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("event_store.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("payment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("settlement_reference", sa.String(160), nullable=True),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subscribers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("contract_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("outcome_fingerprint", sa.String(64), nullable=False),
        sa.Column("disposition", sa.String(40), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "length(currency) = 3 AND currency = upper(currency)",
            name="ck_prepaid_funding_trigger_currency",
        ),
        sa.CheckConstraint(
            "length(request_fingerprint) = 64",
            name="ck_prepaid_funding_trigger_request_fingerprint",
        ),
        sa.CheckConstraint(
            "length(outcome_fingerprint) = 64",
            name="ck_prepaid_funding_trigger_outcome_fingerprint",
        ),
    )
    op.create_index(
        "uq_prepaid_funding_trigger_event_store",
        _TRIGGER_TABLE,
        ["event_store_id"],
        unique=True,
    )
    op.create_index(
        "ix_prepaid_funding_trigger_account_created",
        _TRIGGER_TABLE,
        ["account_id", "created_at"],
    )

    # --- prepaid_funding_trigger_subscription_outcomes ---------------------
    op.create_table(
        _OUTCOME_TABLE,
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False
        ),
        sa.Column(
            "trigger_execution_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{_TRIGGER_TABLE}.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "subscription_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subscriptions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("disposition", sa.String(40), nullable=False),
        sa.Column("funding_source", sa.String(40), nullable=True),
        sa.Column(
            "invoice_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("invoices.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "invoice_line_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("invoice_lines.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "entitlement_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("service_entitlements.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "funding_evidence_ids",
            postgresql.JSONB(),
            nullable=False,
            server_default="[]",
        ),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("evidence_fingerprint", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "length(currency) = 3 AND currency = upper(currency)",
            name="ck_prepaid_funding_trigger_outcome_currency",
        ),
        sa.CheckConstraint(
            "length(evidence_fingerprint) = 64",
            name="ck_prepaid_funding_trigger_outcome_fingerprint",
        ),
        sa.UniqueConstraint(
            "trigger_execution_id",
            "subscription_id",
            "period_start",
            "period_end",
            name="uq_prepaid_funding_trigger_outcome_period",
        ),
    )


def downgrade() -> None:
    op.drop_table(_OUTCOME_TABLE)
    op.drop_index(
        "ix_prepaid_funding_trigger_account_created", table_name=_TRIGGER_TABLE
    )
    op.drop_index("uq_prepaid_funding_trigger_event_store", table_name=_TRIGGER_TABLE)
    op.drop_table(_TRIGGER_TABLE)

    # Refuse, don't silently corrupt (2026-09, round 7; established pattern:
    # see 580_invoice_line_tax_snapshots.py's downgrade). A pre-mutation
    # ambiguous classification legitimately writes a review item with NO
    # invoice yet (the whole reason this column was widened to nullable
    # above) -- if any such row exists after real use, restoring `NOT NULL`
    # would either fail outright or, worse on some backends, silently drop
    # data. This is a genuine data-loss tradeoff, not a reversible schema
    # tweak, so the downgrade refuses outright rather than guessing.
    bind = op.get_bind()
    if bind.scalar(
        sa.text(
            f"SELECT EXISTS (SELECT 1 FROM {_EXCEPTIONS_TABLE} "
            "WHERE invoice_id IS NULL)"
        )
    ):
        raise RuntimeError(
            "prepaid_draft_reconciliation_exceptions has rows with a NULL "
            "invoice_id (a pre-mutation ambiguous classification with no "
            "invoice yet) -- restoring NOT NULL would destroy or fail to "
            "represent this evidence. Resolve or reassign those rows before "
            "downgrading, or retain the nullable column and roll forward."
        )
    op.drop_index("uq_prepaid_draft_exception_invoice", table_name=_EXCEPTIONS_TABLE)
    op.alter_column(
        _EXCEPTIONS_TABLE,
        "invoice_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )
    op.create_index(
        "uq_prepaid_draft_exception_invoice",
        _EXCEPTIONS_TABLE,
        ["invoice_id"],
        unique=True,
    )
