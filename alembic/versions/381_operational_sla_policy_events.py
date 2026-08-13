"""Make operational SLA event selection explicit and UI-owned.

Revision ID: 381_operational_sla_policy_events
Revises: 372_vendor_payment_projection
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "381_operational_sla_policy_events"
down_revision = "380_integration_platform_cutover"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "operational_escalation_policies",
        sa.Column("trigger", sa.String(length=120)),
    )
    op.execute(
        sa.text(
            "UPDATE operational_escalation_policies "
            "SET unresolved_after_seconds = cooldown_seconds "
            "WHERE unresolved_after_seconds IS NULL AND cooldown_seconds > 0"
        )
    )
    op.execute(
        sa.text(
            "UPDATE operational_escalation_policies SET cooldown_seconds = 0 "
            "WHERE cooldown_seconds <> 0"
        )
    )
    op.drop_index(
        "ix_operational_escalation_policies_scope",
        table_name="operational_escalation_policies",
    )
    op.create_index(
        "ix_operational_escalation_policies_scope",
        "operational_escalation_policies",
        ["entity_type", "trigger", "scope_type", "scope_id", "is_active"],
    )
    op.create_index(
        "uq_operational_escalation_policy_event_level_active",
        "operational_escalation_policies",
        ["entity_type", "trigger", "level"],
        unique=True,
        postgresql_where=sa.text("is_active IS TRUE AND trigger IS NOT NULL"),
    )
    op.execute(
        sa.text(
            """
            INSERT INTO domain_settings (
                id, domain, key, value_type, value_text, value_json,
                is_secret, is_active, created_at, updated_at
            ) VALUES (
                gen_random_uuid(), 'workflow', 'support_ticket_type_sla_policy',
                'json', NULL,
                CAST(:policy AS jsonb), false, true, now(), now()
            )
            -- No conflict TARGET. A target must match a unique index existing
            -- when this runs, and `(domain, key)` no longer does: migration 507
            -- replaced it with `uq_domain_settings_scope_domain_key`, and
            -- `001_squashed_initial_schema` builds the baseline from the
            -- CURRENT model metadata, so the model is this chain's history.
            -- Untargeted `DO NOTHING` needs no arbiter and means exactly what
            -- this wanted: skip the insert if any constraint would reject it.
            ON CONFLICT DO NOTHING
            """
        ).bindparams(
            policy=(
                '{"customer link disconnection":24,'
                '"multiple customer link disconnection":24,'
                '"customer realignment":24,'
                '"cabinet disconnection":24,'
                '"multiple cabinet link disconnection":24,'
                '"multiple cabinet disconnection":24,'
                '"cabinet migration":24,'
                '"core link disconnection":48,'
                '"multiple core link disconnection":48}'
            )
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM domain_settings "
            "WHERE domain = 'workflow' "
            "AND key = 'support_ticket_type_sla_policy'"
        )
    )
    op.drop_index(
        "uq_operational_escalation_policy_event_level_active",
        table_name="operational_escalation_policies",
    )
    op.execute(
        sa.text(
            "UPDATE operational_escalation_policies "
            "SET cooldown_seconds = COALESCE(unresolved_after_seconds, 0) "
            "WHERE cooldown_seconds = 0"
        )
    )
    op.drop_index(
        "ix_operational_escalation_policies_scope",
        table_name="operational_escalation_policies",
    )
    op.create_index(
        "ix_operational_escalation_policies_scope",
        "operational_escalation_policies",
        ["entity_type", "scope_type", "scope_id", "is_active"],
    )
    op.drop_column("operational_escalation_policies", "trigger")
