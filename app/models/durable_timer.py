"""Durable per-entity timers (ADR 0007 Phase 5, section 7).

Every time-based business transition is represented by exactly one current
timer owned by the transition that requires it: owner + entity + purpose +
generation + due time + expected source version + declared output event.

The owning transition creates, replaces, or cancels its timer inside its own
transaction. A unique current generation makes stale deliveries harmless: a
fired timer whose generation is no longer current is idempotently rejected.

The timer runtime scans ``due_at <= now`` on an index, emits the declared
trigger, and records delivery. It performs no customer, invoice, funding, or
access decision — those belong to the consumer that declared the timer. This
is what retires the business-wide dunning and prepaid sweeps.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class TimerStatus(enum.Enum):
    """Lifecycle of one timer generation."""

    scheduled = "scheduled"
    fired = "fired"
    canceled = "canceled"
    superseded = "superseded"


class DurableTimer(Base):
    """One generation of one owner's timer for one entity and purpose."""

    __tablename__ = "durable_timers"
    __table_args__ = (
        UniqueConstraint(
            "owner",
            "entity_kind",
            "entity_id",
            "purpose",
            "generation",
            name="uq_durable_timer_generation",
        ),
        # One *current* timer per (owner, entity, purpose).
        Index(
            "uq_durable_timer_current",
            "owner",
            "entity_kind",
            "entity_id",
            "purpose",
            unique=True,
            postgresql_where=text("status = 'scheduled'"),
            sqlite_where=text("status = 'scheduled'"),
        ),
        # The bounded due scan.
        Index("ix_durable_timer_due", "status", "due_at"),
        CheckConstraint("generation >= 1", name="ck_durable_timer_generation"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner: Mapped[str] = mapped_column(String(120), nullable=False)
    entity_kind: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    purpose: Mapped[str] = mapped_column(String(80), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # The source version the owner expected when it scheduled this timer; the
    # consumer revalidates it so a stale fire cannot act on superseded state.
    expected_source_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1
    )
    output_event_type: Mapped[str] = mapped_column(String(100), nullable=False)

    status: Mapped[TimerStatus] = mapped_column(
        Enum(TimerStatus, name="timerstatus"),
        nullable=False,
        default=TimerStatus.scheduled,
    )
    fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fired_event_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

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
