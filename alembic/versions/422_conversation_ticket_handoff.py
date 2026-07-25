"""Add native conversation-to-ticket provenance.

Revision ID: 422_conversation_ticket_handoff
Revises: 421_service_extension_activity_sot

Before this revision a ticket opened from an inbox conversation left no trace of
where it came from: the workspace's "Create ticket" control was a demo adapter,
and nothing in `support_tickets` referenced `inbox_conversations`. Agents could
not see that a ticket already existed for a thread, and the inbox could not show
that a thread had been escalated.

This mirrors `work_order.origin_ticket_id` (migration 382): a nullable FK on the
downstream row pointing back at its origin, RESTRICT so an origin cannot be
deleted out from under its consequence, and an index because the common read is
"tickets for this conversation".

One conversation may issue many tickets. There is no backfill — no prior data
carried this relationship, and inferring it from timestamps or subscriber
overlap would manufacture provenance that was never recorded.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "422_conversation_ticket_handoff"
down_revision = "421_service_extension_activity_sot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "support_tickets",
        sa.Column(
            "origin_conversation_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
    )
    op.create_foreign_key(
        "fk_support_tickets_origin_conversation_id_inbox_conversations",
        "support_tickets",
        "inbox_conversations",
        ["origin_conversation_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_support_tickets_origin_conversation_id",
        "support_tickets",
        ["origin_conversation_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_support_tickets_origin_conversation_id", table_name="support_tickets"
    )
    op.drop_constraint(
        "fk_support_tickets_origin_conversation_id_inbox_conversations",
        "support_tickets",
        type_="foreignkey",
    )
    op.drop_column("support_tickets", "origin_conversation_id")
