"""Planned splice work (cut sheets) bound to native work orders.

A plan is the design decision: which exact strand ends meet in which closure
tray. Execution evidence stays with the reviewed splice change request; a
plan item records at most a link to the change request that executed it, so
plan progress is derived from review state and never drifts on its own.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class FiberSplicePlanStatus(enum.Enum):
    """Typed vocabulary for the checked plan-status string column."""

    draft = "draft"
    issued = "issued"
    cancelled = "cancelled"


class FiberSplicePlan(Base):
    __tablename__ = "fiber_splice_plans"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'issued', 'cancelled')",
            name="ck_fiber_splice_plans_status_known",
        ),
        # One live plan per work order: completion gating and execution
        # matching stay decidable.
        Index(
            "uq_fiber_splice_plans_live_work_order",
            "work_order_id",
            unique=True,
            postgresql_where=text("status != 'cancelled'"),
            sqlite_where=text("status != 'cancelled'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    work_order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("work_order.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=FiberSplicePlanStatus.draft.value
    )
    notes: Mapped[str | None] = mapped_column(Text)
    created_by_person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    work_order = relationship("WorkOrder")
    items = relationship(
        "FiberSplicePlanItem",
        back_populates="plan",
        cascade="all, delete-orphan",
        order_by="FiberSplicePlanItem.position_index",
    )


class FiberSplicePlanItem(Base):
    __tablename__ = "fiber_splice_plan_items"
    __table_args__ = (
        UniqueConstraint(
            "plan_id", "position_index", name="uq_fiber_splice_plan_items_position"
        ),
        CheckConstraint(
            "from_strand_id <> to_strand_id",
            name="ck_fiber_splice_plan_items_distinct_strands",
        ),
        CheckConstraint(
            "from_strand_end IN ('a', 'b') AND to_strand_end IN ('a', 'b')",
            name="ck_fiber_splice_plan_items_strand_ends",
        ),
        Index(
            "uq_fiber_splice_plan_items_execution",
            "executed_change_request_id",
            unique=True,
            postgresql_where=text("executed_change_request_id IS NOT NULL"),
            sqlite_where=text("executed_change_request_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fiber_splice_plans.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    position_index: Mapped[int] = mapped_column(Integer, nullable=False)
    closure_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fiber_splice_closures.id", ondelete="RESTRICT"),
        nullable=False,
    )
    tray_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fiber_splice_trays.id", ondelete="RESTRICT")
    )
    tray_position: Mapped[int | None] = mapped_column(Integer)
    from_strand_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fiber_strands.id", ondelete="RESTRICT"),
        nullable=False,
    )
    from_strand_end: Mapped[str] = mapped_column(String(1), nullable=False)
    to_strand_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fiber_strands.id", ondelete="RESTRICT"),
        nullable=False,
    )
    to_strand_end: Mapped[str] = mapped_column(String(1), nullable=False)
    splice_type: Mapped[str] = mapped_column(String(80), nullable=False)
    expected_loss_db: Mapped[float | None] = mapped_column(Float)
    notes: Mapped[str | None] = mapped_column(Text)
    executed_change_request_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fiber_change_requests.id", ondelete="RESTRICT"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    plan = relationship("FiberSplicePlan", back_populates="items")
    executed_change_request = relationship("FiberChangeRequest")
