"""positive, expiring ONT reconcile cohort admissions

The fleet-wide ``network.ont_reconcile`` control remains an emergency stop,
but enabling it is no longer positive authority to walk the fleet. Automatic
reconciliation requires one reviewed, unexpired admission per ONT, and the
owner rechecks that permission under the ONT row lock before device contact.

Admissions expire closed: elapsed authority stops working without a writer.
Expired and revoked rows remain as evidence, while the partial unique index
allows a newly reviewed admission to supersede that history.

Revision ID: 490_ont_reconcile_positive_admission
Revises: 489_unique_sellable_offer_name
Create Date: 2026-08-05
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "490_ont_reconcile_positive_admission"
down_revision = "489_unique_sellable_offer_name"
branch_labels = None
depends_on = None

_STATUS = "ontreconcileadmissionstatus"
_STATUS_VALUES = ("active", "expired", "revoked")
_SCOPE = "ontreconcilescope"
_SCOPE_VALUES = ("automatic_sweep",)


def _enum(name: str, values: tuple[str, ...], *, create: bool, postgres: bool):
    if not postgres:
        return sa.Enum(*values, name=name)
    if create:
        op.execute(
            sa.text(
                "DO $$ BEGIN "
                f"CREATE TYPE {name} AS ENUM ({', '.join(repr(v) for v in values)}); "
                "EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
            )
        )
    return postgresql.ENUM(*values, name=name, create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name.startswith("postgres")
    scope_type = _enum(_SCOPE, _SCOPE_VALUES, create=False, postgres=is_postgres)
    status_type = _enum(_STATUS, _STATUS_VALUES, create=True, postgres=is_postgres)

    op.create_table(
        "ont_reconcile_admissions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "ont_unit_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ont_units.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "scope", scope_type, nullable=False, server_default="automatic_sweep"
        ),
        sa.Column("status", status_type, nullable=False, server_default="active"),
        sa.Column("cohort_key", sa.String(length=120), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("actor", sa.String(length=160), nullable=False),
        sa.Column("reviewer", sa.String(length=160), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column(
            "admitted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("ended_by", sa.String(length=160)),
        sa.Column("end_reason", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "expires_at > admitted_at",
            name="ck_ont_reconcile_admissions_expiry_after_admission",
        ),
    )
    op.create_index(
        "ix_ont_reconcile_admissions_ont_unit_id",
        "ont_reconcile_admissions",
        ["ont_unit_id"],
    )
    op.create_index(
        "ix_ont_reconcile_admissions_status",
        "ont_reconcile_admissions",
        ["status"],
    )
    op.create_index(
        "ix_ont_reconcile_admissions_expires_at",
        "ont_reconcile_admissions",
        ["expires_at"],
    )
    op.create_index(
        "ix_ont_reconcile_admissions_cohort_key",
        "ont_reconcile_admissions",
        ["cohort_key"],
    )
    op.create_unique_constraint(
        "uq_ont_reconcile_admissions_idempotency_key",
        "ont_reconcile_admissions",
        ["idempotency_key"],
    )
    op.create_index(
        "uq_ont_reconcile_admissions_active_per_ont_scope",
        "ont_reconcile_admissions",
        ["ont_unit_id", "scope"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
        sqlite_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_ont_reconcile_admissions_active_per_ont_scope",
        table_name="ont_reconcile_admissions",
    )
    op.drop_constraint(
        "uq_ont_reconcile_admissions_idempotency_key",
        "ont_reconcile_admissions",
        type_="unique",
    )
    for name in (
        "ix_ont_reconcile_admissions_cohort_key",
        "ix_ont_reconcile_admissions_expires_at",
        "ix_ont_reconcile_admissions_status",
        "ix_ont_reconcile_admissions_ont_unit_id",
    ):
        op.drop_index(name, table_name="ont_reconcile_admissions")
    op.drop_table("ont_reconcile_admissions")
    if op.get_bind().dialect.name.startswith("postgres"):
        op.execute(sa.text(f"DROP TYPE IF EXISTS {_STATUS}"))
