"""Make subscription lifecycle transitions immutable evidence.

Prerequisite for SLA period scoring (OUTAGE_SLA_SPINE §4). Eligibility is
"active lifecycle ∩ proven service entitlement", so the lifecycle side has to
be evidence a contractual score can rest on. Today it is not:
``SubscriptionLifecycleEvents.update`` set any attribute from a partial
payload — including ``to_status`` and ``created_at`` — and ``.delete`` removed
rows outright. A customer's entitlement history could therefore be rewritten
after a period had been scored against it, which is the same defect migration
467 closed for policy terms, one layer down.

Two changes:

1. ``evidence_grade`` records what each row can be trusted for. Every existing
   row is graded ``unsupported_pre_cutover``: it was mutable for its whole
   life, so it cannot be vouched for retrospectively, and the honest response
   is to label it rather than to assume it. Rows written from here are
   ``transition_evidence``. Scoring reports periods resting on unsupported
   history as incomplete rather than silently scoring them.

2. A trigger rejects UPDATE and DELETE outright. The service-layer methods are
   removed in the same change, but a service can be re-added and a migration
   cannot be argued with; the guarantee belongs where the data is. INSERT is
   untouched — this is append-only, not read-only.

Deliberately NOT done: reconstructing history from current status,
``created_at``, ``updated_at`` or billing anchors. Those produce a plausible
timeline with no evidentiary basis, which is worse than an admitted gap
because it looks authoritative.

Expand-only: one nullable-then-backfilled column plus a trigger. Downgrade
drops both.

Revision ID: 468_immutable_lifecycle_transition_evidence
Revises: 467_sla_policy_versions
Create Date: 2026-08-04
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "468_immutable_lifecycle_transition_evidence"
down_revision = "467_sla_policy_versions"
branch_labels = None
depends_on = None

_GUARD_FUNCTION = "subscription_lifecycle_events_append_only"


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    op.add_column(
        "subscription_lifecycle_events",
        sa.Column("evidence_grade", sa.String(length=32), nullable=True),
    )
    # Grade the existing corpus before the trigger exists, since the trigger
    # would (correctly) reject this very UPDATE.
    op.execute(
        "UPDATE subscription_lifecycle_events "
        "SET evidence_grade = 'unsupported_pre_cutover' "
        "WHERE evidence_grade IS NULL"
    )
    op.alter_column(
        "subscription_lifecycle_events",
        "evidence_grade",
        existing_type=sa.String(length=32),
        nullable=False,
        server_default="transition_evidence",
    )
    op.create_check_constraint(
        "ck_subscription_lifecycle_events_evidence_grade",
        "subscription_lifecycle_events",
        "evidence_grade IN ('transition_evidence', 'unsupported_pre_cutover')",
    )
    op.create_index(
        "ix_subscription_lifecycle_events_subscription_time",
        "subscription_lifecycle_events",
        ["subscription_id", "created_at"],
    )

    if is_postgres:
        op.execute(
            f"""
            CREATE OR REPLACE FUNCTION {_GUARD_FUNCTION}()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION
                    'subscription_lifecycle_events is append-only: % rejected',
                    TG_OP
                    USING ERRCODE = 'restrict_violation';
            END;
            $$ LANGUAGE plpgsql;
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER trg_{_GUARD_FUNCTION}
            BEFORE UPDATE OR DELETE ON subscription_lifecycle_events
            FOR EACH ROW EXECUTE FUNCTION {_GUARD_FUNCTION}();
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            f"DROP TRIGGER IF EXISTS trg_{_GUARD_FUNCTION} "
            "ON subscription_lifecycle_events"
        )
        op.execute(f"DROP FUNCTION IF EXISTS {_GUARD_FUNCTION}()")
    op.drop_index(
        "ix_subscription_lifecycle_events_subscription_time",
        table_name="subscription_lifecycle_events",
    )
    op.drop_constraint(
        "ck_subscription_lifecycle_events_evidence_grade",
        "subscription_lifecycle_events",
        type_="check",
    )
    op.drop_column("subscription_lifecycle_events", "evidence_grade")
