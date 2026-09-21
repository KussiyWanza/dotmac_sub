"""Allow subscriber-backed Quote delivery without a Party contact point.

Revision ID: 584_customer_backed_quote_delivery
Revises: 583_staff_expense_requesters
"""

import sqlalchemy as sa

from alembic import op

revision: str = "584_customer_backed_quote_delivery"
down_revision: str | None = "583_staff_expense_requesters"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.add_column(
        "quote_delivery_requests",
        sa.Column("recipient_masked", sa.String(length=320), nullable=True),
    )
    op.alter_column(
        "quote_delivery_requests",
        "recipient_contact_point_id",
        existing_type=sa.UUID(),
        nullable=True,
    )
    op.create_check_constraint(
        "ck_quote_delivery_requests_recipient_evidence",
        "quote_delivery_requests",
        "recipient_contact_point_id IS NOT NULL OR recipient_masked IS NOT NULL",
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    bind = op.get_bind()
    direct_recipients = bind.execute(
        sa.text(
            "SELECT count(*) FROM quote_delivery_requests "
            "WHERE recipient_contact_point_id IS NULL"
        )
    ).scalar_one()
    if direct_recipients:
        raise RuntimeError(
            "Cannot restore Party-only Quote delivery while subscriber-backed "
            "delivery evidence exists"
        )
    op.drop_constraint(
        "ck_quote_delivery_requests_recipient_evidence",
        "quote_delivery_requests",
        type_="check",
    )
    op.alter_column(
        "quote_delivery_requests",
        "recipient_contact_point_id",
        existing_type=sa.UUID(),
        nullable=False,
    )
    op.drop_column("quote_delivery_requests", "recipient_masked")
