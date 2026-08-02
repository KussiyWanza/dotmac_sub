"""make ONT WAN service instances an owned, exact-service intent record

``OntWanServiceInstance`` modelled service intent but had no application writer:
the repository contained no constructor outside tests, and production held 8
rows against 1,523 ONTs. Rows written by nothing cannot authorise anything, so
before this the table recorded that something once wrote a value, not that
anyone declared intent.

This makes it an authoritative record:

* ``subscription_id`` — exact service grain. An ONT-grain row says "this device
  may terminate PPP", which is not "this SERVICE terminates here", and a
  delivery ruling built on the weaker claim can hand one service's credential
  to another.
* ``is_primary`` — which instance carries the service's primary Internet
  termination. ``priority`` may order instances; it never selects authority.
* ``lifecycle_state`` — planned/unverified → active → retired, the single
  authority. ``is_active`` becomes derived and is kept in step by the owner.
* ``revision`` — bound into a delivery ruling, so a ruling taken before a
  replace cannot authorise a write after it.
* provenance — actor, reason, evidence reference and transition timestamps.

EVERY EXISTING ROW STARTS NON-AUTHORISING. They land in ``unverified``
regardless of ``is_active``, because their provenance is unknown and adopting
them would repeat the mistake this slice exists to correct: a previous gate
authorised on 12 surviving ``OntAssignment.pppoe_username`` values that
migration 084 had already cleared.

NO UNIQUE CONSTRAINT YET. The one-active-primary-per-subscription and
per-ONT invariants are enforced by the owner commands now. The partial unique
indexes are added in a later migration, after inventory, backfill and
verification -- adding them here would either fail on unadjudicated data or
force this migration to pick a winner, which is the ownership decision it must
not make.

Revision ID: 456_ont_wan_service_intent_owner
Revises: 455_add_cpe_rf_signal_columns
Create Date: 2026-08-02
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "456_ont_wan_service_intent_owner"
down_revision = "455_add_cpe_rf_signal_columns"
branch_labels = None
depends_on = None

_LIFECYCLE = "ontwanservicelifecycle"
_LIFECYCLE_VALUES = ("planned", "unverified", "active", "retired")


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name.startswith("postgres")

    if is_postgres:
        # Checked creation: the column below uses the same named type, so an
        # unchecked create would emit a second CREATE TYPE on an incremental
        # upgrade path even where a fresh-model run looks clean.
        op.execute(
            sa.text(
                "DO $$ BEGIN "
                f"CREATE TYPE {_LIFECYCLE} AS ENUM "
                f"({', '.join(repr(v) for v in _LIFECYCLE_VALUES)}); "
                "EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
            )
        )
        lifecycle_type: sa.types.TypeEngine = postgresql.ENUM(
            *_LIFECYCLE_VALUES, name=_LIFECYCLE, create_type=False
        )
    else:
        lifecycle_type = sa.Enum(*_LIFECYCLE_VALUES, name=_LIFECYCLE)

    op.add_column(
        "ont_wan_service_instances",
        sa.Column("subscription_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "ont_wan_service_instances",
        sa.Column(
            "is_primary", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )
    op.add_column(
        "ont_wan_service_instances",
        sa.Column(
            "lifecycle_state",
            lifecycle_type,
            nullable=False,
            server_default="unverified",
        ),
    )
    op.add_column(
        "ont_wan_service_instances",
        sa.Column(
            "revision", sa.Integer(), nullable=False, server_default=sa.text("1")
        ),
    )
    op.add_column(
        "ont_wan_service_instances",
        sa.Column("declared_by", sa.String(length=160), nullable=True),
    )
    op.add_column(
        "ont_wan_service_instances",
        sa.Column("declared_reason", sa.Text(), nullable=True),
    )
    op.add_column(
        "ont_wan_service_instances",
        sa.Column("evidence_ref", sa.String(length=200), nullable=True),
    )
    op.add_column(
        "ont_wan_service_instances",
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "ont_wan_service_instances",
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "ont_wan_service_instances",
        sa.Column("retired_reason", sa.Text(), nullable=True),
    )
    op.add_column(
        "ont_wan_service_instances",
        sa.Column("replaced_by_id", postgresql.UUID(as_uuid=True), nullable=True),
    )

    op.create_foreign_key(
        "fk_ont_wan_service_instances_subscription",
        "ont_wan_service_instances",
        "subscriptions",
        ["subscription_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_ont_wan_service_instances_replaced_by",
        "ont_wan_service_instances",
        "ont_wan_service_instances",
        ["replaced_by_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_ont_wan_service_instances_subscription_id",
        "ont_wan_service_instances",
        ["subscription_id"],
    )
    op.create_index(
        "ix_ont_wan_service_instances_lifecycle_state",
        "ont_wan_service_instances",
        ["lifecycle_state"],
    )

    # Explicit and deliberate: no pre-existing row is adopted as intent.
    op.execute(
        sa.text(
            "UPDATE ont_wan_service_instances "
            "SET lifecycle_state = 'unverified', "
            "    declared_reason = COALESCE(declared_reason, "
            "      'pre-owner row; provenance unknown, non-authorising until "
            "adjudicated')"
        )
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ont_wan_service_instances_lifecycle_state",
        table_name="ont_wan_service_instances",
    )
    op.drop_index(
        "ix_ont_wan_service_instances_subscription_id",
        table_name="ont_wan_service_instances",
    )
    op.drop_constraint(
        "fk_ont_wan_service_instances_replaced_by",
        "ont_wan_service_instances",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_ont_wan_service_instances_subscription",
        "ont_wan_service_instances",
        type_="foreignkey",
    )
    for column in (
        "replaced_by_id",
        "retired_reason",
        "retired_at",
        "activated_at",
        "evidence_ref",
        "declared_reason",
        "declared_by",
        "revision",
        "lifecycle_state",
        "is_primary",
        "subscription_id",
    ):
        op.drop_column("ont_wan_service_instances", column)
    bind = op.get_bind()
    if bind.dialect.name.startswith("postgres"):
        op.execute(sa.text(f"DROP TYPE IF EXISTS {_LIFECYCLE}"))
