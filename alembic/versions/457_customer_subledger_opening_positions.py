"""Add reviewed customer-subledger opening-position evidence.

ADR 0007 Phase 3 (opening capture). Each non-quarantined account/currency gets
one immutable, finance-approved residual that bridges the verified legacy
position to shadow postings at an exact preview cutoff. Quarantined accounts
are deliberately absent; this migration creates no data and moves no read
authority.

Revision ID: 457_customer_subledger_opening_positions
Revises: 456_ont_wan_service_intent_owner
Create Date: 2026-08-02
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "457_customer_subledger_opening_positions"
down_revision = "456_ont_wan_service_intent_owner"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "customer_subledger_opening_positions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "verification_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("billing_cutover_verification_runs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "baseline_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("prepaid_funding_baselines.id", ondelete="RESTRICT"),
        ),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subscribers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("legacy_position", sa.Numeric(18, 4), nullable=False),
        sa.Column("shadow_position_before", sa.Numeric(18, 4), nullable=False),
        sa.Column("opening_delta", sa.Numeric(18, 4), nullable=False),
        sa.Column("evidence_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("review_reference", sa.Text(), nullable=False),
        sa.Column("captured_by", sa.String(length=160), nullable=False),
        sa.Column("command_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(currency) = 3 AND currency = upper(currency)",
            name="ck_customer_subledger_opening_currency",
        ),
        sa.CheckConstraint(
            "length(evidence_fingerprint) = 64",
            name="ck_customer_subledger_opening_evidence_hash",
        ),
        sa.CheckConstraint(
            "opening_delta = legacy_position - shadow_position_before",
            name="ck_customer_subledger_opening_exact_delta",
        ),
        sa.UniqueConstraint(
            "account_id",
            "currency",
            name="uq_customer_subledger_opening_account_currency",
        ),
        sa.UniqueConstraint(
            "verification_run_id",
            "account_id",
            "currency",
            name="uq_customer_subledger_opening_run_account_currency",
        ),
    )
    op.create_index(
        "ix_customer_subledger_opening_verification_run",
        "customer_subledger_opening_positions",
        ["verification_run_id"],
    )
    op.create_table(
        "customer_subledger_authority_cutovers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("singleton_key", sa.String(length=40), nullable=False),
        sa.Column(
            "verification_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("billing_cutover_verification_runs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("result_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("review_reference", sa.Text(), nullable=False),
        sa.Column("activated_by", sa.String(length=160), nullable=False),
        sa.Column("command_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("cutover_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(result_fingerprint) = 64",
            name="ck_customer_subledger_authority_cutover_hash",
        ),
        sa.UniqueConstraint(
            "singleton_key",
            name="uq_customer_subledger_authority_cutover_singleton",
        ),
        sa.UniqueConstraint(
            "verification_run_id",
            name="uq_customer_subledger_authority_cutover_verification_run",
        ),
    )


def downgrade() -> None:
    op.drop_table("customer_subledger_authority_cutovers")
    op.drop_table("customer_subledger_opening_positions")
