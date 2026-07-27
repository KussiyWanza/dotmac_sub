"""Allow vendor-less intake evidence on the installation-project lifecycle.

Publishing a project for competitive bidding is a lifecycle decision taken
*before* any vendor exists, so the append-only evidence row cannot name one.
Every transition from ``assigned``/``approved`` onward still carries its vendor;
only the intake decision may be NULL.

Revision ID: 425_vendor_project_intake_evidence
Revises: 424_proposed_route_review_evidence
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "425_vendor_project_intake_evidence"
down_revision = "424_proposed_route_review_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "installation_project_lifecycle_events",
        "vendor_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )


def downgrade() -> None:
    # Vendor-less intake evidence cannot be represented under the old shape.
    # Drop only those rows so the NOT NULL can be restored; every other
    # lifecycle event already carries its vendor.
    op.execute(
        sa.text(
            "DELETE FROM installation_project_lifecycle_events WHERE vendor_id IS NULL"
        )
    )
    op.alter_column(
        "installation_project_lifecycle_events",
        "vendor_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )
