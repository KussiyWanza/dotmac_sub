"""Add the missing trigram indexes for admin customer search.

The canonical customer-list search combines every searchable customer column
with ``OR`` predicates. Production already has trigram indexes for the other
text branches, but ``display_name`` and ``phone`` were left unindexed. Either
unindexed branch can make PostgreSQL choose a sequential scan for the combined
predicate, which is then paid twice for the page count and result query.

PostgreSQL builds these indexes concurrently so customer writes remain
available during deployment. The extension is part of the base schema.

Revision ID: 504_customer_search_trigram_indexes
Revises: 503_reconcile_ticket_portal_visibility
Create Date: 2026-08-08
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "504_customer_search_trigram_indexes"
down_revision: str | None = "503_reconcile_ticket_portal_visibility"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEXES: tuple[tuple[str, str], ...] = (
    ("ix_trgm_subscribers_display_name", "display_name"),
    ("ix_trgm_subscribers_phone", "phone"),
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            for name, column in _INDEXES:
                op.execute(
                    f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} "
                    f"ON subscribers USING gin ({column} gin_trgm_ops)"
                )
        return

    for name, column in _INDEXES:
        op.execute(f"CREATE INDEX IF NOT EXISTS {name} ON subscribers ({column})")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            for name, _column in _INDEXES:
                op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
        return

    for name, _column in _INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name}")
