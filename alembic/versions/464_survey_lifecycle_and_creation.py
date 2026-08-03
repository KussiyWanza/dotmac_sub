"""Add the native Survey lifecycle and creation contract.

Existing Surveys are preserved as drafts for public-answer safety.
The additive columns allow the owner service to cut over creation, lifecycle,
invitation, trigger, and response writes without inferring authority from the
legacy ``is_active`` flag.

Revision ID: 464_survey_lifecycle_and_creation
Revises: 463_outage_customer_notices
Create Date: 2026-08-02
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "464_survey_lifecycle_and_creation"
down_revision = "463_outage_customer_notices"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "surveys",
        sa.Column(
            "trigger_type",
            sa.String(length=32),
            nullable=False,
            server_default="manual",
        ),
    )
    op.add_column("surveys", sa.Column("public_slug", sa.String(length=120)))
    op.add_column("surveys", sa.Column("thank_you_message", sa.Text()))
    op.add_column(
        "surveys",
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="draft",
        ),
    )
    op.add_column(
        "surveys",
        sa.Column(
            "created_by_id",
            sa.Uuid(),
            sa.ForeignKey("parties.id", ondelete="RESTRICT"),
        ),
    )
    op.add_column("surveys", sa.Column("expires_at", sa.DateTime(timezone=True)))
    op.add_column("surveys", sa.Column("segment_filter", sa.JSON()))
    op.add_column(
        "surveys",
        sa.Column("total_invited", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "surveys",
        sa.Column("total_responses", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("surveys", sa.Column("avg_rating", sa.Numeric(6, 2)))
    op.add_column("surveys", sa.Column("nps_score", sa.Numeric(6, 2)))
    op.add_column(
        "surveys", sa.Column("creation_idempotency_key", sa.String(length=80))
    )
    op.add_column("surveys", sa.Column("creation_fingerprint", sa.String(length=64)))
    op.execute(sa.text("UPDATE surveys SET questions = '[]' WHERE questions IS NULL"))
    op.alter_column(
        "surveys",
        "questions",
        existing_type=sa.JSON(),
        nullable=False,
        server_default=sa.text("'[]'"),
    )
    op.create_check_constraint(
        "ck_surveys_trigger_type",
        "surveys",
        "trigger_type IN ('manual', 'ticket_closed', 'work_order_completed')",
    )
    op.create_check_constraint(
        "ck_surveys_status",
        "surveys",
        "status IN ('draft', 'active', 'paused', 'closed')",
    )
    op.create_unique_constraint("uq_surveys_public_slug", "surveys", ["public_slug"])
    op.create_unique_constraint(
        "uq_surveys_creation_idempotency_key",
        "surveys",
        ["creation_idempotency_key"],
    )

    op.create_table(
        "survey_invitations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "survey_id",
            sa.Uuid(),
            sa.ForeignKey("surveys.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "subscriber_id",
            sa.Uuid(),
            sa.ForeignKey("subscribers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("token", sa.String(length=80), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_event_id", sa.Uuid(), nullable=False),
        sa.Column("source_entity_id", sa.Uuid()),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "length(trim(token)) > 0",
            name="ck_survey_invitations_token_not_blank",
        ),
        sa.CheckConstraint(
            "source_type IN ('manual', 'ticket_closed', 'work_order_completed')",
            name="ck_survey_invitations_source_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'completed', 'expired')",
            name="ck_survey_invitations_status",
        ),
        sa.UniqueConstraint("token", name="uq_survey_invitations_token"),
        sa.UniqueConstraint(
            "survey_id",
            "subscriber_id",
            "source_event_id",
            name="uq_survey_invitations_event_recipient",
        ),
    )
    op.create_index(
        "ix_survey_invitations_survey_id", "survey_invitations", ["survey_id"]
    )
    op.create_index(
        "ix_survey_invitations_subscriber_id",
        "survey_invitations",
        ["subscriber_id"],
    )

    op.add_column(
        "survey_responses", sa.Column("invitation_id", sa.Uuid(), nullable=True)
    )
    op.add_column(
        "survey_responses", sa.Column("nps_value", sa.Integer(), nullable=True)
    )
    op.create_foreign_key(
        "fk_survey_responses_survey_id_surveys",
        "survey_responses",
        "surveys",
        ["survey_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_survey_responses_invitation_id_survey_invitations",
        "survey_responses",
        "survey_invitations",
        ["invitation_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_survey_responses_invitation_id",
        "survey_responses",
        ["invitation_id"],
    )
    op.create_check_constraint(
        "ck_survey_responses_nps_value",
        "survey_responses",
        "nps_value IS NULL OR (nps_value >= 0 AND nps_value <= 10)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_survey_responses_nps_value", "survey_responses", type_="check"
    )
    op.drop_constraint(
        "uq_survey_responses_invitation_id", "survey_responses", type_="unique"
    )
    op.drop_constraint(
        "fk_survey_responses_invitation_id_survey_invitations",
        "survey_responses",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_survey_responses_survey_id_surveys",
        "survey_responses",
        type_="foreignkey",
    )
    op.drop_column("survey_responses", "nps_value")
    op.drop_column("survey_responses", "invitation_id")
    op.drop_index(
        "ix_survey_invitations_subscriber_id", table_name="survey_invitations"
    )
    op.drop_index("ix_survey_invitations_survey_id", table_name="survey_invitations")
    op.drop_table("survey_invitations")

    op.drop_constraint("uq_surveys_creation_idempotency_key", "surveys", type_="unique")
    op.drop_constraint("uq_surveys_public_slug", "surveys", type_="unique")
    op.drop_constraint("ck_surveys_status", "surveys", type_="check")
    op.drop_constraint("ck_surveys_trigger_type", "surveys", type_="check")
    op.alter_column(
        "surveys",
        "questions",
        existing_type=sa.JSON(),
        nullable=True,
        server_default=None,
    )
    for column in (
        "creation_fingerprint",
        "creation_idempotency_key",
        "nps_score",
        "avg_rating",
        "total_responses",
        "total_invited",
        "segment_filter",
        "expires_at",
        "created_by_id",
        "status",
        "thank_you_message",
        "public_slug",
        "trigger_type",
    ):
        op.drop_column("surveys", column)
