"""Add explicit Phase 2 billing-verification classifications.

The generic cutover run needs distinct evidence for approved new-cadence
differences and for obligation gaps/overlaps. These columns remain migration
evidence only; no billing authority or read path moves.

Revision ID: 438_billing_phase2_verification_counts
Revises: 437_add_pon_port_admin_enabled
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "438_billing_phase2_verification_counts"
down_revision = "437_add_pon_port_admin_enabled"
branch_labels = None
depends_on = None


_COUNTS_CHECK = (
    "cohort_count >= 0 AND covered_count >= 0 "
    "AND unresolved_count >= 0 AND ambiguous_count >= 0 "
    "AND unexpected_unlinked_count >= 0 AND duplicate_count >= 0 "
    "AND shadow_variance_count >= 0 "
    "AND expected_difference_count >= 0 AND gap_count >= 0 "
    "AND overlap_count >= 0"
)


def upgrade() -> None:
    for column_name in (
        "expected_difference_count",
        "gap_count",
        "overlap_count",
    ):
        op.add_column(
            "billing_cutover_verification_runs",
            sa.Column(
                column_name,
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
        )
    op.drop_constraint(
        "ck_billing_cutover_verification_nonnegative_counts",
        "billing_cutover_verification_runs",
        type_="check",
    )
    op.create_check_constraint(
        "ck_billing_cutover_verification_nonnegative_counts",
        "billing_cutover_verification_runs",
        _COUNTS_CHECK,
    )
    for column_name in (
        "expected_difference_count",
        "gap_count",
        "overlap_count",
    ):
        op.alter_column(
            "billing_cutover_verification_runs",
            column_name,
            server_default=None,
        )


def downgrade() -> None:
    op.drop_constraint(
        "ck_billing_cutover_verification_nonnegative_counts",
        "billing_cutover_verification_runs",
        type_="check",
    )
    for column_name in (
        "overlap_count",
        "gap_count",
        "expected_difference_count",
    ):
        op.drop_column("billing_cutover_verification_runs", column_name)
    op.create_check_constraint(
        "ck_billing_cutover_verification_nonnegative_counts",
        "billing_cutover_verification_runs",
        "cohort_count >= 0 AND covered_count >= 0 "
        "AND unresolved_count >= 0 AND ambiguous_count >= 0 "
        "AND unexpected_unlinked_count >= 0 AND duplicate_count >= 0 "
        "AND shadow_variance_count >= 0",
    )
