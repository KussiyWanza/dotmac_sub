"""Persist immutable billing-obligation rating provenance.

New shadow obligations retain every material rating replay input. Existing
rows remain explicitly incomplete: this migration does not infer historical
coverage or tax configuration.

Revision ID: 439_billing_obligation_rating_provenance
Revises: 438_billing_phase2_verification_counts
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "439_billing_obligation_rating_provenance"
down_revision = "438_billing_phase2_verification_counts"
branch_labels = None
depends_on = None


_RATE_BASIS = postgresql.ENUM(
    "fixed_per_service_period",
    "per_rate_unit",
    "per_quantity",
    "usage_metered",
    name="ratebasis",
    create_type=False,
)
_INTERVAL_UNIT = postgresql.ENUM(
    "day",
    "week",
    "month",
    "year",
    name="intervalunit",
    create_type=False,
)
_PRORATION_POLICY = postgresql.ENUM(
    "none",
    "full_period",
    "actual_calendar_days",
    "actual_elapsed_time",
    name="prorationpolicy",
    create_type=False,
)


def upgrade() -> None:
    op.add_column(
        "billing_obligations",
        sa.Column(
            "rating_provenance_complete",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    for column in (
        sa.Column("rating_policy_version", sa.String(40)),
        sa.Column("rating_coverage_start", sa.DateTime(timezone=True)),
        sa.Column("rating_coverage_end", sa.DateTime(timezone=True)),
        sa.Column("rating_unit_price", sa.Numeric(14, 4)),
        sa.Column("rating_quantity", sa.Numeric(14, 4)),
        sa.Column("rating_rate_basis", _RATE_BASIS),
        sa.Column("rating_rate_unit", _INTERVAL_UNIT),
        sa.Column("rating_rate_quantity", sa.Numeric(14, 4)),
        sa.Column("rating_timezone_name", sa.String(64)),
        sa.Column("rating_proration_policy", _PRORATION_POLICY),
        sa.Column("rating_rate_units", sa.Numeric(38, 28)),
        sa.Column("rating_proration_factor", sa.Numeric(38, 28)),
        sa.Column("rating_tax_treatment_code", sa.String(60)),
        sa.Column("rating_tax_rate_id", postgresql.UUID(as_uuid=True)),
        sa.Column("rating_tax_rate_percent", sa.Numeric(6, 4)),
        sa.Column("rating_tax_inclusive", sa.Boolean()),
        sa.Column("rating_input_fingerprint", sa.String(64)),
    ):
        op.add_column("billing_obligations", column)
    op.create_foreign_key(
        "fk_billing_obligation_rating_tax_rate",
        "billing_obligations",
        "tax_rates",
        ["rating_tax_rate_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_billing_obligation_rating_coverage",
        "billing_obligations",
        "rating_coverage_start IS NULL OR rating_coverage_end IS NULL "
        "OR (rating_coverage_end > rating_coverage_start "
        "AND rating_coverage_start >= period_start "
        "AND rating_coverage_end <= period_end)",
    )
    op.create_check_constraint(
        "ck_billing_obligation_rating_provenance_complete",
        "billing_obligations",
        "NOT rating_provenance_complete OR ("
        "rating_policy_version IS NOT NULL "
        "AND rating_coverage_start IS NOT NULL "
        "AND rating_coverage_end IS NOT NULL "
        "AND rating_unit_price IS NOT NULL "
        "AND rating_quantity IS NOT NULL "
        "AND rating_rate_basis IS NOT NULL "
        "AND rating_rate_unit IS NOT NULL "
        "AND rating_rate_quantity IS NOT NULL "
        "AND rating_timezone_name IS NOT NULL "
        "AND rating_proration_policy IS NOT NULL "
        "AND rating_rate_units IS NOT NULL "
        "AND rating_proration_factor IS NOT NULL "
        "AND rating_tax_rate_percent IS NOT NULL "
        "AND rating_tax_inclusive IS NOT NULL "
        "AND rating_input_fingerprint IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_billing_obligation_rating_values",
        "billing_obligations",
        "NOT rating_provenance_complete OR ("
        "rating_unit_price >= 0 AND rating_quantity > 0 "
        "AND rating_rate_quantity > 0 AND rating_rate_units >= 0 "
        "AND rating_proration_factor >= 0 "
        "AND rating_proration_factor <= 1 "
        "AND rating_tax_rate_percent >= 0)",
    )
    op.create_check_constraint(
        "ck_billing_obligation_rating_tax_source",
        "billing_obligations",
        "NOT rating_provenance_complete OR ("
        "(rating_tax_treatment_code IS NULL "
        "AND rating_tax_rate_id IS NULL "
        "AND rating_tax_rate_percent = 0) "
        "OR (rating_tax_treatment_code IS NOT NULL "
        "AND rating_tax_rate_id IS NOT NULL))",
    )
    op.alter_column(
        "billing_obligations",
        "rating_provenance_complete",
        server_default=None,
    )


def downgrade() -> None:
    for constraint_name in (
        "ck_billing_obligation_rating_tax_source",
        "ck_billing_obligation_rating_values",
        "ck_billing_obligation_rating_provenance_complete",
        "ck_billing_obligation_rating_coverage",
    ):
        op.drop_constraint(
            constraint_name,
            "billing_obligations",
            type_="check",
        )
    op.drop_constraint(
        "fk_billing_obligation_rating_tax_rate",
        "billing_obligations",
        type_="foreignkey",
    )
    for column_name in (
        "rating_input_fingerprint",
        "rating_tax_inclusive",
        "rating_tax_rate_percent",
        "rating_tax_rate_id",
        "rating_tax_treatment_code",
        "rating_proration_factor",
        "rating_rate_units",
        "rating_proration_policy",
        "rating_timezone_name",
        "rating_rate_quantity",
        "rating_rate_unit",
        "rating_rate_basis",
        "rating_quantity",
        "rating_unit_price",
        "rating_coverage_end",
        "rating_coverage_start",
        "rating_policy_version",
        "rating_provenance_complete",
    ):
        op.drop_column("billing_obligations", column_name)
