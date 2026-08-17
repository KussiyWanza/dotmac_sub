"""Reject new active subscriptions without a billing anchor.

Revision ID: 539_active_sub_billing_anchor
Revises: 538_invoice_due_date_basis
Create Date: 2026-08-16
"""

from __future__ import annotations

from alembic import op

revision = "539_active_sub_billing_anchor"
down_revision = "538_invoice_due_date_basis"
branch_labels = None
depends_on = None

_CONSTRAINT = "ck_subscriptions_active_billing_anchor"


def upgrade() -> None:
    # The known legacy NULL-anchor cohort is review/repair stock. NOT VALID
    # preserves those rows while PostgreSQL immediately rejects any new or
    # changed active row that would reproduce the defect.
    op.execute(
        f"""
        ALTER TABLE subscriptions
        ADD CONSTRAINT {_CONSTRAINT}
        CHECK (
            status != 'active'
            OR (start_at IS NOT NULL AND next_billing_at IS NOT NULL)
        ) NOT VALID
        """
    )


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "subscriptions", type_="check")
