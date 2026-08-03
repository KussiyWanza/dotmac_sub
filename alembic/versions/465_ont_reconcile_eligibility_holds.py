"""per-ONT reconciliation holds owned by network.ont_reconcile_eligibility

The fleet-wide ``network.ont_reconcile`` control is the blunt instrument this
replaces. Disabling it stops convergence for every ONT, and because
``_close_expired_remote_access`` and ``_reconcile_dialer_credentials`` run
inside ``run_ont_reconcile_sweep`` AFTER the gate, it also silently pauses
expired remote-access cleanup and the dialer reconcile. A hold that needs to
cover five devices should not cost the other ~1,500 their convergence.

Design notes carried in the schema:

* ONE ACTIVE HOLD PER ONT AND SCOPE, as a PARTIAL unique index. Released holds
  remain as history and must not block a future hold on the same device.
* ``review_due_at`` IS NOT AN EXPIRY. Nothing in this schema releases a hold on
  a timer: an expiring hold would hand a suppressed device back to the sweeper
  at an arbitrary moment, which is the surprise a hold exists to prevent. Being
  overdue is a reporting state; only an explicit release command ends a hold.
* ``reviewer`` is separate from ``actor`` because suppressing convergence on a
  customer device is a two-person decision. The owner rejects self-review.
* ``idempotency_key`` is unique so a retried place-hold command cannot create a
  second row or trip the partial unique index.

Revision ID: 465_ont_reconcile_eligibility_holds
Revises: 464_survey_lifecycle_and_creation
Create Date: 2026-08-02
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "465_ont_reconcile_eligibility_holds"
down_revision = "464_survey_lifecycle_and_creation"
branch_labels = None
depends_on = None

_SCOPE = "ontreconcilescope"
_STATUS = "ontreconcileholdstatus"
_SCOPE_VALUES = ("automatic_sweep",)
_STATUS_VALUES = ("active", "released")


def _checked_enum(name: str, values: tuple[str, ...], is_postgres: bool):
    """Create the type only if absent.

    Unchecked creation emits a second CREATE TYPE on an incremental upgrade
    path even where a fresh-model run looks clean.
    """
    if not is_postgres:
        return sa.Enum(*values, name=name)
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

    scope_type = _checked_enum(_SCOPE, _SCOPE_VALUES, is_postgres)
    status_type = _checked_enum(_STATUS, _STATUS_VALUES, is_postgres)

    op.create_table(
        "ont_reconcile_holds",
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
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("actor", sa.String(length=160), nullable=False),
        sa.Column("reviewer", sa.String(length=160), nullable=False),
        # NOT NULL: the owner requires an idempotency key, and a nullable
        # column would let a direct writer bypass the contract.
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column(
            "placed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("review_due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_by", sa.String(length=160), nullable=True),
        sa.Column("release_reason", sa.Text(), nullable=True),
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
    )

    op.create_index(
        "ix_ont_reconcile_holds_ont_unit_id", "ont_reconcile_holds", ["ont_unit_id"]
    )
    op.create_index("ix_ont_reconcile_holds_status", "ont_reconcile_holds", ["status"])
    op.create_index(
        "ix_ont_reconcile_holds_review_due_at", "ont_reconcile_holds", ["review_due_at"]
    )
    op.create_unique_constraint(
        "uq_ont_reconcile_holds_idempotency_key",
        "ont_reconcile_holds",
        ["idempotency_key"],
    )

    # PARTIAL on BOTH dialects. Released rows are history and must not block a
    # new hold; a full unique constraint would turn that history into a
    # permanent lockout. sqlite_where is set too so the test database enforces
    # the same rule the production one does.
    op.create_index(
        "uq_ont_reconcile_holds_active_per_ont_scope",
        "ont_reconcile_holds",
        ["ont_unit_id", "scope"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
        sqlite_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_ont_reconcile_holds_active_per_ont_scope",
        table_name="ont_reconcile_holds",
    )
    op.drop_constraint(
        "uq_ont_reconcile_holds_idempotency_key",
        "ont_reconcile_holds",
        type_="unique",
    )
    for name in (
        "ix_ont_reconcile_holds_review_due_at",
        "ix_ont_reconcile_holds_status",
        "ix_ont_reconcile_holds_ont_unit_id",
    ):
        op.drop_index(name, table_name="ont_reconcile_holds")
    op.drop_table("ont_reconcile_holds")
    bind = op.get_bind()
    if bind.dialect.name.startswith("postgres"):
        op.execute(sa.text(f"DROP TYPE IF EXISTS {_STATUS}"))
        op.execute(sa.text(f"DROP TYPE IF EXISTS {_SCOPE}"))
