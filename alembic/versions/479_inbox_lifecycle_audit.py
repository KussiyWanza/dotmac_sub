"""Add prospective Team Inbox lifecycle audit evidence.

Revision ID: 479_inbox_lifecycle_audit
Revises: 478_quote_deposit_structural_links
Create Date: 2026-08-05

This expand migration deliberately does not infer historical events. Native
writes become complete after cutover; reviewed historical reconstruction is a
separate manifest-bound owner command.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "479_inbox_lifecycle_audit"
down_revision: str | None = "478_quote_deposit_structural_links"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _event_common(
    table: str, *, subject_column: sa.Column, source_constraint: str
) -> None:
    op.create_table(
        table,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        subject_column,
        sa.Column("previous_status", sa.String(40)),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("actor_person_id", postgresql.UUID(as_uuid=True)),
        sa.Column("reason_code", sa.String(80), nullable=False),
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("source_id", sa.String(160), nullable=False),
        sa.Column("evidence_grade", sa.String(40), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source", "source_id", name=source_constraint),
    )


def _append_only(table: str) -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    function = f"{table}_append_only"
    op.execute(
        f"""
        CREATE FUNCTION {function}() RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION '{table} is append-only';
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER trg_{function}
        BEFORE UPDATE OR DELETE ON {table}
        FOR EACH ROW EXECUTE FUNCTION {function}();
        """
    )


def upgrade() -> None:
    op.create_table(
        "inbox_routing_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("inbox_conversations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("previous_service_team_id", postgresql.UUID(as_uuid=True)),
        sa.Column("service_team_id", postgresql.UUID(as_uuid=True)),
        sa.Column("previous_person_id", postgresql.UUID(as_uuid=True)),
        sa.Column("person_id", postgresql.UUID(as_uuid=True)),
        sa.Column("actor_person_id", postgresql.UUID(as_uuid=True)),
        sa.Column("decision_mode", sa.String(40), nullable=False),
        sa.Column("presence_status", sa.String(40)),
        sa.Column("presence_observed_at", sa.DateTime(timezone=True)),
        sa.Column("active_conversation_count", sa.Integer()),
        sa.Column("max_concurrent_conversations", sa.Integer()),
        sa.Column("reason_code", sa.String(80), nullable=False),
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("source_id", sa.String(160), nullable=False),
        sa.Column("evidence_grade", sa.String(40), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "source", "source_id", name="uq_inbox_routing_event_source"
        ),
        sa.CheckConstraint(
            "event_type IN ('assigned', 'reassigned', 'queued', 'unassigned', "
            "'escalated', 'auto_assignment_declined')",
            name="ck_inbox_routing_event_type",
        ),
        sa.CheckConstraint(
            "source IN ('routing_command', 'historical_backfill')",
            name="ck_inbox_routing_event_source",
        ),
        sa.CheckConstraint(
            "evidence_grade IN ('native', 'authoritative_historical', "
            "'strongly_inferred', 'weakly_inferred', 'unknown')",
            name="ck_inbox_routing_event_evidence_grade",
        ),
        sa.CheckConstraint(
            "decision_mode IN ('manual', 'automatic', 'system')",
            name="ck_inbox_routing_event_decision_mode",
        ),
    )
    op.create_index(
        "ix_inbox_routing_event_conversation_time",
        "inbox_routing_events",
        ["conversation_id", "occurred_at"],
    )
    _event_common(
        "inbox_status_transition_events",
        subject_column=sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("inbox_conversations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        source_constraint="uq_inbox_status_event_source",
    )
    op.create_check_constraint(
        "ck_inbox_status_event_status",
        "inbox_status_transition_events",
        "status IN ('open', 'pending', 'snoozed', 'resolved')",
    )
    op.create_check_constraint(
        "ck_inbox_status_event_source_kind",
        "inbox_status_transition_events",
        "source IN ('status_command', 'historical_backfill')",
    )
    op.create_check_constraint(
        "ck_inbox_status_event_evidence_grade",
        "inbox_status_transition_events",
        "evidence_grade IN ('native', 'authoritative_historical', "
        "'strongly_inferred', 'weakly_inferred', 'unknown')",
    )
    op.create_index(
        "ix_inbox_status_event_conversation_time",
        "inbox_status_transition_events",
        ["conversation_id", "occurred_at"],
    )
    _event_common(
        "inbox_agent_presence_events",
        subject_column=sa.Column(
            "person_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        source_constraint="uq_inbox_presence_event_source",
    )
    op.create_check_constraint(
        "ck_inbox_presence_event_status",
        "inbox_agent_presence_events",
        "status IN ('online', 'away', 'on_break', 'offline')",
    )
    op.create_check_constraint(
        "ck_inbox_presence_event_source_kind",
        "inbox_agent_presence_events",
        "source IN ('presence_command', 'historical_backfill')",
    )
    op.create_check_constraint(
        "ck_inbox_presence_event_evidence_grade",
        "inbox_agent_presence_events",
        "evidence_grade IN ('native', 'authoritative_historical', "
        "'strongly_inferred', 'weakly_inferred', 'unknown')",
    )
    op.create_index(
        "ix_inbox_presence_event_person_time",
        "inbox_agent_presence_events",
        ["person_id", "occurred_at"],
    )
    op.add_column(
        "inbox_conversation_assignments",
        sa.Column("ended_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "inbox_conversation_assignments",
        sa.Column("ended_by_event_id", postgresql.UUID(as_uuid=True)),
    )
    op.create_foreign_key(
        "fk_inbox_assignment_ended_by_event",
        "inbox_conversation_assignments",
        "inbox_routing_events",
        ["ended_by_event_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "ALTER TABLE inbox_conversation_assignments ADD CONSTRAINT "
            "ck_inbox_assignment_interval_evidence CHECK ("
            "(is_active IS TRUE AND ended_at IS NULL AND ended_by_event_id IS NULL) OR "
            "(is_active IS FALSE AND ended_at IS NOT NULL AND ended_by_event_id IS NOT NULL)"
            ") NOT VALID"
        )
    for table in (
        "inbox_routing_events",
        "inbox_status_transition_events",
        "inbox_agent_presence_events",
    ):
        _append_only(table)
    op.create_table(
        "inbox_audit_reconstruction_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("manifest_sha256", sa.String(64), nullable=False),
        sa.Column("source_watermark", sa.String(240), nullable=False),
        sa.Column("approval_reference", sa.String(160), nullable=False),
        sa.Column("actor_person_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("applied_count", sa.Integer(), nullable=False),
        sa.Column("exception_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_inbox_audit_reconstruction_key"
        ),
    )
    _append_only("inbox_audit_reconstruction_runs")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(
        sa.text("SELECT count(*) FROM inbox_audit_reconstruction_runs")
    ).scalar_one():
        raise RuntimeError(
            "Inbox audit reconstruction receipts exist; use a forward fix"
        )
    for table in (
        "inbox_routing_events",
        "inbox_status_transition_events",
        "inbox_agent_presence_events",
    ):
        if bind.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one():
            raise RuntimeError(
                "Inbox lifecycle evidence exists; use a reviewed forward fix"
            )
    op.drop_constraint(
        "fk_inbox_assignment_ended_by_event",
        "inbox_conversation_assignments",
        type_="foreignkey",
    )
    if bind.dialect.name == "postgresql":
        op.drop_constraint(
            "ck_inbox_assignment_interval_evidence",
            "inbox_conversation_assignments",
            type_="check",
        )
    op.drop_column("inbox_conversation_assignments", "ended_by_event_id")
    op.drop_column("inbox_conversation_assignments", "ended_at")
    if bind.dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_inbox_audit_reconstruction_runs_append_only "
            "ON inbox_audit_reconstruction_runs"
        )
        op.execute(
            "DROP FUNCTION IF EXISTS inbox_audit_reconstruction_runs_append_only()"
        )
    op.drop_table("inbox_audit_reconstruction_runs")
    for table in (
        "inbox_agent_presence_events",
        "inbox_status_transition_events",
        "inbox_routing_events",
    ):
        if bind.dialect.name == "postgresql":
            function = f"{table}_append_only"
            op.execute(f"DROP TRIGGER IF EXISTS trg_{function} ON {table}")
            op.execute(f"DROP FUNCTION IF EXISTS {function}()")
        op.drop_table(table)
