"""Default invoice PDF payment presentment to Paystack.

Revision ID: 575_invoice_pdf_payment_presentment_paystack_default
Revises: 574_remove_ticket_assignment_permission
Create Date: 2026-09-04

Customer ``payment_method`` now controls normal invoice PDF presentment. This
keeps the existing setting as the fallback and moves its old bank-account
default to Paystack for unconfigured or blank customer records.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "575_invoice_pdf_payment_presentment_paystack_default"
down_revision: str | None = "574_remove_ticket_assignment_permission"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "domain_settings" not in tables:
        return
    bind.execute(
        sa.text(
            """
            UPDATE domain_settings
            SET value_text = 'paystack', updated_at = CURRENT_TIMESTAMP
            WHERE domain = 'billing'
              AND key = 'invoice_pdf_payment_presentment'
              AND is_active = true
              AND lower(trim(coalesce(value_text, ''))) IN ('', 'bank_account')
            """
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "domain_settings" not in tables:
        return
    bind.execute(
        sa.text(
            """
            UPDATE domain_settings
            SET value_text = 'bank_account', updated_at = CURRENT_TIMESTAMP
            WHERE domain = 'billing'
              AND key = 'invoice_pdf_payment_presentment'
              AND is_active = true
              AND lower(trim(coalesce(value_text, ''))) = 'paystack'
            """
        )
    )
