"""Add a DB-level unique constraint on offer_versions(offer_id, version_number).

Before this migration, uniqueness of ``version_number`` within one offer was
enforced ONLY in application code: an advisory lock plus a pre-insert
existence check inside
``app.services.catalog.offer_access_requirement.admit_offer_version``. A
caller that bypassed that owner command entirely (or a code path added later
that writes ``OfferVersion`` rows directly) had no database-level backstop at
all -- this migration adds one.

This migration is authored only and deliberately NOT applied against any
database as part of this change. It is expected to be able to FAIL against
a database that already holds duplicate (offer_id, version_number) pairs
(pre-existing dirty data) -- that is correct, intended behavior: surfacing a
real data problem loudly at migration time is preferable to silently
omitting the constraint because dirty data might exist. Any such failure
must be repaired (de-duplicate or re-number the conflicting rows) before this
migration can apply; that repair is out of scope here.

Revision ID: 611_offer_versions_unique_version_number
Revises: 610_offer_version_admission_permission
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "611_offer_versions_unique_version_number"
down_revision: str | None = "610_offer_version_admission_permission"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT_NAME = "uq_offer_versions_offer_id_version_number"
_TABLE_NAME = "offer_versions"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # SET LOCAL is scoped to this migration's own transaction and reverts
        # automatically when it ends -- unlike a plain SET, it never discards
        # the operator-configured global lock_timeout (alembic/env.py) for
        # any statement that runs after this one. Mirrors
        # 608_offer_access_requirement's exact budget.
        op.execute("SET LOCAL lock_timeout = '5s'")
        op.execute("SET LOCAL statement_timeout = '15min'")
    op.create_unique_constraint(
        _CONSTRAINT_NAME,
        _TABLE_NAME,
        ["offer_id", "version_number"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")
        op.execute("SET LOCAL statement_timeout = '15min'")
    op.drop_constraint(_CONSTRAINT_NAME, _TABLE_NAME, type_="unique")
