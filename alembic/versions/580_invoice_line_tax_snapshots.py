"""Add immutable invoice-line tax snapshots for ERP accounting projection.

Existing taxed lines deliberately remain unsnapshotted. The versioned ERP
projection reports those rows as ``tax_snapshot_missing`` instead of guessing
historical tax facts from today's mutable tax-rate configuration.

Revision ID: 580_invoice_line_tax_snapshots
Revises: 579_erp_sync_retry
"""

import sqlalchemy as sa

from alembic import op

revision: str = "580_invoice_line_tax_snapshots"
down_revision: str | None = "579_erp_sync_retry"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None

_CONSTRAINT = "ck_invoice_lines_tax_snapshot_complete"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.add_column(
        "invoice_lines",
        sa.Column("tax_rate_snapshot_version", sa.Integer(), nullable=True),
    )
    op.add_column(
        "invoice_lines",
        sa.Column("tax_rate_code_snapshot", sa.String(length=40), nullable=True),
    )
    op.add_column(
        "invoice_lines",
        sa.Column(
            "tax_rate_percent_snapshot", sa.Numeric(precision=6, scale=4), nullable=True
        ),
    )
    op.add_column(
        "invoice_lines",
        sa.Column("tax_rate_is_active_snapshot", sa.Boolean(), nullable=True),
    )
    # Some composed-module bootstrap paths materialize the model constraint
    # before provider migrations run. Preserve that validated constraint when
    # present; add the same invariant as NOT VALID on ordinary upgrades.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conrelid = 'invoice_lines'::regclass
                  AND conname = '{_CONSTRAINT}'
            ) THEN
                ALTER TABLE invoice_lines
                ADD CONSTRAINT {_CONSTRAINT} CHECK (
                    (tax_rate_snapshot_version IS NULL
                     AND tax_rate_code_snapshot IS NULL
                     AND tax_rate_percent_snapshot IS NULL
                     AND tax_rate_is_active_snapshot IS NULL)
                    OR (tax_rate_snapshot_version = 1
                        AND tax_rate_id IS NOT NULL
                        AND tax_rate_percent_snapshot IS NOT NULL
                        AND tax_rate_is_active_snapshot IS NOT NULL)
                ) NOT VALID;
            END IF;
        END
        $$
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.scalar(
        sa.text(
            "SELECT EXISTS ("
            "SELECT 1 FROM invoice_lines "
            "WHERE tax_rate_snapshot_version IS NOT NULL"
            ")"
        )
    ):
        raise RuntimeError(
            "Invoice tax snapshots exist; retain the additive schema and roll forward"
        )
    op.drop_constraint(_CONSTRAINT, "invoice_lines", type_="check")
    op.drop_column("invoice_lines", "tax_rate_is_active_snapshot")
    op.drop_column("invoice_lines", "tax_rate_percent_snapshot")
    op.drop_column("invoice_lines", "tax_rate_code_snapshot")
    op.drop_column("invoice_lines", "tax_rate_snapshot_version")
