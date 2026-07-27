"""Durable owner-output consumer receipts (ADR 0007 Phase 4).

The outbox row (``EventStore``) already stages an owner's output in the same
transaction as its state change. What it cannot prove is the *consumer* side:
that a given consumer applied a given event exactly once, or that it reached an
explicitly reviewed terminal failure instead of being silently abandoned.

An :class:`OwnerOutputReceipt` records that proof. The unique
``(consumer, event_id)`` pair makes replay harmless, and the receipt commits in
the same transaction as the consumer's business effect — so the system only
ever contains completed, pending, retrying, or explicitly failed work.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, Enum, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ReceiptOutcome(enum.Enum):
    """How one consumer finished one owner output."""

    succeeded = "succeeded"
    terminal_failure = "terminal_failure"


class OwnerOutputReceipt(Base):
    """One consumer's committed outcome for one owner-output event."""

    __tablename__ = "owner_output_receipts"
    __table_args__ = (
        UniqueConstraint(
            "consumer",
            "event_id",
            name="uq_owner_output_receipt_consumer_event",
        ),
        Index("ix_owner_output_receipt_event", "event_id"),
        Index("ix_owner_output_receipt_outcome", "outcome"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # The consuming owner (manifest service name).
    consumer: Mapped[str] = mapped_column(String(120), nullable=False)
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    producer_owner: Mapped[str] = mapped_column(String(120), nullable=False)
    schema_version: Mapped[int] = mapped_column(nullable=False, default=1)

    outcome: Mapped[ReceiptOutcome] = mapped_column(
        Enum(ReceiptOutcome, name="receiptoutcome"), nullable=False
    )
    # The consumer's own business idempotency key for the applied effect.
    effect_idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    # Terminal failures carry reviewable evidence; success carries none.
    failure_reason: Mapped[str | None] = mapped_column(Text)

    command_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    correlation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    causation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
