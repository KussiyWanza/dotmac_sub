"""Add durable per-entity timers and reason-scoped collections cases.

ADR 0007 Phase 5 (expand). Tables only. The dunning_runner and
prepaid_balance_sweep schedules keep running unchanged; timers and cases are
shadow evidence until the Phase 5 parity gate passes.

Revision ID: 433_durable_timers_collections_cases
Revises: 432_owner_output_receipts
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "433_durable_timers_collections_cases"
down_revision = "432_owner_output_receipts"
branch_labels = None
depends_on = None

_AUTHORITY = sa.Enum("shadow", "authoritative", name="billingrecordauthority")
_TIMER_STATUS = sa.Enum(
    "scheduled", "fired", "canceled", "superseded", name="timerstatus"
)
_REASON = sa.Enum("postpaid_overdue", "prepaid_underfunded", name="collectionsreason")
_CASE_STATE = sa.Enum(
    "open",
    "warned",
    "escalated",
    "consequence_requested",
    "closed",
    name="collectionscasestate",
)


def upgrade() -> None:
    bind = op.get_bind()
    _TIMER_STATUS.create(bind, checkfirst=True)
    _REASON.create(bind, checkfirst=True)
    _CASE_STATE.create(bind, checkfirst=True)

    op.create_table(
        "durable_timers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("owner", sa.String(120), nullable=False),
        sa.Column("entity_kind", sa.String(80), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("purpose", sa.String(80), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "expected_source_version",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
        sa.Column("output_event_type", sa.String(100), nullable=False),
        sa.Column("status", _TIMER_STATUS, nullable=False),
        sa.Column("fired_at", sa.DateTime(timezone=True)),
        sa.Column("fired_event_id", postgresql.UUID(as_uuid=True)),
        sa.Column("command_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "owner",
            "entity_kind",
            "entity_id",
            "purpose",
            "generation",
            name="uq_durable_timer_generation",
        ),
        sa.CheckConstraint("generation >= 1", name="ck_durable_timer_generation"),
    )
    op.create_index(
        "uq_durable_timer_current",
        "durable_timers",
        ["owner", "entity_kind", "entity_id", "purpose"],
        unique=True,
        postgresql_where=sa.text("status = 'scheduled'"),
        sqlite_where=sa.text("status = 'scheduled'"),
    )
    op.create_index("ix_durable_timer_due", "durable_timers", ["status", "due_at"])

    op.create_table(
        "collections_cases",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subscription_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reason", _REASON, nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("authority", _AUTHORITY, nullable=False, server_default="shadow"),
        sa.Column("state", _CASE_STATE, nullable=False),
        sa.Column("source_kind", sa.String(80), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("warned_at", sa.DateTime(timezone=True)),
        sa.Column("escalated_at", sa.DateTime(timezone=True)),
        sa.Column("consequence_requested_at", sa.DateTime(timezone=True)),
        sa.Column("consequence_idempotency_key", sa.String(200)),
        sa.Column("consequence_event_id", postgresql.UUID(as_uuid=True)),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("close_reason", sa.Text()),
        sa.Column("command_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "consequence_idempotency_key",
            name="uq_collections_case_consequence_key",
        ),
    )
    op.create_index(
        "uq_collections_case_live",
        "collections_cases",
        ["account_id", "subscription_id", "reason"],
        unique=True,
        postgresql_where=sa.text("state != 'closed'"),
        sqlite_where=sa.text("state != 'closed'"),
    )
    op.create_index(
        "ix_collections_case_account", "collections_cases", ["account_id", "state"]
    )


def downgrade() -> None:
    op.drop_table("collections_cases")
    op.drop_table("durable_timers")
    bind = op.get_bind()
    _CASE_STATE.drop(bind, checkfirst=True)
    _REASON.drop(bind, checkfirst=True)
    _TIMER_STATUS.drop(bind, checkfirst=True)
