"""Reconcile legacy support narrative as internal-only.

Revision ID: 503_reconcile_ticket_portal_visibility
Revises: 502_open_setting_domain_vocabulary
Create Date: 2026-08-08

PR #69 exposed local ticket descriptions and comments whose legacy default was
``is_internal = false``. PR #2108 corrected only new comment entry. The old CRM
had no customer ticket timeline, so no pre-cutover narrative has affirmative
publication evidence. This forward-only reconciliation classifies every
description and comment present at migration time as internal. New Selfcare
customer submissions explicitly opt into portal visibility after this runs.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "503_reconcile_ticket_portal_visibility"
down_revision: str | None = "502_open_setting_domain_vocabulary"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.execute(sa.text("SET LOCAL statement_timeout = '60s'"))

    op.add_column(
        "support_tickets",
        sa.Column(
            "description_is_internal",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    op.execute(
        sa.text(
            "UPDATE support_ticket_comments "
            "SET is_internal = true "
            "WHERE is_internal = false"
        )
    )
    op.alter_column(
        "support_ticket_comments",
        "is_internal",
        existing_type=sa.Boolean(),
        nullable=False,
        server_default=sa.true(),
    )


def downgrade() -> None:
    """Move the marker only; legacy publication intent cannot be reconstructed."""
