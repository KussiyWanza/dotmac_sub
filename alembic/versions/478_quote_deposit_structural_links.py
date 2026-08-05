"""Add structural Quote-to-Invoice and checkout-to-Invoice identities.

Revision ID: 478_quote_deposit_structural_links
Revises: 477_quote_send_permission
Create Date: 2026-08-05

ADR 0007 makes JSON metadata provenance-only. This expand/backfill migration
materializes the existing quotation-deposit and invoice-checkout identities as
foreign-key-backed links before application readers cut over to them.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "478_quote_deposit_structural_links"
down_revision: str | None = "477_quote_send_permission"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _backfill_invoice_checkout_links() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    rows = bind.execute(
        sa.text(
            """
            SELECT t.id AS intent_id, i.id AS invoice_id
            FROM topup_intents AS t
            JOIN invoices AS i
              ON i.id::text = t.metadata ->> 'invoice_id'
            WHERE t.invoice_id IS NULL
              AND t.metadata ->> 'payment_flow' = 'invoice_payment'
            ORDER BY t.id
            """
        )
    ).mappings()
    payloads = [dict(row) for row in rows]
    if payloads:
        bind.execute(
            sa.text(
                """
                UPDATE topup_intents
                SET invoice_id = :invoice_id
                WHERE id = :intent_id AND invoice_id IS NULL
                """
            ),
            payloads,
        )


def _backfill_quote_deposit_links() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    rows = bind.execute(
        sa.text(
            """
            SELECT i.id AS invoice_id,
                   q.id AS quote_id,
                   i.account_id AS account_id,
                   i.created_at AS created_at
            FROM invoices AS i
            JOIN quotes AS q
              ON q.id::text = i.metadata ->> 'quote_id'
             AND q.subscriber_id = i.account_id
            WHERE i.metadata ->> 'payment_flow' = 'quote_deposit'
            ORDER BY i.created_at, i.id
            """
        )
    ).mappings()
    payloads = [
        {
            "id": uuid.uuid4(),
            "invoice_id": row["invoice_id"],
            "quote_id": row["quote_id"],
            "account_id": row["account_id"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]
    if payloads:
        bind.execute(
            sa.text(
                """
                INSERT INTO quote_deposit_invoice_links
                  (id, invoice_id, quote_id, account_id, created_at)
                VALUES
                  (:id, :invoice_id, :quote_id, :account_id, :created_at)
                ON CONFLICT (invoice_id) DO NOTHING
                """
            ),
            payloads,
        )


def upgrade() -> None:
    op.add_column(
        "topup_intents",
        sa.Column(
            "invoice_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_topup_intents_invoice_id_invoices",
        "topup_intents",
        "invoices",
        ["invoice_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_topup_intents_invoice_id",
        "topup_intents",
        ["invoice_id"],
    )

    op.create_table(
        "quote_deposit_invoice_links",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "quote_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "invoice_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["quote_id"],
            ["quotes.id"],
            name="fk_quote_deposit_invoice_links_quote_id_quotes",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["invoice_id"],
            ["invoices.id"],
            name="fk_quote_deposit_invoice_links_invoice_id_invoices",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["subscribers.id"],
            name="fk_quote_deposit_invoice_links_account_id_subscribers",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_quote_deposit_invoice_links"),
        sa.UniqueConstraint(
            "invoice_id",
            name="uq_quote_deposit_invoice_links_invoice_id",
        ),
    )
    op.create_index(
        "ix_quote_deposit_invoice_links_quote_id",
        "quote_deposit_invoice_links",
        ["quote_id"],
    )

    _backfill_invoice_checkout_links()
    _backfill_quote_deposit_links()


def downgrade() -> None:
    op.drop_index(
        "ix_quote_deposit_invoice_links_quote_id",
        table_name="quote_deposit_invoice_links",
    )
    op.drop_table("quote_deposit_invoice_links")
    op.drop_index("ix_topup_intents_invoice_id", table_name="topup_intents")
    op.drop_constraint(
        "fk_topup_intents_invoice_id_invoices",
        "topup_intents",
        type_="foreignkey",
    )
    op.drop_column("topup_intents", "invoice_id")
