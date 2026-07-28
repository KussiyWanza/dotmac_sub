"""Add durable billing shadow-delivery and cutover-verification evidence.

ADR 0007 requires a durable complete-cohort run before any authority move.
These records are evidence only; migration states and billing read paths remain
unchanged.

Revision ID: 436_billing_shadow_verification_evidence
Revises: 435_access_invitations
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "436_billing_shadow_verification_evidence"
down_revision = "435_access_invitations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "billing_shadow_delivery_evidence",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "sales_order_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sales_orders.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "terminal_event_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("obligation_count", sa.Integer(), nullable=False),
        sa.Column("obligation_ids_sha256", sa.String(64), nullable=False),
        sa.Column("command_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "obligation_count >= 0",
            name="ck_billing_shadow_delivery_obligation_count",
        ),
        sa.CheckConstraint(
            "length(obligation_ids_sha256) = 64",
            name="ck_billing_shadow_delivery_obligation_hash",
        ),
        sa.UniqueConstraint(
            "terminal_event_id",
            name="uq_billing_shadow_delivery_terminal_event",
        ),
    )
    op.create_index(
        "ix_billing_shadow_delivery_sales_order",
        "billing_shadow_delivery_evidence",
        ["sales_order_id", "created_at"],
    )

    op.create_table(
        "billing_cutover_verification_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("phase", sa.String(40), nullable=False),
        sa.Column("cohort_name", sa.String(120), nullable=False),
        sa.Column("evidence_schema_version", sa.Integer(), nullable=False),
        sa.Column("policy_version", sa.String(80), nullable=False),
        sa.Column("cutoff_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observation_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observation_ended_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cohort_count", sa.Integer(), nullable=False),
        sa.Column("covered_count", sa.Integer(), nullable=False),
        sa.Column("unresolved_count", sa.Integer(), nullable=False),
        sa.Column("ambiguous_count", sa.Integer(), nullable=False),
        sa.Column("unexpected_unlinked_count", sa.Integer(), nullable=False),
        sa.Column("duplicate_count", sa.Integer(), nullable=False),
        sa.Column("shadow_variance_count", sa.Integer(), nullable=False),
        sa.Column("source_fingerprint", sa.String(64), nullable=False),
        sa.Column("result_fingerprint", sa.String(64), nullable=False),
        sa.Column("currency_totals", postgresql.JSONB(), nullable=False),
        sa.Column("cohort_classification", postgresql.JSONB(), nullable=False),
        sa.Column("event_outcomes", postgresql.JSONB(), nullable=False),
        sa.Column("code_version", sa.String(80), nullable=False),
        sa.Column("database_schema_version", sa.String(120), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("command_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor", sa.String(120), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("operator_approved_by", sa.String(120)),
        sa.Column("operator_approved_at", sa.DateTime(timezone=True)),
        sa.Column("finance_approved_by", sa.String(120)),
        sa.Column("finance_approved_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "cohort_count >= 0 AND covered_count >= 0 "
            "AND unresolved_count >= 0 AND ambiguous_count >= 0 "
            "AND unexpected_unlinked_count >= 0 AND duplicate_count >= 0 "
            "AND shadow_variance_count >= 0",
            name="ck_billing_cutover_verification_nonnegative_counts",
        ),
        sa.CheckConstraint(
            "covered_count <= cohort_count",
            name="ck_billing_cutover_verification_covered_bound",
        ),
        sa.CheckConstraint(
            "length(source_fingerprint) = 64 AND length(result_fingerprint) = 64",
            name="ck_billing_cutover_verification_hashes",
        ),
        sa.CheckConstraint(
            "(operator_approved_by IS NULL AND operator_approved_at IS NULL) OR "
            "(operator_approved_by IS NOT NULL AND operator_approved_at IS NOT NULL)",
            name="ck_billing_cutover_operator_approval_pair",
        ),
        sa.CheckConstraint(
            "(finance_approved_by IS NULL AND finance_approved_at IS NULL) OR "
            "(finance_approved_by IS NOT NULL AND finance_approved_at IS NOT NULL)",
            name="ck_billing_cutover_finance_approval_pair",
        ),
        sa.CheckConstraint(
            "finance_approved_at IS NULL OR operator_approved_at IS NOT NULL",
            name="ck_billing_cutover_finance_requires_operator",
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_billing_cutover_verification_idempotency",
        ),
    )
    op.create_index(
        "ix_billing_cutover_verification_phase_cutoff",
        "billing_cutover_verification_runs",
        ["phase", "cutoff_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_billing_cutover_verification_phase_cutoff",
        table_name="billing_cutover_verification_runs",
    )
    op.drop_table("billing_cutover_verification_runs")
    op.drop_index(
        "ix_billing_shadow_delivery_sales_order",
        table_name="billing_shadow_delivery_evidence",
    )
    op.drop_table("billing_shadow_delivery_evidence")
