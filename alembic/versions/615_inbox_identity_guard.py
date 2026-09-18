"""Add the operator-controlled Inbox identity guard setting."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "615_inbox_identity_guard"
down_revision: str | None = "614_automation_runtime_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {
        column["name"]
        for column in inspector.get_columns("inbox_customer_completion_policy_versions")
    }
    if "identity_guard_enabled" not in columns:
        op.add_column(
            "inbox_customer_completion_policy_versions",
            sa.Column(
                "identity_guard_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            ),
        )
        op.alter_column(
            "inbox_customer_completion_policy_versions",
            "identity_guard_enabled",
            server_default=None,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {
        column["name"]
        for column in inspector.get_columns("inbox_customer_completion_policy_versions")
    }
    if "identity_guard_enabled" in columns:
        op.drop_column(
            "inbox_customer_completion_policy_versions", "identity_guard_enabled"
        )
