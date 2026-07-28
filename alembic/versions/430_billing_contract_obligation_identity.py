"""Add structural billing contract, cadence, and obligation identity.

ADR 0007 Phase 1 (expand). This migration only creates tables: no existing
read path changes, no existing row is rewritten, and every row written by the
new owners carries ``authority = 'shadow'`` until the Phase 1 cutover gate
passes.

PostgreSQL-only guarantees live here rather than in ``__table_args__`` so the
SQLite unit-test harness can still create the same tables:

- ``btree_gist`` plus an exclusion constraint proving no two effective versions
  of one contract overlap in time (ADR 0007 invariant 1);
- the partial unique index for the single open-ended effective version.

Revision ID: 430_billing_contract_obligation_identity
Revises: 429_inbox_conversation_participants
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "430_billing_contract_obligation_identity"
down_revision = "429_inbox_conversation_participants"
branch_labels = None
depends_on = None


_AUTHORITY = postgresql.ENUM(
    "shadow",
    "authoritative",
    name="billingrecordauthority",
    create_type=False,
)
_SOURCE_KIND = postgresql.ENUM(
    "sales_order_line",
    "plan_change",
    "renewal",
    "staff_correction",
    "migration_backfill",
    name="billingcontractsourcekind",
    create_type=False,
)
_VERSION_STATUS = postgresql.ENUM(
    "draft",
    "effective",
    "superseded",
    "canceled",
    name="billingcontractversionstatus",
    create_type=False,
)
_RATE_BASIS = postgresql.ENUM(
    "fixed_per_service_period",
    "per_rate_unit",
    "per_quantity",
    "usage_metered",
    name="ratebasis",
    create_type=False,
)
_INTERVAL_UNIT = postgresql.ENUM(
    "day",
    "week",
    "month",
    "year",
    name="intervalunit",
    create_type=False,
)
_COLLECTION_TIMING = postgresql.ENUM(
    "advance",
    "arrears",
    name="collectiontiming",
    create_type=False,
)
_ALIGNMENT = postgresql.ENUM(
    "contract_anniversary",
    "calendar_period_start",
    "fixed_anchor_day",
    name="cadencealignment",
    create_type=False,
)
_END_OF_MONTH = postgresql.ENUM(
    "clamp_to_month_end",
    "strict_same_day_or_skip",
    name="endofmonthrule",
    create_type=False,
)
_PRORATION = postgresql.ENUM(
    "none",
    "full_period",
    "actual_calendar_days",
    "actual_elapsed_time",
    name="prorationpolicy",
    create_type=False,
)
_CHARGE_COMPONENT = postgresql.ENUM(
    "recurring_service",
    "installation",
    "activation",
    "addon",
    "equipment",
    "usage",
    "other",
    name="chargecomponent",
    create_type=False,
)
_ACCOUNTING_TREATMENT = postgresql.ENUM(
    "receivable",
    "prepaid_consumption",
    "non_cash_grant",
    name="accountingtreatment",
    create_type=False,
)
_OBLIGATION_STATE = postgresql.ENUM(
    "scheduled",
    "open",
    "partially_resolved",
    "resolved",
    "canceled",
    "written_off",
    name="obligationstate",
    create_type=False,
)
_OBLIGATION_RESOLUTION = postgresql.ENUM(
    "settlement",
    "credit",
    "prepaid_consumption",
    "grant",
    "waiver",
    "write_off",
    "pre_earning_cancellation",
    "reversal",
    name="obligationresolutionkind",
    create_type=False,
)

_ALL_ENUMS = (
    _AUTHORITY,
    _SOURCE_KIND,
    _VERSION_STATUS,
    _RATE_BASIS,
    _INTERVAL_UNIT,
    _COLLECTION_TIMING,
    _ALIGNMENT,
    _END_OF_MONTH,
    _PRORATION,
    _CHARGE_COMPONENT,
    _ACCOUNTING_TREATMENT,
    _OBLIGATION_STATE,
    _OBLIGATION_RESOLUTION,
)


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    bind = op.get_bind()
    for enum_type in _ALL_ENUMS:
        enum_type.create(bind, checkfirst=True)

    op.create_table(
        "billing_contracts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subscribers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "subscription_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subscriptions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("authority", _AUTHORITY, nullable=False, server_default="shadow"),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("subscription_id", name="uq_billing_contract_subscription"),
    )
    op.create_index("ix_billing_contract_account", "billing_contracts", ["account_id"])
    op.create_index("ix_billing_contract_authority", "billing_contracts", ["authority"])

    op.create_table(
        "billing_contract_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "contract_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("billing_contracts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", _VERSION_STATUS, nullable=False),
        sa.Column("authority", _AUTHORITY, nullable=False, server_default="shadow"),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subscribers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "subscription_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subscriptions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("source_kind", _SOURCE_KIND, nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True)),
        sa.Column("contracted_price", sa.Numeric(14, 4), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("rate_basis", _RATE_BASIS, nullable=False),
        sa.Column("rate_unit", _INTERVAL_UNIT, nullable=False),
        sa.Column("rate_quantity", sa.Numeric(14, 4), nullable=False),
        sa.Column("service_interval_unit", _INTERVAL_UNIT, nullable=False),
        sa.Column("service_interval_count", sa.Integer(), nullable=False),
        sa.Column("invoice_interval_unit", _INTERVAL_UNIT, nullable=False),
        sa.Column("invoice_interval_count", sa.Integer(), nullable=False),
        sa.Column("collection_timing", _COLLECTION_TIMING, nullable=False),
        sa.Column("alignment", _ALIGNMENT, nullable=False),
        sa.Column("anchor_day", sa.Integer()),
        sa.Column("end_of_month_rule", _END_OF_MONTH, nullable=False),
        sa.Column("timezone_name", sa.String(64), nullable=False),
        sa.Column("proration_policy", _PRORATION, nullable=False),
        sa.Column(
            "payment_terms_days", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("tax_treatment_code", sa.String(60)),
        sa.Column(
            "tax_inclusive", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("discount_code", sa.String(60)),
        sa.Column("discount_amount", sa.Numeric(14, 4)),
        sa.Column(
            "supersedes_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("billing_contract_versions.id", ondelete="RESTRICT"),
        ),
        sa.Column("superseded_at", sa.DateTime(timezone=True)),
        sa.Column("actor", sa.String(160), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("command_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "contract_id", "version", name="uq_billing_contract_version_number"
        ),
        sa.CheckConstraint(
            "ends_at IS NULL OR ends_at > starts_at",
            name="ck_billing_contract_version_interval",
        ),
        sa.CheckConstraint(
            "contracted_price >= 0", name="ck_billing_contract_version_price_sign"
        ),
        sa.CheckConstraint(
            "service_interval_count > 0 AND invoice_interval_count > 0",
            name="ck_billing_contract_version_interval_counts",
        ),
        sa.CheckConstraint(
            "rate_quantity > 0", name="ck_billing_contract_version_rate_quantity"
        ),
        sa.CheckConstraint(
            "anchor_day IS NULL OR (anchor_day >= 1 AND anchor_day <= 31)",
            name="ck_billing_contract_version_anchor_day",
        ),
    )
    op.create_index(
        "ix_billing_contract_version_source",
        "billing_contract_versions",
        ["source_kind", "source_id"],
    )
    op.create_index(
        "ix_billing_contract_version_effective",
        "billing_contract_versions",
        ["contract_id", "starts_at"],
    )
    # One open-ended effective version per contract.
    op.create_index(
        "uq_billing_contract_version_current",
        "billing_contract_versions",
        ["contract_id"],
        unique=True,
        postgresql_where=sa.text("status = 'effective' AND ends_at IS NULL"),
        sqlite_where=sa.text("status = 'effective' AND ends_at IS NULL"),
    )

    op.create_table(
        "billing_contract_lines",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "contract_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("billing_contract_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("contract_line_key", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("charge_component", _CHARGE_COMPONENT, nullable=False),
        sa.Column("component_key", sa.String(120), nullable=False, server_default=""),
        sa.Column("description", sa.String(255), nullable=False),
        sa.Column("quantity", sa.Numeric(14, 4), nullable=False),
        sa.Column("unit_price", sa.Numeric(14, 4), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("accounting_treatment", _ACCOUNTING_TREATMENT, nullable=False),
        sa.Column("is_finite", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("tax_treatment_code", sa.String(60)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "contract_version_id",
            "charge_component",
            "component_key",
            name="uq_billing_contract_line_component",
        ),
        sa.CheckConstraint(
            "unit_price >= 0 AND quantity > 0",
            name="ck_billing_contract_line_amounts",
        ),
    )
    op.create_index(
        "ix_billing_contract_line_version",
        "billing_contract_lines",
        ["contract_version_id"],
    )

    op.create_table(
        "billing_obligations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "contract_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("billing_contracts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "contract_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("billing_contract_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("contract_line_key", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subscribers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "subscription_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subscriptions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("authority", _AUTHORITY, nullable=False, server_default="shadow"),
        sa.Column("charge_component", _CHARGE_COMPONENT, nullable=False),
        sa.Column("source_kind", _SOURCE_KIND, nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("net_amount", sa.Numeric(14, 4), nullable=False),
        sa.Column("tax_amount", sa.Numeric(14, 4), nullable=False),
        sa.Column("gross_amount", sa.Numeric(14, 4), nullable=False),
        sa.Column("resolved_amount", sa.Numeric(14, 4), nullable=False),
        sa.Column("accounting_treatment", _ACCOUNTING_TREATMENT, nullable=False),
        sa.Column("collection_timing", _COLLECTION_TIMING, nullable=False),
        sa.Column("is_finite", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("state", _OBLIGATION_STATE, nullable=False),
        sa.Column("resolution_kind", _OBLIGATION_RESOLUTION),
        sa.Column("opened_at", sa.DateTime(timezone=True)),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("due_at", sa.DateTime(timezone=True)),
        sa.Column(
            "corrects_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("billing_obligations.id", ondelete="RESTRICT"),
        ),
        sa.Column(
            "reversed_by_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("billing_obligations.id", ondelete="RESTRICT"),
        ),
        sa.Column("command_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        # The natural identity. This constraint, not application code, is what
        # stops a replay or a concurrent generator charging twice.
        sa.UniqueConstraint(
            "contract_line_key",
            "contract_version_id",
            "charge_component",
            "source_kind",
            "source_id",
            "source_version",
            "period_start",
            "period_end",
            "currency",
            name="uq_billing_obligation_natural_identity",
        ),
        sa.CheckConstraint(
            "period_end > period_start", name="ck_billing_obligation_period"
        ),
        sa.CheckConstraint(
            "net_amount >= 0 AND tax_amount >= 0 AND gross_amount >= 0",
            name="ck_billing_obligation_amount_sign",
        ),
        sa.CheckConstraint(
            "resolved_amount >= 0 AND resolved_amount <= gross_amount",
            name="ck_billing_obligation_resolved_bound",
        ),
    )
    op.create_index(
        "ix_billing_obligation_contract",
        "billing_obligations",
        ["contract_id", "period_start"],
    )
    op.create_index(
        "ix_billing_obligation_account_state",
        "billing_obligations",
        ["account_id", "state"],
    )
    op.create_index(
        "ix_billing_obligation_subscription",
        "billing_obligations",
        ["subscription_id", "period_start"],
    )
    op.create_index(
        "ix_billing_obligation_authority", "billing_obligations", ["authority"]
    )

    if _is_postgres():
        # ADR 0007 invariant 1: at most one version is effective for a contract
        # at an instant. A partial unique index only covers the open-ended row;
        # this proves it for every closed interval too.
        op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
        op.execute(
            """
            ALTER TABLE billing_contract_versions
            ADD CONSTRAINT ex_billing_contract_version_no_overlap
            EXCLUDE USING gist (
                contract_id WITH =,
                tstzrange(starts_at, ends_at) WITH &&
            )
            WHERE (status = 'effective')
            """
        )


def downgrade() -> None:
    if _is_postgres():
        op.execute(
            "ALTER TABLE billing_contract_versions "
            "DROP CONSTRAINT IF EXISTS ex_billing_contract_version_no_overlap"
        )

    op.drop_table("billing_obligations")
    op.drop_table("billing_contract_lines")
    op.drop_index(
        "uq_billing_contract_version_current",
        table_name="billing_contract_versions",
    )
    op.drop_table("billing_contract_versions")
    op.drop_table("billing_contracts")

    bind = op.get_bind()
    for enum_type in reversed(_ALL_ENUMS):
        enum_type.drop(bind, checkfirst=True)
