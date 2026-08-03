"""Make Quotes Lead-first and configure template WorkOrder automation.

Quote authoring no longer requires a Subscriber account. The account link is
attached by the atomic quote-acceptance coordinator. Project Type becomes a
first-class Quote field and existing metadata values are backfilled. Project
template tasks gain explicit WorkOrder automation controls; false preserves
every existing template's behavior.

Expand/backfill/cutover: the nullable Quote account column is backward
compatible and legacy metadata Project Types are copied into the typed column;
no commercial lifecycle state is changed. Existing null Lead links remain
legacy debt, while the application command rejects them for every new write.

Revision ID: 462_quote_acceptance_sales_conversion
Revises: 461_outage_incident_ticket_links
Create Date: 2026-08-02
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "462_quote_acceptance_sales_conversion"
down_revision = "461_outage_incident_ticket_links"
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
        "quotes",
        sa.Column("project_type", sa.String(length=60), nullable=True),
    )
    quotes = sa.table(
        "quotes",
        sa.column("project_type", sa.String(length=60)),
        sa.column("metadata", sa.JSON()),
    )
    op.execute(
        quotes.update()
        .where(quotes.c.project_type.is_(None))
        .where(quotes.c.metadata.isnot(None))
        .values(project_type=quotes.c.metadata["project_type"].as_string())
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
    quotes = sa.table(
        "quotes",
        sa.column("subscriber_id", sa.Uuid()),
        sa.column("project_type", sa.String(length=60)),
        sa.column("metadata", sa.JSON()),
    )
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
    unprojected_types = op.get_bind().scalar(
        sa.select(sa.func.count())
        .select_from(quotes)
        .where(quotes.c.project_type.isnot(None))
        .where(
            sa.or_(
                quotes.c.metadata.is_(None),
                sa.func.coalesce(quotes.c.metadata["project_type"].as_string(), "")
                != quotes.c.project_type,
            )
        )
    )
    if int(unprojected_types or 0):
        raise RuntimeError(
            "Downgrade blocked: Quote Project Types without an exact metadata "
            "projection must be reviewed before dropping the typed column"
        )
    op.drop_column("project_template_tasks", "work_order_requires_as_built_evidence")
    op.drop_column("project_template_tasks", "auto_create_work_order")
    op.drop_column("quotes", "project_type")
    op.alter_column(
        "quotes",
        "subscriber_id",
        existing_type=sa.Uuid(),
        nullable=False,
    )
