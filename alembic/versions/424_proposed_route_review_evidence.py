"""Add append-only evidence for staff proposed-route review decisions.

Revision ID: 424_proposed_route_review_evidence
Revises: 423_prepaid_opening_funding_reconciliation
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "424_proposed_route_review_evidence"
down_revision = "423_prepaid_opening_funding_reconciliation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "proposed_route_revision_review_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("quote_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("vendor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("from_status", sa.String(length=40), nullable=False),
        sa.Column("to_status", sa.String(length=40), nullable=False),
        sa.Column("actor_type", sa.String(length=40), nullable=False),
        sa.Column("actor_id", sa.String(length=120), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "from_status <> to_status",
            name="ck_proposed_route_review_event_status_change",
        ),
        sa.ForeignKeyConstraint(
            ["revision_id"], ["proposed_route_revisions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["quote_id"], ["project_quotes.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["installation_projects.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["vendor_id"], ["vendors.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_proposed_route_review_event_event_id",
        "proposed_route_revision_review_events",
        ["event_id"],
        unique=True,
    )
    for name, column in (
        ("ix_proposed_route_review_event_event_type", "event_type"),
        ("ix_proposed_route_review_event_actor_id", "actor_id"),
        ("ix_proposed_route_review_event_quote_id", "quote_id"),
        ("ix_proposed_route_review_event_project_id", "project_id"),
        ("ix_proposed_route_review_event_vendor_id", "vendor_id"),
    ):
        op.create_index(
            name,
            "proposed_route_revision_review_events",
            [column],
        )
    op.create_index(
        "ix_proposed_route_review_event_revision_occurred",
        "proposed_route_revision_review_events",
        ["revision_id", "occurred_at"],
    )

    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            CREATE FUNCTION reject_proposed_route_review_event_mutation()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                RAISE EXCEPTION
                    'proposed_route_revision_review_events is append-only'
                    USING ERRCODE = 'integrity_constraint_violation';
            END;
            $$
            """
        )
        op.execute(
            """
            CREATE TRIGGER proposed_route_revision_review_events_append_only
            BEFORE UPDATE OR DELETE ON proposed_route_revision_review_events
            FOR EACH ROW
            EXECUTE FUNCTION reject_proposed_route_review_event_mutation()
            """
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER proposed_route_revision_review_events_append_only "
            "ON proposed_route_revision_review_events"
        )
        op.execute("DROP FUNCTION reject_proposed_route_review_event_mutation()")

    op.drop_index(
        "ix_proposed_route_review_event_revision_occurred",
        table_name="proposed_route_revision_review_events",
    )
    for name in (
        "ix_proposed_route_review_event_vendor_id",
        "ix_proposed_route_review_event_project_id",
        "ix_proposed_route_review_event_quote_id",
        "ix_proposed_route_review_event_actor_id",
        "ix_proposed_route_review_event_event_type",
        "ix_proposed_route_review_event_event_id",
    ):
        op.drop_index(
            name,
            table_name="proposed_route_revision_review_events",
        )
    op.drop_table("proposed_route_revision_review_events")
