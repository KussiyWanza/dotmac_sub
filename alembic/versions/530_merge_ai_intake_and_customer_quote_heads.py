"""Merge AI intake and customer quote migration heads.

Revision ID: 530_merge_ai_intake_customer_quote
Revises: 529_conversational_ai_intake, 529_customer_quote_lead_linkage
Create Date: 2026-08-13
"""

from __future__ import annotations

revision = "530_merge_ai_intake_customer_quote"
down_revision = (
    "529_conversational_ai_intake",
    "529_customer_quote_lead_linkage",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
