"""add declared cable construction for tube/core color derivation

Revision ID: 448_fiber_segment_color_construction
Revises: 447_payment_proof_corrections
Create Date: 2026-07-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "448_fiber_segment_color_construction"
down_revision: str | None = "447_payment_proof_corrections"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "fiber_segments",
        sa.Column(
            "fibers_per_tube",
            sa.Integer(),
            nullable=True,
            comment="Loose-tube construction: fiber cores per buffer tube",
        ),
    )
    op.add_column(
        "fiber_segments",
        sa.Column(
            "color_standard",
            sa.String(length=40),
            nullable=True,
            comment="Declared color-code standard (FiberColorStandard vocabulary)",
        ),
    )
    op.create_check_constraint(
        "ck_fiber_segments_fibers_per_tube_positive",
        "fiber_segments",
        "fibers_per_tube IS NULL OR fibers_per_tube > 0",
    )
    op.create_check_constraint(
        "ck_fiber_segments_color_standard_known",
        "fiber_segments",
        "color_standard IS NULL OR color_standard IN ('eia_tia_598')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_fiber_segments_color_standard_known", "fiber_segments", type_="check"
    )
    op.drop_constraint(
        "ck_fiber_segments_fibers_per_tube_positive", "fiber_segments", type_="check"
    )
    op.drop_column("fiber_segments", "color_standard")
    op.drop_column("fiber_segments", "fibers_per_tube")
