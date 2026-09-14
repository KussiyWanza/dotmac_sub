"""Add selected approver and per-expense payment destination evidence.

Revision ID: 594_field_expense_destination
Revises: 593_field_location_ping_client_observation_id
Create Date: 2026-09-10
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "594_field_expense_destination"
down_revision = "593_field_location_ping_client_observation_id"
branch_labels = None
depends_on = None

_TABLE = "field_expense_requests"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column("selected_approver_erp_id", postgresql.UUID(as_uuid=True)),
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "selected_approver_system_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("system_users.id"),
        ),
    )
    op.add_column(_TABLE, sa.Column("selected_approver_name", sa.String(200)))
    op.add_column(_TABLE, sa.Column("selected_approver_email", sa.String(255)))
    op.add_column(_TABLE, sa.Column("payment_destination_mode", sa.String(30)))
    op.add_column(_TABLE, sa.Column("payment_destination_token", sa.Text()))
    op.add_column(_TABLE, sa.Column("recipient_bank_code", sa.String(20)))
    op.add_column(_TABLE, sa.Column("recipient_bank_name", sa.String(100)))
    op.add_column(_TABLE, sa.Column("recipient_account_last4", sa.String(4)))
    op.add_column(_TABLE, sa.Column("verified_beneficiary_name", sa.String(150)))
    op.add_column(
        _TABLE, sa.Column("destination_verified_at", sa.DateTime(timezone=True))
    )
    op.add_column(
        _TABLE, sa.Column("destination_token_expires_at", sa.DateTime(timezone=True))
    )
    op.add_column(
        _TABLE, sa.Column("payment_destination_locked_at", sa.DateTime(timezone=True))
    )
    op.create_index(
        "ix_field_expense_requests_selected_approver",
        _TABLE,
        ["selected_approver_system_user_id"],
    )
    op.create_check_constraint(
        "ck_field_expense_requests_destination_mode",
        _TABLE,
        "payment_destination_mode IS NULL OR payment_destination_mode IN ('erp_profile', 'expense_override')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_field_expense_requests_destination_mode", _TABLE, type_="check"
    )
    op.drop_index("ix_field_expense_requests_selected_approver", table_name=_TABLE)
    for column in (
        "payment_destination_locked_at",
        "destination_token_expires_at",
        "destination_verified_at",
        "verified_beneficiary_name",
        "recipient_account_last4",
        "recipient_bank_name",
        "recipient_bank_code",
        "payment_destination_token",
        "payment_destination_mode",
        "selected_approver_email",
        "selected_approver_name",
        "selected_approver_system_user_id",
        "selected_approver_erp_id",
    ):
        op.drop_column(_TABLE, column)
