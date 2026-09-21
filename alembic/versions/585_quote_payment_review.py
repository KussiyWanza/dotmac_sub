"""Require staff review before customer Quote deposit payment.

Revision ID: 585_quote_payment_review
Revises: 584_customer_backed_quote_delivery
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "585_quote_payment_review"
down_revision: str | None = "584_customer_backed_quote_delivery"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None

PERMISSION_KEY = "sales:quote:review"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.add_column(
        "quotes",
        sa.Column(
            "payment_review_status",
            sa.String(length=20),
            nullable=False,
            server_default="pending",
        ),
    )
    op.add_column(
        "quotes",
        sa.Column(
            "payment_review_revision",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "quotes",
        sa.Column(
            "payment_reviewed_by_system_user_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.add_column(
        "quotes",
        sa.Column("payment_reviewed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "quotes", sa.Column("payment_review_reason", sa.Text(), nullable=True)
    )
    op.add_column(
        "quotes",
        sa.Column("payment_review_fingerprint", sa.String(length=64), nullable=True),
    )
    op.create_foreign_key(
        "fk_quotes_payment_reviewer_system_user",
        "quotes",
        "system_users",
        ["payment_reviewed_by_system_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_quotes_payment_review_revision_nonnegative",
        "quotes",
        "payment_review_revision >= 0",
    )
    op.create_check_constraint(
        "ck_quotes_payment_review_status",
        "quotes",
        "payment_review_status IN ('pending', 'approved', 'rejected')",
    )
    op.create_check_constraint(
        "ck_quotes_payment_review_current_state",
        "quotes",
        "(payment_review_status = 'pending' AND "
        "payment_reviewed_by_system_user_id IS NULL AND "
        "payment_reviewed_at IS NULL AND payment_review_fingerprint IS NULL) OR "
        "(payment_review_status IN ('approved', 'rejected') AND "
        "payment_review_revision > 0 AND "
        "payment_reviewed_by_system_user_id IS NOT NULL AND "
        "payment_reviewed_at IS NOT NULL AND "
        "payment_review_fingerprint IS NOT NULL)",
    )
    op.create_table(
        "quote_payment_reviews",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False
        ),
        sa.Column("quote_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("decision", sa.String(length=20), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "reviewed_by_system_user_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("quote_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("command_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("command_fingerprint", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "revision > 0", name="ck_quote_payment_reviews_revision_positive"
        ),
        sa.CheckConstraint(
            "decision IN ('approve', 'reject')",
            name="ck_quote_payment_reviews_decision",
        ),
        sa.CheckConstraint(
            "length(command_fingerprint) = 64 AND length(quote_fingerprint) = 64",
            name="ck_quote_payment_reviews_fingerprints",
        ),
        sa.ForeignKeyConstraint(["quote_id"], ["quotes.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["reviewed_by_system_user_id"],
            ["system_users.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "quote_id", "revision", name="uq_quote_payment_reviews_revision"
        ),
        sa.UniqueConstraint("command_id", name="uq_quote_payment_reviews_command_id"),
    )
    op.create_index(
        "ix_quote_payment_reviews_reviewed_at",
        "quote_payment_reviews",
        ["reviewed_at"],
    )
    op.create_index(
        "ix_quote_payment_reviews_reviewer",
        "quote_payment_reviews",
        ["reviewed_by_system_user_id"],
    )

    bind = op.get_bind()
    existing = bind.execute(
        sa.text("SELECT id FROM permissions WHERE key = :key"),
        {"key": PERMISSION_KEY},
    ).scalar()
    if existing is None:
        now = datetime.now(UTC)
        bind.execute(
            sa.text(
                "INSERT INTO permissions "
                "(id, key, description, is_active, is_ui_assignable, "
                "created_at, updated_at) "
                "VALUES (:id, :key, :description, true, true, :now, :now)"
            ),
            {
                "id": str(uuid4()),
                "key": PERMISSION_KEY,
                "description": "Approve or reject Quotes for customer payment",
                "now": now,
            },
        )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    bind = op.get_bind()
    permission_id = bind.execute(
        sa.text("SELECT id FROM permissions WHERE key = :key"),
        {"key": PERMISSION_KEY},
    ).scalar()
    if permission_id is not None:
        for table in (
            "role_permissions",
            "subscriber_permissions",
            "system_user_permissions",
        ):
            bind.execute(
                sa.text(f"DELETE FROM {table} WHERE permission_id = :permission_id"),
                {"permission_id": permission_id},
            )
        bind.execute(
            sa.text("DELETE FROM permissions WHERE id = :permission_id"),
            {"permission_id": permission_id},
        )
    op.drop_index(
        "ix_quote_payment_reviews_reviewer", table_name="quote_payment_reviews"
    )
    op.drop_index(
        "ix_quote_payment_reviews_reviewed_at", table_name="quote_payment_reviews"
    )
    op.drop_table("quote_payment_reviews")
    op.drop_constraint(
        "ck_quotes_payment_review_current_state", "quotes", type_="check"
    )
    op.drop_constraint("ck_quotes_payment_review_status", "quotes", type_="check")
    op.drop_constraint(
        "ck_quotes_payment_review_revision_nonnegative", "quotes", type_="check"
    )
    op.drop_constraint(
        "fk_quotes_payment_reviewer_system_user", "quotes", type_="foreignkey"
    )
    op.drop_column("quotes", "payment_review_fingerprint")
    op.drop_column("quotes", "payment_review_reason")
    op.drop_column("quotes", "payment_reviewed_at")
    op.drop_column("quotes", "payment_reviewed_by_system_user_id")
    op.drop_column("quotes", "payment_review_revision")
    op.drop_column("quotes", "payment_review_status")
