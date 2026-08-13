"""Add assignment-scoped ONT service-configuration lifecycle identity.

Revision ID: 522_ont_service_configuration_lifecycle
Revises: 521_backfill_nas_radius_pool_links
Create Date: 2026-08-12

This migration is intentionally additive. Legacy reconciler errors and
provisioning events remain unbound; assigning them to a configuration lifecycle
by timestamp would manufacture authority. Reviewed repair is an application
command, not an Alembic data rewrite.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "522_ont_service_configuration_lifecycle"
down_revision: str | None = "521_backfill_nas_radius_pool_links"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PHASES = (
    "saved",
    "queued",
    "applying",
    "readback_pending",
    "verified",
    "failed",
    "superseded",
    "retired",
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # PostgreSQL enum values cannot be removed safely in downgrade. Keeping
        # an unused value is preferable to rebuilding a live operation table.
        op.execute(
            "ALTER TYPE networkoperationtype "
            "ADD VALUE IF NOT EXISTS 'ont_service_config'"
        )

    op.create_table(
        "ont_service_configuration_heads",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "ont_unit_id",
            sa.Uuid(),
            sa.ForeignKey("ont_units.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "assignment_id",
            sa.Uuid(),
            sa.ForeignKey("ont_assignments.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("current_revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "latest_operation_id",
            sa.Uuid(),
            sa.ForeignKey("network_operations.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "phase", sa.String(length=40), nullable=False, server_default="saved"
        ),
        sa.Column("waiting_reason", sa.String(length=160)),
        sa.Column("failure_code", sa.String(length=160)),
        sa.Column("failure_message", sa.Text()),
        sa.Column("last_retry_idempotency_key", sa.String(length=160)),
        sa.Column(
            "last_retry_operation_id",
            sa.Uuid(),
            sa.ForeignKey("network_operations.id", ondelete="SET NULL"),
        ),
        sa.Column("retired_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            f"phase IN ({', '.join(repr(value) for value in _PHASES)})",
            name="ck_ont_service_config_head_phase",
        ),
        sa.UniqueConstraint(
            "assignment_id", name="uq_ont_service_config_head_assignment"
        ),
    )
    op.create_index(
        "ix_ont_service_config_head_ont",
        "ont_service_configuration_heads",
        ["ont_unit_id"],
    )
    op.create_index(
        "ix_ont_service_config_head_latest_operation",
        "ont_service_configuration_heads",
        ["latest_operation_id"],
    )

    op.create_table(
        "ont_service_configuration_revisions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "head_id",
            sa.Uuid(),
            sa.ForeignKey("ont_service_configuration_heads.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "assignment_id",
            sa.Uuid(),
            sa.ForeignKey("ont_assignments.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("section", sa.String(length=40), nullable=False),
        sa.Column("command_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column(
            "desired_change_evidence",
            postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite"),
            nullable=False,
        ),
        sa.Column(
            "operation_id",
            sa.Uuid(),
            sa.ForeignKey("network_operations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "phase", sa.String(length=40), nullable=False, server_default="saved"
        ),
        sa.Column("waiting_reason", sa.String(length=160)),
        sa.Column("failure_code", sa.String(length=160)),
        sa.Column("failure_message", sa.Text()),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            f"phase IN ({', '.join(repr(value) for value in _PHASES)})",
            name="ck_ont_service_config_revision_phase",
        ),
        sa.UniqueConstraint(
            "head_id", "revision", name="uq_ont_service_config_revision"
        ),
        sa.UniqueConstraint(
            "head_id", "idempotency_key", name="uq_ont_service_config_idempotency"
        ),
        sa.UniqueConstraint("operation_id", name="uq_ont_service_config_operation"),
    )
    op.create_index(
        "ix_ont_service_config_revision_assignment",
        "ont_service_configuration_revisions",
        ["assignment_id"],
    )
    op.create_index(
        "ix_ont_service_config_revision_phase",
        "ont_service_configuration_revisions",
        ["phase"],
    )

    op.add_column("ont_units", sa.Column("reconcile_configuration_head_id", sa.Uuid()))
    op.add_column("ont_units", sa.Column("reconcile_assignment_id", sa.Uuid()))
    op.add_column("ont_units", sa.Column("reconcile_desired_revision", sa.Integer()))
    op.add_column("ont_units", sa.Column("reconcile_operation_id", sa.Uuid()))
    op.create_foreign_key(
        "fk_ont_reconcile_configuration_revision",
        "ont_units",
        "ont_service_configuration_revisions",
        ["reconcile_configuration_head_id", "reconcile_desired_revision"],
        ["head_id", "revision"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_ont_units_reconcile_assignment",
        "ont_units",
        "ont_assignments",
        ["reconcile_assignment_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_ont_units_reconcile_operation",
        "ont_units",
        "network_operations",
        ["reconcile_operation_id"],
        ["id"],
        ondelete="SET NULL",
    )

    for name, column_type in (
        ("assignment_id", sa.Uuid()),
        ("configuration_head_id", sa.Uuid()),
        ("configuration_revision", sa.Integer()),
        ("operation_id", sa.Uuid()),
    ):
        op.add_column("ont_provisioning_events", sa.Column(name, column_type))
    op.create_foreign_key(
        "fk_ont_provisioning_event_assignment",
        "ont_provisioning_events",
        "ont_assignments",
        ["assignment_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_ont_provisioning_event_configuration_revision",
        "ont_provisioning_events",
        "ont_service_configuration_revisions",
        ["configuration_head_id", "configuration_revision"],
        ["head_id", "revision"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_ont_provisioning_event_operation",
        "ont_provisioning_events",
        "network_operations",
        ["operation_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_ont_provisioning_events_configuration",
        "ont_provisioning_events",
        ["configuration_head_id", "configuration_revision"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ont_provisioning_events_configuration",
        table_name="ont_provisioning_events",
    )
    op.drop_constraint(
        "fk_ont_provisioning_event_operation",
        "ont_provisioning_events",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_ont_provisioning_event_configuration_revision",
        "ont_provisioning_events",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_ont_provisioning_event_assignment",
        "ont_provisioning_events",
        type_="foreignkey",
    )
    for name in (
        "operation_id",
        "configuration_revision",
        "configuration_head_id",
        "assignment_id",
    ):
        op.drop_column("ont_provisioning_events", name)

    op.drop_constraint(
        "fk_ont_units_reconcile_operation", "ont_units", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_ont_units_reconcile_assignment", "ont_units", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_ont_reconcile_configuration_revision", "ont_units", type_="foreignkey"
    )
    for name in (
        "reconcile_operation_id",
        "reconcile_desired_revision",
        "reconcile_assignment_id",
        "reconcile_configuration_head_id",
    ):
        op.drop_column("ont_units", name)

    op.drop_table("ont_service_configuration_revisions")
    op.drop_table("ont_service_configuration_heads")
