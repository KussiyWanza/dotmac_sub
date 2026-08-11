"""Canonicalize legacy resolved support-ticket status as closed.

``resolved`` was both a stored Ticket status and a user-facing label even
though the business workflow has one completed state: ``closed``. This repair
changes only the status value on matching Ticket rows. Direct SQL deliberately
leaves ``updated_at``, ``resolved_at``, ``closed_at``, ownership, comments,
attachments, metadata, audit evidence, and every other Ticket field untouched.

The same legacy value could also survive in operator status choices and ticket
automation JSON. Those exact status fields are canonicalized without changing
other configured values, conditions, actions, or their order. Historical audit
records remain immutable evidence and are not rewritten.

Every UPDATE is predicate-bounded and therefore safe to rerun. Downgrade is a
no-op because closed rows cannot be distinguished reliably from rows that were
always closed; restoring ``resolved`` would invent history.

Revision ID: 517_close_legacy_resolved_tickets
Revises: 516_material_request_erp_submission
Create Date: 2026-08-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "517_close_legacy_resolved_tickets"
down_revision: str | None = "516_material_request_erp_submission"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
        bind.execute(sa.text("SET LOCAL statement_timeout = '60s'"))

    bind.execute(
        sa.text(
            """
            UPDATE support_tickets
               SET status = 'closed'
             WHERE status = 'resolved'
            """
        )
    )

    if bind.dialect.name != "postgresql":
        return

    # Preserve option order while collapsing a possible ["resolved", "closed"]
    # duplicate into one canonical "closed" entry.
    bind.execute(
        sa.text(
            """
            WITH canonical_items AS (
                SELECT settings.id,
                       CASE
                           WHEN item.status = 'resolved' THEN 'closed'
                           ELSE item.status
                       END AS status,
                       MIN(item.ordinality) AS first_ordinality
                  FROM domain_settings AS settings
                  CROSS JOIN LATERAL jsonb_array_elements_text(
                      settings.value_json::jsonb
                  ) WITH ORDINALITY AS item(status, ordinality)
                 WHERE settings.key = 'support_ticket_status_options'
                   AND jsonb_typeof(settings.value_json::jsonb) = 'array'
                   AND settings.value_json::jsonb ? 'resolved'
                 GROUP BY settings.id,
                          CASE
                              WHEN item.status = 'resolved' THEN 'closed'
                              ELSE item.status
                          END
            ), canonical_options AS (
                SELECT id,
                       jsonb_agg(status ORDER BY first_ordinality)::json AS value_json
                  FROM canonical_items
                 GROUP BY id
            )
            UPDATE domain_settings AS settings
               SET value_json = canonical_options.value_json
              FROM canonical_options
             WHERE settings.id = canonical_options.id
            """
        )
    )
    bind.execute(
        sa.text(
            """
            UPDATE support_ticket_automation_rules
               SET action_value = jsonb_set(
                       action_value::jsonb,
                       '{status}',
                       to_jsonb('closed'::text),
                       false
                   )::json
             WHERE action_type::text = 'set_status'
               AND action_value->>'status' = 'resolved'
            """
        )
    )
    bind.execute(
        sa.text(
            """
            UPDATE support_ticket_automation_rules
               SET conditions = jsonb_set(
                       conditions::jsonb,
                       '{status}',
                       to_jsonb('closed'::text),
                       false
                   )::json
             WHERE conditions->>'status' = 'resolved'
            """
        )
    )


def downgrade() -> None:
    """Do not recreate a retired status or falsify which rows once used it."""
