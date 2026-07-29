"""Add physical administrative state to PON ports.

Revision ID: 437_add_pon_port_admin_enabled
Revises: 436_billing_shadow_verification_evidence
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "437_add_pon_port_admin_enabled"
down_revision: str | None = "436_billing_shadow_verification_evidence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "pon_ports",
        sa.Column(
            "admin_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )


def downgrade() -> None:
    op.drop_column("pon_ports", "admin_enabled")
