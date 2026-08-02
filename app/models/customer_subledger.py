"""Operational customer subledger records (ADR 0007 Phase 3).

One business-owner command produces one immutable :class:`CustomerPostingGroup`
carrying zero or more typed :class:`CustomerPositionEffect` rows. Customer
financial position is derived only from these postings, per currency and
semantic lane — never by recombining documents with selected ledger rows.

Position effects carry typed economic meanings (receivable issue, settlement,
credit creation/consumption, prepaid reservation/consumption, write-off,
refund, adjustment). They are deliberately not ERP chart-of-account debits and
credits: Dotmac ERP owns the general ledger, and this subledger must never
become a shadow GL.

Wrong postings are corrected by a linked reversal group. Posted economic
history is never updated or deleted.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.billing_contract import BillingRecordAuthority


class PostingCommandKind(enum.Enum):
    """Closed vocabulary of business results that produce a posting group."""

    receivable_issue = "receivable_issue"
    payment_settlement = "payment_settlement"
    credit_note_application = "credit_note_application"
    customer_credit_deposit = "customer_credit_deposit"
    customer_credit_application = "customer_credit_application"
    prepaid_reservation = "prepaid_reservation"
    prepaid_consumption = "prepaid_consumption"
    write_off = "write_off"
    refund = "refund"
    adjustment = "adjustment"
    opening_position = "opening_position"
    reversal = "reversal"


class PostingProducer(enum.Enum):
    """Closed set of owners allowed to stage posting groups.

    Typed provenance: the participant API accepts only these identities, so a
    posting can never carry a free-form producer string. Extend the enum in
    the same change that registers a new producing owner.
    """

    account_credit_deposits = "financial.account_credit_deposits"
    payment_provider_events = "financial.payment_provider_events"
    payment_proofs = "financial.payment_proofs"
    account_credit_applications = "financial.account_credit_applications"
    prepaid_draft_reconciliation = "financial.prepaid_draft_reconciliation"
    account_adjustments = "financial.account_adjustments"


class PostingSourceKind(enum.Enum):
    """Closed set of source records a posting group may cite."""

    payment = "payment"
    payment_allocation = "payment_allocation"
    payment_refund = "payment_refund"
    payment_reversal = "payment_reversal"
    invoice = "invoice"
    ledger_entry = "ledger_entry"
    credit_note = "credit_note"
    account_adjustment = "account_adjustment"
    prepaid_opening_funding_consumption = "prepaid_opening_funding_consumption"
    prepaid_funding_baseline = "prepaid_funding_baseline"


class PositionEffectKind(enum.Enum):
    """Typed economic meaning of one position effect.

    Each kind moves exactly one semantic lane of the per-currency customer
    position. These are operational meanings, not accounting debits/credits.
    """

    receivable_issued = "receivable_issued"
    receivable_settled = "receivable_settled"
    customer_credit_created = "customer_credit_created"
    customer_credit_consumed = "customer_credit_consumed"
    prepaid_funding_reserved = "prepaid_funding_reserved"
    prepaid_reservation_released = "prepaid_reservation_released"
    prepaid_funding_consumed = "prepaid_funding_consumed"
    receivable_written_off = "receivable_written_off"
    credit_refunded = "credit_refunded"
    adjustment_applied = "adjustment_applied"


_posting_command_enum = Enum(PostingCommandKind, name="postingcommandkind")
_effect_kind_enum = Enum(PositionEffectKind, name="positioneffectkind")
_authority_enum = Enum(BillingRecordAuthority, name="billingrecordauthority")


class CustomerPostingGroup(Base):
    """Immutable posting evidence for exactly one idempotent business result."""

    __tablename__ = "customer_posting_groups"
    __table_args__ = (
        # One posting group per idempotent business result.
        UniqueConstraint(
            "producer_owner",
            "idempotency_key",
            name="uq_customer_posting_group_idempotency",
        ),
        # One active reversal chain: a group is reversed at most once.
        UniqueConstraint(
            "reverses_group_id",
            name="uq_customer_posting_group_single_reversal",
        ),
        Index("ix_customer_posting_group_account", "account_id", "currency"),
        Index("ix_customer_posting_group_source", "source_kind", "source_id"),
        Index("ix_customer_posting_group_authority", "authority"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscribers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    authority: Mapped[BillingRecordAuthority] = mapped_column(
        _authority_enum, nullable=False, default=BillingRecordAuthority.shadow
    )

    # The business command that produced this result.
    command_kind: Mapped[PostingCommandKind] = mapped_column(
        _posting_command_enum, nullable=False
    )
    producer_owner: Mapped[str] = mapped_column(String(120), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(80), nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    command_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    correlation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    causation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)

    reverses_group_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("customer_posting_groups.id", ondelete="RESTRICT"),
    )

    actor: Mapped[str] = mapped_column(String(160), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)

    effects: Mapped[list[CustomerPositionEffect]] = relationship(
        "CustomerPositionEffect",
        back_populates="group",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class CustomerPositionEffect(Base):
    """One typed movement of one semantic lane, always a positive amount."""

    __tablename__ = "customer_position_effects"
    __table_args__ = (
        Index("ix_customer_position_effect_group", "group_id"),
        Index("ix_customer_position_effect_links", "obligation_id"),
        CheckConstraint("amount > 0", name="ck_customer_position_effect_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("customer_posting_groups.id", ondelete="CASCADE"),
        nullable=False,
    )
    effect: Mapped[PositionEffectKind] = mapped_column(
        _effect_kind_enum, nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)

    # Exact structural links where applicable. Metadata is never a join.
    obligation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("billing_obligations.id", ondelete="RESTRICT"),
    )
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    payment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    credit_note_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    entitlement_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    group: Mapped[CustomerPostingGroup] = relationship(
        "CustomerPostingGroup", back_populates="effects"
    )
