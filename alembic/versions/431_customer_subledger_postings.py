"""Add operational customer subledger posting groups and effects.

ADR 0007 Phase 3 (expand). Tables only: no existing read path changes, and
rows are written with ``authority = 'shadow'`` until the Phase 3 cutover gate
(per-currency/lane parity plus finance sign-off) passes.

Revision ID: 431_customer_subledger_postings
Revises: 430_billing_contract_obligation_identity
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "431_customer_subledger_postings"
down_revision = "430_billing_contract_obligation_identity"
branch_labels = None
depends_on = None

_AUTHORITY = sa.Enum("shadow", "authoritative", name="billingrecordauthority")
_COMMAND_KIND = sa.Enum(
    "receivable_issue",
    "payment_settlement",
    "credit_note_application",
    "customer_credit_deposit",
    "customer_credit_application",
    "prepaid_reservation",
    "prepaid_consumption",
    "write_off",
    "refund",
    "adjustment",
    "opening_position",
    "reversal",
    name="postingcommandkind",
)
_EFFECT_KIND = sa.Enum(
    "receivable_issued",
    "receivable_settled",
    "customer_credit_created",
    "customer_credit_consumed",
    "prepaid_funding_reserved",
    "prepaid_reservation_released",
    "prepaid_funding_consumed",
    "receivable_written_off",
    "credit_refunded",
    "adjustment_applied",
    name="positioneffectkind",
)


def upgrade() -> None:
    bind = op.get_bind()
    # billingrecordauthority already exists from revision 427.
    _COMMAND_KIND.create(bind, checkfirst=True)
    _EFFECT_KIND.create(bind, checkfirst=True)

    op.create_table(
        "customer_posting_groups",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subscribers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("authority", _AUTHORITY, nullable=False, server_default="shadow"),
        sa.Column("command_kind", _COMMAND_KIND, nullable=False),
        sa.Column("producer_owner", sa.String(120), nullable=False),
        sa.Column("source_kind", sa.String(80), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("command_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("causation_id", postgresql.UUID(as_uuid=True)),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column(
            "reverses_group_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customer_posting_groups.id", ondelete="RESTRICT"),
        ),
        sa.Column("actor", sa.String(160), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.UniqueConstraint(
            "producer_owner",
            "idempotency_key",
            name="uq_customer_posting_group_idempotency",
        ),
        sa.UniqueConstraint(
            "reverses_group_id",
            name="uq_customer_posting_group_single_reversal",
        ),
    )
    op.create_index(
        "ix_customer_posting_group_account",
        "customer_posting_groups",
        ["account_id", "currency"],
    )
    op.create_index(
        "ix_customer_posting_group_source",
        "customer_posting_groups",
        ["source_kind", "source_id"],
    )
    op.create_index(
        "ix_customer_posting_group_authority",
        "customer_posting_groups",
        ["authority"],
    )

    op.create_table(
        "customer_position_effects",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "group_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customer_posting_groups.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("effect", _EFFECT_KIND, nullable=False),
        sa.Column("amount", sa.Numeric(14, 4), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column(
            "obligation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("billing_obligations.id", ondelete="RESTRICT"),
        ),
        sa.Column("invoice_id", postgresql.UUID(as_uuid=True)),
        sa.Column("payment_id", postgresql.UUID(as_uuid=True)),
        sa.Column("credit_note_id", postgresql.UUID(as_uuid=True)),
        sa.Column("entitlement_id", postgresql.UUID(as_uuid=True)),
        sa.CheckConstraint("amount > 0", name="ck_customer_position_effect_positive"),
    )
    op.create_index(
        "ix_customer_position_effect_group",
        "customer_position_effects",
        ["group_id"],
    )
    op.create_index(
        "ix_customer_position_effect_links",
        "customer_position_effects",
        ["obligation_id"],
    )


def downgrade() -> None:
    op.drop_table("customer_position_effects")
    op.drop_table("customer_posting_groups")
    bind = op.get_bind()
    _EFFECT_KIND.drop(bind, checkfirst=True)
    _COMMAND_KIND.drop(bind, checkfirst=True)
    # billingrecordauthority remains owned by revision 427.
