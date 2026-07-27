"""Finite sales-order funding gate evidence (ADR 0007 Phase 6, section 9).

`SalesOrder.amount_paid` is provenance, not authority. The target stores the
exact finite obligation identities an order is funded by and the resolution
evidence that advanced its gate — so partial funding never releases service,
full finite funding advances the order exactly once, and future recurring
obligations on the subscription contract cannot reopen or inflate the
historical order result.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.billing_contract import BillingRecordAuthority


class FundingGateState(enum.Enum):
    """The gate advances forward exactly once."""

    pending = "pending"
    funded = "funded"


class SalesOrderFundingGate(Base):
    """One order's finite funding gate and its transition evidence."""

    __tablename__ = "sales_order_funding_gates"
    __table_args__ = (
        UniqueConstraint("sales_order_id", name="uq_sales_order_funding_gate"),
        Index("ix_sales_order_funding_gate_state", "state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    sales_order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sales_orders.id", ondelete="RESTRICT"),
        nullable=False,
    )
    authority: Mapped[BillingRecordAuthority] = mapped_column(
        Enum(BillingRecordAuthority, name="billingrecordauthority"),
        nullable=False,
        default=BillingRecordAuthority.shadow,
    )
    state: Mapped[FundingGateState] = mapped_column(
        Enum(FundingGateState, name="fundinggatestate"),
        nullable=False,
        default=FundingGateState.pending,
    )
    funded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    funded_event_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    command_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    correlation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class SalesOrderFundingObligation(Base):
    """One finite obligation an order's gate depends on, with its resolution."""

    __tablename__ = "sales_order_funding_obligations"
    __table_args__ = (
        UniqueConstraint(
            "gate_id",
            "obligation_id",
            name="uq_sales_order_funding_obligation",
        ),
        Index("ix_sales_order_funding_obligation_gate", "gate_id", "resolved"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    gate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sales_order_funding_gates.id", ondelete="CASCADE"),
        nullable=False,
    )
    obligation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("billing_obligations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    resolved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    resolution_kind: Mapped[str | None] = mapped_column(String(60))
    resolved_event_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
