"""Persisted effective-dated SLA policy versions.

OUTAGE_SLA_SPINE §4: the contractual terms a customer is owed become immutable,
effective-dated versions instead of the mutable `sla_profiles` row, whose every
edit silently rewrote historical scores.

Two constraints here exist only in the migration and cannot be expressed by
model metadata alone, so both carry executable canaries in
`tests/integration/test_sla_policy_versions_postgres.py`:

- `ex_sla_policy_versions_no_overlap` — a GiST exclusion constraint forbidding
  two versions of one `policy_key` covering the same instant. This is what
  makes "the policy in force at T" a single answer rather than a guess, and
  it is enforceable only in the database: two concurrent writers each reading
  before writing would both pass an application-level check.
- `ck_sla_policy_versions_scope_matches_source` — binds the precedence claim
  to the scope column, so a row cannot claim subscription-contract precedence
  with no subscription.
- `ck_sla_policy_versions_key_is_derived` — binds `policy_key` to the derived
  `(source, scope)` identity, so the series name cannot disagree with the real
  scope it governs.

Requires the `btree_gist` extension for the mixed equality/range exclusion.

Expand-only: one new table, no backfill. `sla_profiles` is untouched and stays
the fallback until the cutover slice retires it. Downgrade drops the table and
leaves the extension in place (other objects may rely on it).

Revision ID: 467_sla_policy_versions
Revises: 466_team_inbox_channel_ai_routes
Create Date: 2026-08-03
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "467_sla_policy_versions"
down_revision = "466_team_inbox_channel_ai_routes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"
    if is_postgres:
        op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    op.create_table(
        "sla_policy_versions",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False
        ),
        sa.Column("policy_key", sa.String(length=120), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=30), nullable=False),
        sa.Column(
            "subscription_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subscriptions.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "subscriber_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subscribers.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "offer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("catalog_offers.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("availability_target_percent", sa.Numeric(6, 3), nullable=True),
        sa.Column(
            "calendar_timezone",
            sa.String(length=64),
            nullable=False,
            server_default="Africa/Lagos",
        ),
        sa.Column(
            "maintenance_excludable",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column("credit_percent_per_breach", sa.Numeric(6, 3), nullable=True),
        sa.Column("credit_cap_percent", sa.Numeric(6, 3), nullable=True),
        sa.Column("contract_reference", sa.String(length=200), nullable=True),
        sa.Column("established_by", sa.String(length=120), nullable=True),
        sa.Column(
            "supersedes_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sla_policy_versions.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("command_fingerprint", sa.String(length=80), nullable=True),
        sa.Column("command_idempotency_key", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "policy_key", "version", name="uq_sla_policy_versions_key_version"
        ),
        sa.UniqueConstraint(
            "command_fingerprint", name="uq_sla_policy_versions_fingerprint"
        ),
        sa.UniqueConstraint(
            "command_idempotency_key",
            name="uq_sla_policy_versions_idempotency_key",
        ),
        sa.CheckConstraint("version >= 1", name="ck_sla_policy_versions_version"),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_sla_policy_versions_range",
        ),
        sa.CheckConstraint(
            "availability_target_percent IS NULL "
            "OR (availability_target_percent > 0 "
            "AND availability_target_percent <= 100)",
            name="ck_sla_policy_versions_target_bounds",
        ),
        sa.CheckConstraint(
            "source = 'internal_measurement' "
            "OR availability_target_percent IS NOT NULL",
            name="ck_sla_policy_versions_contractual_target",
        ),
        sa.CheckConstraint(
            "(source = 'subscription_contract' AND subscription_id IS NOT NULL "
            " AND subscriber_id IS NULL AND offer_id IS NULL) "
            "OR (source = 'account_contract' AND subscriber_id IS NOT NULL "
            " AND subscription_id IS NULL AND offer_id IS NULL) "
            "OR (source = 'offer_version' AND offer_id IS NOT NULL "
            " AND subscription_id IS NULL AND subscriber_id IS NULL) "
            "OR (source = 'internal_measurement' AND subscription_id IS NULL "
            " AND subscriber_id IS NULL AND offer_id IS NULL)",
            name="ck_sla_policy_versions_scope_matches_source",
        ),
    )
    op.create_index(
        "ix_sla_policy_versions_key", "sla_policy_versions", ["policy_key", "version"]
    )
    op.create_index(
        "ix_sla_policy_versions_subscription",
        "sla_policy_versions",
        ["subscription_id"],
    )
    op.create_index(
        "ix_sla_policy_versions_subscriber", "sla_policy_versions", ["subscriber_id"]
    )
    op.create_index("ix_sla_policy_versions_offer", "sla_policy_versions", ["offer_id"])
    op.create_index(
        "ix_sla_policy_versions_effective",
        "sla_policy_versions",
        ["effective_from", "effective_to"],
    )

    if is_postgres:
        # The series identity is (source, scope). Keying the exclusion on
        # policy_key ALONE would let two different keys target the same
        # subscription for the same period, producing two equal-precedence
        # policies and an undefined resolver winner. Coalescing the three
        # nullable scope columns to a single non-null discriminator makes the
        # real scope the thing that cannot overlap.
        #
        # tstzrange '[)' matches the half-open semantics the resolver uses, so
        # one version may end exactly where the next begins; a NULL
        # effective_to becomes an unbounded upper edge.
        op.execute(
            """
            ALTER TABLE sla_policy_versions
            ADD CONSTRAINT ex_sla_policy_versions_no_overlap
            EXCLUDE USING gist (
                source WITH =,
                (COALESCE(subscription_id, subscriber_id, offer_id,
                          '00000000-0000-0000-0000-000000000000'::uuid))
                    WITH =,
                tstzrange(effective_from, effective_to, '[)') WITH &&
            )
            """
        )
        # policy_key is a FUNCTION of (source, scope), so it must not be able
        # to disagree with them. A unique index over (source, scope, key) would
        # not do this — it permits many keys per scope, each trivially unique.
        # This binds the derived identity instead, matching
        # `derive_policy_key`.
        op.execute(
            """
            ALTER TABLE sla_policy_versions
            ADD CONSTRAINT ck_sla_policy_versions_key_is_derived
            CHECK (
                policy_key = source || ':' || COALESCE(
                    subscription_id::text,
                    subscriber_id::text,
                    offer_id::text,
                    'global'
                )
            )
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "ALTER TABLE sla_policy_versions "
            "DROP CONSTRAINT IF EXISTS ex_sla_policy_versions_no_overlap"
        )
    if bind.dialect.name == "postgresql":
        op.execute(
            "ALTER TABLE sla_policy_versions "
            "DROP CONSTRAINT IF EXISTS ck_sla_policy_versions_key_is_derived"
        )
    for index in (
        "ix_sla_policy_versions_effective",
        "ix_sla_policy_versions_offer",
        "ix_sla_policy_versions_subscriber",
        "ix_sla_policy_versions_subscription",
        "ix_sla_policy_versions_key",
    ):
        op.drop_index(index, table_name="sla_policy_versions")
    op.drop_table("sla_policy_versions")
