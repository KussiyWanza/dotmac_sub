"""Add subscription/period evidence to prepaid_draft_reconciliation_exceptions.

Part of the single-owner funding-consequence cutover
(``app.services.prepaid_service_renewals``). A review item can now be raised
for a renewal invoice this owner itself just constructed -- before any
pre-existing customer draft was even involved -- so the exact subscription and
funded period the review item blocks needs to be recorded alongside the
invoice, not inferred from it later. Additive and nullable: every row raised
before this column existed stays valid with no backfill.

Revision ID: 599_prepaid_draft_exception_period_evidence
Revises: 598_opening_corrections
Create Date: 2026-09-12

Re-parented (2026-09, post-merge) from ``597_prepaid_draft_repair_permission``
to ``598_opening_corrections``: PR #3099 landed on ``main`` first and its own
migration already claims ``597`` as its parent, so this chain now sits
linearly after it rather than branching independently off the same revision
(which would otherwise leave ``597`` with two children and an unresolvable
multi-head alembic state). Purely a chain re-pointing -- no schema change
here is altered by this.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "599_prepaid_draft_exception_period_evidence"
down_revision: str | None = "598_opening_corrections"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "prepaid_draft_reconciliation_exceptions"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(
            "subscription_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subscriptions.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )
    op.add_column(
        _TABLE, sa.Column("period_start", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        _TABLE, sa.Column("period_end", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index(
        "ix_prepaid_draft_exception_subscription",
        _TABLE,
        ["subscription_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_prepaid_draft_exception_subscription", table_name=_TABLE)
    op.drop_column(_TABLE, "period_end")
    op.drop_column(_TABLE, "period_start")
    op.drop_column(_TABLE, "subscription_id")
