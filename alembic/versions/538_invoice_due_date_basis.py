"""Add versioned invoice due-date provenance and quarantine known ambiguity.

Revision ID: 538_invoice_due_date_basis
Revises: 537_team_inbox_plain_bodies
Create Date: 2026-08-16
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "538_invoice_due_date_basis"
down_revision = "537_team_inbox_plain_bodies"
branch_labels = None
depends_on = None

_BASIS_VALUES = (
    "contract_terms",
    "prepaid_service_period",
    "provider_observation",
    "approved_manual_override",
    "unknown_unverified",
)
_BASIS_TYPE = postgresql.ENUM(
    *_BASIS_VALUES,
    name="invoice_due_date_basis",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    postgresql.ENUM(
        *_BASIS_VALUES,
        name="invoice_due_date_basis",
    ).create(bind, checkfirst=True)
    op.add_column(
        "invoices",
        sa.Column("due_date_basis", _BASIS_TYPE, nullable=True),
    )
    op.add_column(
        "invoices",
        sa.Column("due_date_basis_ref", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "invoices",
        sa.Column("due_date_policy_version", sa.String(length=64), nullable=True),
    )

    # Read-only incident analysis established that this exact historical cohort
    # has no reconstructible contractual/operator provenance. Preserve the date
    # as observed evidence, but make the uncertainty explicit so Collections
    # cannot treat it as a lawful consequence trigger. The creation cutoff is
    # the analysis boundary: later, newly issued Aug 22 invoices must enter via
    # the native verified issuance contract instead of this historical repair.
    op.execute(
        sa.text(
            """
            UPDATE invoices
               SET due_date_basis = 'unknown_unverified'
             WHERE is_active
               AND due_at IS NOT NULL
               AND (due_at AT TIME ZONE 'Africa/Lagos')::date = DATE '2026-08-22'
               AND created_at < TIMESTAMPTZ '2026-08-17 00:00:00+01'
            """
        )
    )

    op.create_check_constraint(
        "ck_invoices_verified_due_date_basis",
        "invoices",
        "due_date_basis IS NULL OR "
        "due_date_basis = 'unknown_unverified' OR "
        "(due_at IS NOT NULL AND "
        "(status = 'draft' OR (issued_at IS NOT NULL AND due_at >= issued_at)) "
        "AND due_date_basis_ref IS NOT NULL AND "
        "length(trim(due_date_basis_ref)) > 0 AND "
        "due_date_policy_version IS NOT NULL AND "
        "length(trim(due_date_policy_version)) > 0)",
    )
    op.create_index(
        "ix_invoices_due_date_basis_due_at",
        "invoices",
        ["due_date_basis", "due_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_invoices_due_date_basis_due_at", table_name="invoices")
    op.drop_constraint(
        "ck_invoices_verified_due_date_basis",
        "invoices",
        type_="check",
    )
    op.drop_column("invoices", "due_date_policy_version")
    op.drop_column("invoices", "due_date_basis_ref")
    op.drop_column("invoices", "due_date_basis")
    postgresql.ENUM(name="invoice_due_date_basis").drop(op.get_bind(), checkfirst=True)
