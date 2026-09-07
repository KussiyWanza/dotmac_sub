"""Allow the terminal resolved AI intake session state.

Revision ID: 582_ai_intake_resolved_state
Revises: 581_inbox_delivery_status_index
Create Date: 2026-09-07
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "582_ai_intake_resolved_state"
down_revision: str | None = "581_inbox_delivery_status_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_ai_intake_sessions_state"
_STATES_WITH_RESOLVED = (
    "state IN ('eligible', 'welcome_pending', 'collecting_intent', "
    "'awaiting_customer', 'classified', 'handoff_requested', 'completed', "
    "'resolved', 'stopped_human_takeover', 'fallback_escalated', 'expired', "
    "'failed', 'ineligible')"
)
_PREVIOUS_STATES = (
    "state IN ('eligible', 'welcome_pending', 'collecting_intent', "
    "'awaiting_customer', 'classified', 'handoff_requested', 'completed', "
    "'stopped_human_takeover', 'fallback_escalated', 'expired', 'failed', "
    "'ineligible')"
)


def upgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "ai_intake_sessions", type_="check")
    op.create_check_constraint(
        _CONSTRAINT,
        "ai_intake_sessions",
        _STATES_WITH_RESOLVED,
    )


def downgrade() -> None:
    op.execute(
        "UPDATE ai_intake_sessions SET state = 'completed' WHERE state = 'resolved'"
    )
    op.drop_constraint(_CONSTRAINT, "ai_intake_sessions", type_="check")
    op.create_check_constraint(
        _CONSTRAINT,
        "ai_intake_sessions",
        _PREVIOUS_STATES,
    )
