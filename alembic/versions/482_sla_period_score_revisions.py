"""Persist immutable SLA period-score revisions and their evidence snapshots.

Revision ID: 482_sla_period_score_revisions
Revises: 481_billing_reconciliation_permissions
Create Date: 2026-08-05

The scorer composes facts owned elsewhere, but its reproducible result is an
authoritative customer.service_level record.  A revision therefore snapshots
the exact proven eligibility and positive-monitoring intervals it consumed.
Changed evidence appends a revision; UPDATE and DELETE are rejected on all
three tables.  The database also refuses an incomplete score labelled passing
or at-risk, so concurrency or an alternate writer cannot guess through an
evidence gap.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "482_sla_period_score_revisions"
down_revision: str | None = "481_billing_reconciliation_permissions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (
    "sla_period_score_revisions",
    "sla_score_eligibility_intervals",
    "sla_score_monitoring_intervals",
)
_APPEND_ONLY_FUNCTION = "sla_score_evidence_append_only"


def _uuid() -> sa.types.TypeEngine[object]:
    return postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "sla_period_score_revisions",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("subscription_id", _uuid(), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("supersedes_id", _uuid(), nullable=True),
        sa.Column("eligible_seconds", sa.Integer(), nullable=False),
        sa.Column("unavailable_seconds", sa.Integer(), nullable=False),
        sa.Column("excluded_seconds", sa.Integer(), nullable=False),
        sa.Column("unknown_seconds", sa.Integer(), nullable=False),
        sa.Column("verdict", sa.String(length=32), nullable=False),
        sa.Column("evidence_complete", sa.Boolean(), nullable=False),
        sa.Column("completeness_issues", sa.JSON(), nullable=False),
        sa.Column("availability_lower_bound_percent", sa.Numeric(7, 4), nullable=True),
        sa.Column("availability_upper_bound_percent", sa.Numeric(7, 4), nullable=True),
        sa.Column("policy_segments", sa.JSON(), nullable=False),
        sa.Column("policy_version_ids", sa.JSON(), nullable=False),
        sa.Column("outage_interval_ids", sa.JSON(), nullable=False),
        sa.Column("lifecycle_evidence_ids", sa.JSON(), nullable=False),
        sa.Column("evidence_digest", sa.String(length=71), nullable=False),
        sa.Column("recorded_by", sa.String(length=160), nullable=False),
        sa.Column("command_id", _uuid(), nullable=False),
        sa.Column("command_idempotency_key", sa.String(length=160), nullable=True),
        sa.Column("correlation_id", _uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "period_end > period_start AND evaluated_at >= period_start",
            name="ck_sla_period_scores_time_bounds",
        ),
        sa.CheckConstraint("revision >= 1", name="ck_sla_period_scores_revision"),
        sa.CheckConstraint(
            "(revision = 1 AND supersedes_id IS NULL) OR "
            "(revision > 1 AND supersedes_id IS NOT NULL)",
            name="ck_sla_period_scores_revision_link",
        ),
        sa.CheckConstraint(
            "eligible_seconds >= 0 AND unavailable_seconds >= 0 "
            "AND excluded_seconds >= 0 AND unknown_seconds >= 0",
            name="ck_sla_period_scores_nonnegative_seconds",
        ),
        sa.CheckConstraint(
            "unavailable_seconds + excluded_seconds + unknown_seconds "
            "<= eligible_seconds",
            name="ck_sla_period_scores_accounted_bounds",
        ),
        sa.CheckConstraint(
            "verdict IN ('passing', 'at_risk', 'breach', 'unavailable', "
            "'no_contractual_sla')",
            name="ck_sla_period_scores_verdict",
        ),
        sa.CheckConstraint(
            "evidence_complete OR verdict NOT IN ('passing', 'at_risk')",
            name="ck_sla_period_scores_no_incomplete_pass",
        ),
        sa.CheckConstraint(
            "availability_lower_bound_percent IS NULL OR "
            "(availability_lower_bound_percent >= 0 "
            "AND availability_lower_bound_percent <= 100)",
            name="ck_sla_period_scores_lower_bound",
        ),
        sa.CheckConstraint(
            "availability_upper_bound_percent IS NULL OR "
            "(availability_upper_bound_percent >= 0 "
            "AND availability_upper_bound_percent <= 100)",
            name="ck_sla_period_scores_upper_bound",
        ),
        sa.CheckConstraint(
            "availability_lower_bound_percent IS NULL "
            "OR availability_upper_bound_percent IS NULL "
            "OR availability_lower_bound_percent "
            "<= availability_upper_bound_percent",
            name="ck_sla_period_scores_bound_order",
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["subscriptions.id"],
            ondelete="RESTRICT",
            name="fk_sla_period_scores_subscription",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_id", "subscription_id", "period_start", "period_end"],
            [
                "sla_period_score_revisions.id",
                "sla_period_score_revisions.subscription_id",
                "sla_period_score_revisions.period_start",
                "sla_period_score_revisions.period_end",
            ],
            ondelete="RESTRICT",
            name="fk_sla_period_scores_supersedes",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sla_period_score_revisions"),
        sa.UniqueConstraint(
            "subscription_id",
            "period_start",
            "period_end",
            "revision",
            name="uq_sla_period_scores_period_revision",
        ),
        sa.UniqueConstraint(
            "subscription_id",
            "period_start",
            "period_end",
            "evidence_digest",
            name="uq_sla_period_scores_period_evidence",
        ),
        sa.UniqueConstraint("command_id", name="uq_sla_period_scores_command_id"),
        sa.UniqueConstraint(
            "command_idempotency_key",
            name="uq_sla_period_scores_idempotency_key",
        ),
        sa.UniqueConstraint(
            "id",
            "subscription_id",
            name="uq_sla_period_scores_id_subscription",
        ),
        sa.UniqueConstraint(
            "id",
            "subscription_id",
            "period_start",
            "period_end",
            name="uq_sla_period_scores_identity_scope",
        ),
    )
    op.create_index(
        "ix_sla_period_scores_subscription_period",
        "sla_period_score_revisions",
        ["subscription_id", "period_start", "period_end", "revision"],
    )

    op.create_table(
        "sla_score_eligibility_intervals",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("score_revision_id", _uuid(), nullable=False),
        sa.Column("subscription_id", _uuid(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evidence_grade", sa.String(length=24), nullable=False),
        sa.Column("entitlement_source", sa.String(length=48), nullable=False),
        sa.Column("entitlement_evidence_ids", sa.JSON(), nullable=False),
        sa.Column("lifecycle_evidence_ids", sa.JSON(), nullable=False),
        sa.Column("fingerprint", sa.String(length=71), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "ends_at > starts_at", name="ck_sla_eligibility_positive_interval"
        ),
        sa.CheckConstraint(
            "evidence_grade IN ('authoritative', 'provisional')",
            name="ck_sla_eligibility_evidence_grade",
        ),
        sa.CheckConstraint(
            "fingerprint LIKE 'sha256:%'", name="ck_sla_eligibility_fingerprint"
        ),
        sa.ForeignKeyConstraint(
            ["score_revision_id", "subscription_id"],
            [
                "sla_period_score_revisions.id",
                "sla_period_score_revisions.subscription_id",
            ],
            ondelete="RESTRICT",
            name="fk_sla_eligibility_score",
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["subscriptions.id"],
            ondelete="RESTRICT",
            name="fk_sla_eligibility_subscription",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sla_score_eligibility_intervals"),
        sa.UniqueConstraint(
            "score_revision_id",
            "fingerprint",
            name="uq_sla_eligibility_score_fingerprint",
        ),
    )
    op.create_index(
        "ix_sla_eligibility_score_time",
        "sla_score_eligibility_intervals",
        ["score_revision_id", "starts_at", "ends_at"],
    )

    op.create_table(
        "sla_score_monitoring_intervals",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("score_revision_id", _uuid(), nullable=False),
        sa.Column("subscription_id", _uuid(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(length=48), nullable=False),
        sa.Column("source_id", _uuid(), nullable=False),
        sa.Column("fingerprint", sa.String(length=71), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "ends_at > starts_at", name="ck_sla_monitoring_positive_interval"
        ),
        sa.CheckConstraint(
            "source = 'radius_accounting_session'",
            name="ck_sla_monitoring_source",
        ),
        sa.CheckConstraint(
            "fingerprint LIKE 'sha256:%'", name="ck_sla_monitoring_fingerprint"
        ),
        sa.ForeignKeyConstraint(
            ["score_revision_id", "subscription_id"],
            [
                "sla_period_score_revisions.id",
                "sla_period_score_revisions.subscription_id",
            ],
            ondelete="RESTRICT",
            name="fk_sla_monitoring_score",
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["subscriptions.id"],
            ondelete="RESTRICT",
            name="fk_sla_monitoring_subscription",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sla_score_monitoring_intervals"),
        sa.UniqueConstraint(
            "score_revision_id",
            "fingerprint",
            name="uq_sla_monitoring_score_fingerprint",
        ),
    )
    op.create_index(
        "ix_sla_monitoring_score_time",
        "sla_score_monitoring_intervals",
        ["score_revision_id", "starts_at", "ends_at"],
    )

    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            f"""
            CREATE FUNCTION {_APPEND_ONLY_FUNCTION}()
            RETURNS trigger AS $$
            BEGIN
              RAISE EXCEPTION '% is append-only: % rejected', TG_TABLE_NAME, TG_OP
                USING ERRCODE = 'integrity_constraint_violation';
            END;
            $$ LANGUAGE plpgsql;
            """
        )
        for table in _TABLES:
            op.execute(
                f"""
                CREATE TRIGGER trg_{table}_append_only
                BEFORE UPDATE OR DELETE ON {table}
                FOR EACH ROW EXECUTE FUNCTION {_APPEND_ONLY_FUNCTION}();
                """
            )


def downgrade() -> None:
    bind = op.get_bind()
    for table in _TABLES:
        row_count = bind.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one()
        if row_count:
            raise RuntimeError(
                "SLA score evidence has been written; downgrade requires a "
                "reviewed forward fix"
            )

    if bind.dialect.name == "postgresql":
        for table in reversed(_TABLES):
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON {table}")
        op.execute(f"DROP FUNCTION IF EXISTS {_APPEND_ONLY_FUNCTION}()")

    op.drop_index(
        "ix_sla_monitoring_score_time",
        table_name="sla_score_monitoring_intervals",
    )
    op.drop_table("sla_score_monitoring_intervals")
    op.drop_index(
        "ix_sla_eligibility_score_time",
        table_name="sla_score_eligibility_intervals",
    )
    op.drop_table("sla_score_eligibility_intervals")
    op.drop_index(
        "ix_sla_period_scores_subscription_period",
        table_name="sla_period_score_revisions",
    )
    op.drop_table("sla_period_score_revisions")
