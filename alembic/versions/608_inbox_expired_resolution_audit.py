"""Add explicit expired-conversation resolution audit evidence.

Revision ID: 608_inbox_expired_resolution_audit
Revises: 607_team_inbox_provider_identity_scope
Create Date: 2026-09-14
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "608_inbox_expired_resolution_audit"
down_revision = "607_team_inbox_provider_identity_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "inbox_status_transition_events",
        sa.Column("resolution_reason", sa.String(length=80), nullable=True),
    )
    op.add_column(
        "inbox_status_transition_events",
        sa.Column("channel_state_at_resolution", sa.String(length=40), nullable=True),
    )
    op.create_check_constraint(
        "ck_inbox_status_event_resolution_reason",
        "inbox_status_transition_events",
        "resolution_reason IS NULL OR resolution_reason IN ("
        "'customer_stopped_responding', 'whatsapp_window_expired', "
        "'issue_completed_before_expiry', 'duplicate_conversation', "
        "'no_further_action_required', 'spam_irrelevant', 'other')",
    )
    op.create_check_constraint(
        "ck_inbox_status_event_resolution_channel_state",
        "inbox_status_transition_events",
        "channel_state_at_resolution IS NULL OR channel_state_at_resolution IN ("
        "'active_window', 'expired', 'unavailable', 'not_applicable')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_inbox_status_event_resolution_channel_state",
        "inbox_status_transition_events",
        type_="check",
    )
    op.drop_constraint(
        "ck_inbox_status_event_resolution_reason",
        "inbox_status_transition_events",
        type_="check",
    )
    op.drop_column("inbox_status_transition_events", "channel_state_at_resolution")
    op.drop_column("inbox_status_transition_events", "resolution_reason")
