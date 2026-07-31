"""add derived acceptance snapshot to field fiber test results

Revision ID: 450_fiber_test_acceptance
Revises: 449_fiber_splice_plans
Create Date: 2026-07-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "450_fiber_test_acceptance"
down_revision: str | None = "449_fiber_splice_plans"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "field_fiber_test_results",
        sa.Column("derived_passed", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "field_fiber_test_results",
        sa.Column("derived_verdict", sa.String(length=40), nullable=True),
    )
    op.add_column(
        "field_fiber_test_results",
        sa.Column("applied_minimum_db", sa.Float(), nullable=True),
    )
    op.add_column(
        "field_fiber_test_results",
        sa.Column("applied_maximum_db", sa.Float(), nullable=True),
    )
    op.add_column(
        "field_fiber_test_results",
        sa.Column("acceptance_policy_version", sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        "ck_field_fiber_tests_derived_verdict",
        "field_fiber_test_results",
        "derived_verdict IS NULL OR derived_verdict IN "
        "('within_threshold', 'exceeds_threshold', 'no_measurement', 'no_policy')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_field_fiber_tests_derived_verdict",
        "field_fiber_test_results",
        type_="check",
    )
    op.drop_column("field_fiber_test_results", "acceptance_policy_version")
    op.drop_column("field_fiber_test_results", "applied_maximum_db")
    op.drop_column("field_fiber_test_results", "applied_minimum_db")
    op.drop_column("field_fiber_test_results", "derived_verdict")
    op.drop_column("field_fiber_test_results", "derived_passed")
