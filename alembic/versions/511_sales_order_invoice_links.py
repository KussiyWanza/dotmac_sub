"""Structural SalesOrder <-> Invoice link, replacing the metadata join.

Sale-to-money was joined through ``Project.metadata_``: a JSON string
comparison on ``sales_order_id`` plus ``selfcare_installation_invoice_id``,
with a full-scan fallback. Settlement evidence therefore could not be
attributed to the commercial order, and nothing could derive the order's
financial status from it.

The table lives in Sales. ``invoices`` gains no ``sales_order_id`` column, so
Finance keeps owning settlement without depending on Sales.

Expand only. The historical metadata keys are left in place and are still the
read path; this migration backfills what they already assert so a parity check
can compare the two before any read cutover.

Revision ID: 511_sales_order_invoice_links
Revises: 510_inbox_manager_ai_permission
"""

from __future__ import annotations

import logging

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "511_sales_order_invoice_links"
down_revision = "510_inbox_manager_ai_permission"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sales_order_invoice_links",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "sales_order_id",
            UUID(as_uuid=True),
            sa.ForeignKey("sales_orders.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "invoice_id",
            UUID(as_uuid=True),
            sa.ForeignKey("invoices.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "account_id",
            UUID(as_uuid=True),
            sa.ForeignKey("subscribers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column(
            "origin", sa.String(length=16), nullable=False, server_default="native"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "invoice_id", name="uq_sales_order_invoice_links_invoice_id"
        ),
        sa.CheckConstraint(
            "purpose IN ('installation', 'deposit', 'subscription')",
            name="ck_sales_order_invoice_links_purpose",
        ),
        sa.CheckConstraint(
            "origin IN ('native', 'backfill')",
            name="ck_sales_order_invoice_links_origin",
        ),
    )
    op.create_index(
        "ix_sales_order_invoice_links_sales_order_id",
        "sales_order_invoice_links",
        ["sales_order_id"],
    )

    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # The backfill reads a JSON path and relies on ON CONFLICT; the SQLite
        # metadata lane has no rows to recover.
        return

    # The order side comes from ``projects.sales_order_id`` — a real FK with a
    # unique constraint. Only the invoice id has no structural column yet, so
    # it is the one value recovered from metadata, which is what this migration
    # exists to replace.
    #
    # Only rows whose invoice, sales order and account all still resolve are
    # linked; a dangling id is left for review rather than forced through a
    # RESTRICT foreign key. DISTINCT ON keeps one row per invoice, honouring
    # the unique constraint when two projects name the same invoice.
    op.execute(
        sa.text(
            """
            INSERT INTO sales_order_invoice_links
                (id, sales_order_id, invoice_id, account_id, purpose, origin, created_at)
            SELECT DISTINCT ON (i.id)
                gen_random_uuid(),
                so.id,
                i.id,
                so.subscriber_id,
                'installation',
                'backfill',
                now()
            FROM projects p
            JOIN sales_orders so
              ON so.id = p.sales_order_id
            JOIN invoices i
              ON i.id = (p."metadata" ->> 'selfcare_installation_invoice_id')::uuid
            JOIN subscribers s
              ON s.id = so.subscriber_id
            WHERE p.sales_order_id IS NOT NULL
              AND p."metadata" ->> 'selfcare_installation_invoice_id' IS NOT NULL
              AND p."metadata" ->> 'selfcare_installation_invoice_id' ~
                  '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
            ORDER BY i.id, p.created_at DESC
            ON CONFLICT ON CONSTRAINT uq_sales_order_invoice_links_invoice_id
            DO NOTHING
            """
        )
    )

    # Cutover gate. `metadata_without_link` is what a read cutover would lose:
    # while it is non-zero, moving reads onto the link drops the invoice
    # association for those orders and they read as never invoiced.
    #
    # Reported here rather than from a service because ADR 0007 makes metadata
    # provenance only — a permanent reader comparing the two would be a new
    # metadata financial-identity read, which `test_billing_target_architecture`
    # rightly refuses. Parity is migration-time evidence, so it belongs to the
    # migration that creates it.
    parity = bind.execute(
        sa.text(
            """
            WITH joined AS (
                SELECT (p."metadata" ->> 'selfcare_installation_invoice_id')::uuid
                           AS invoice_id
                FROM projects p
                WHERE p.sales_order_id IS NOT NULL
                  AND p."metadata" ->> 'selfcare_installation_invoice_id' IS NOT NULL
                  AND p."metadata" ->> 'selfcare_installation_invoice_id' ~
                      '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
            )
            SELECT
                (SELECT count(*) FROM joined) AS metadata_joins,
                (SELECT count(*) FROM sales_order_invoice_links) AS links,
                (SELECT count(*) FROM joined j
                   WHERE NOT EXISTS (
                     SELECT 1 FROM sales_order_invoice_links l
                      WHERE l.invoice_id = j.invoice_id)) AS metadata_without_link
            """
        )
    ).one()
    logging.getLogger("alembic.runtime.migration").warning(
        "sales_order_invoice_link_backfill_parity: metadata_joins=%s links=%s "
        "metadata_without_link=%s (read cutover is gated on the last being 0)",
        parity.metadata_joins,
        parity.links,
        parity.metadata_without_link,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_sales_order_invoice_links_sales_order_id",
        table_name="sales_order_invoice_links",
    )
    op.drop_table("sales_order_invoice_links")
