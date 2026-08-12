"""Expand Sub's audit table to the kernel R1 union without rewriting history.

This product-owned migration mirrors the three columns already present in the
kernel a42 candidate. It does not compose the kernel lineage: kernel revision
0001 still collides with Sub's independently-owned ``audit_events`` table and
can run only after every table in that atomic revision is dispositioned.

All additions are nullable and no row is backfilled. In particular,
``created_at`` is added without a default and only then receives ``now()`` as a
future-insert default. Combining those operations would make every historical
row read as if it were persisted at migration time, a false statement that
could not later be distinguished from real data.

``actor_party_id`` has no foreign key. Audit attribution must survive deletion
of the Party used only as optional accountability enrichment. ``details`` is
JSONB so Sub can dual-write its legacy ``metadata`` plus IP/user-agent evidence
without dropping any queryable legacy column during expansion.

The operations take ordinary PostgreSQL catalog locks and use the deployment
migration runner's configured lock/statement timeouts and retry policy. No
table scan or data rewrite is intended. Downgrade removes only these additive
columns and their index; it is destructive to post-R1 values and is therefore
appropriate only before any R1 writer is admitted.

Revision ID: 524_audit_events_kernel_r1
Revises: 523_domain_settings_tenant_fk
Create Date: 2026-08-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "524_audit_events_kernel_r1"
down_revision: str | None = "523_domain_settings_tenant_fk"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "audit_events"
ACTOR_PARTY_INDEX = "ix_audit_events_actor_party_id"


def upgrade() -> None:
    op.add_column(
        TABLE,
        sa.Column("actor_party_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(TABLE, sa.Column("details", postgresql.JSONB(), nullable=True))

    # Two DDL statements are load-bearing: historical persistence time is
    # unknown and must remain NULL, while only future inserts receive now().
    op.add_column(
        TABLE,
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.alter_column(
        TABLE,
        "created_at",
        existing_type=sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        existing_nullable=True,
    )
    op.create_index(ACTOR_PARTY_INDEX, TABLE, ["actor_party_id"])


def downgrade() -> None:
    op.drop_index(ACTOR_PARTY_INDEX, table_name=TABLE)
    op.drop_column(TABLE, "created_at")
    op.drop_column(TABLE, "details")
    op.drop_column(TABLE, "actor_party_id")
