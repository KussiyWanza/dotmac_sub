"""Make Quotes Lead-first and configure template WorkOrder automation.

Quote authoring no longer requires a Subscriber account. The account link is
attached by the atomic quote-acceptance coordinator. Project template tasks
gain explicit WorkOrder automation controls; false preserves every existing
template's behavior.

Expand/cutover only: the nullable Quote account column is backward compatible,
and no historical Quote is rewritten. Existing null Lead links remain legacy
debt, while the application command rejects them for every new write.

Revision ID: 457_quote_acceptance_sales_conversion
Revises: 456_ont_wan_service_intent_owner
Create Date: 2026-08-02
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "457_quote_acceptance_sales_conversion"
down_revision = "456_ont_wan_service_intent_owner"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "quotes",
        "subscriber_id",
        existing_type=sa.Uuid(),
        nullable=True,
    )
    op.add_column(
        "project_template_tasks",
        sa.Column(
            "auto_create_work_order",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "project_template_tasks",
        sa.Column(
            "work_order_requires_as_built_evidence",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )


def downgrade() -> None:
    quotes = sa.table("quotes", sa.column("subscriber_id", sa.Uuid()))
    null_accounts = op.get_bind().scalar(
        sa.select(sa.func.count())
        .select_from(quotes)
        .where(quotes.c.subscriber_id.is_(None))
    )
    if int(null_accounts or 0):
        raise RuntimeError(
            "Downgrade blocked: Lead-backed Quotes without Subscriber accounts "
            "must be reviewed before restoring the legacy NOT NULL contract"
        )
    op.drop_column("project_template_tasks", "work_order_requires_as_built_evidence")
    op.drop_column("project_template_tasks", "auto_create_work_order")
    op.alter_column(
        "quotes",
        "subscriber_id",
        existing_type=sa.Uuid(),
        nullable=False,
    )
