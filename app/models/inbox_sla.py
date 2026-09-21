"""Authoritative Inbox SLA configuration, clocks, and audit evidence."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, time

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class InboxSlaPolicy(Base):
    __tablename__ = "inbox_sla_policies"
    __table_args__ = (
        UniqueConstraint("name", name="uq_inbox_sla_policies_name"),
        Index("ix_inbox_sla_policies_active_default", "is_active", "is_default"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(
        String(64), default="Africa/Lagos", nullable=False
    )
    working_days: Mapped[list[int]] = mapped_column(
        JSON, default=lambda: [0, 1, 2, 3, 4], nullable=False
    )
    workday_start: Mapped[time] = mapped_column(Time, default=time(9), nullable=False)
    workday_end: Mapped[time] = mapped_column(Time, default=time(17), nullable=False)
    holidays: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    source_reference: Mapped[str | None] = mapped_column(String(160))
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )
    rules = relationship(
        "InboxSlaRule", back_populates="policy", cascade="all, delete-orphan"
    )


class InboxSlaRule(Base):
    __tablename__ = "inbox_sla_rules"
    __table_args__ = (
        CheckConstraint(
            "first_response_minutes > 0", name="ck_inbox_sla_rules_first_positive"
        ),
        CheckConstraint(
            "next_response_minutes IS NULL OR next_response_minutes > 0",
            name="ck_inbox_sla_rules_next_positive",
        ),
        CheckConstraint(
            "resolution_minutes > 0", name="ck_inbox_sla_rules_resolution_positive"
        ),
        CheckConstraint(
            "warning_minutes >= 0 AND warning_minutes < first_response_minutes",
            name="ck_inbox_sla_rules_warning_bounds",
        ),
        Index(
            "ix_inbox_sla_rules_match",
            "policy_id",
            "is_active",
            "service_team_id",
            "channel_type",
            "priority",
        ),
        UniqueConstraint(
            "policy_id",
            "service_team_id",
            "channel_type",
            "priority",
            name="uq_inbox_sla_rules_match",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    policy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("inbox_sla_policies.id", ondelete="CASCADE"),
        nullable=False,
    )
    service_team_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_teams.id", ondelete="RESTRICT")
    )
    channel_type: Mapped[str | None] = mapped_column(String(40))
    priority: Mapped[int | None] = mapped_column(Integer)
    first_response_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    next_response_minutes: Mapped[int | None] = mapped_column(Integer)
    resolution_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    warning_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    source_reference: Mapped[str | None] = mapped_column(String(160))
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )
    policy = relationship("InboxSlaPolicy", back_populates="rules")


class InboxSlaClock(Base):
    __tablename__ = "inbox_sla_clocks"
    __table_args__ = (
        UniqueConstraint("conversation_id", name="uq_inbox_sla_clocks_conversation"),
        Index(
            "ix_inbox_sla_clocks_due",
            "status",
            "first_response_due_at",
            "resolution_due_at",
        ),
        Index("ix_inbox_sla_clocks_policy", "policy_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("inbox_conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    policy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("inbox_sla_policies.id", ondelete="RESTRICT"),
        nullable=False,
    )
    rule_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("inbox_sla_rules.id", ondelete="RESTRICT"),
        nullable=False,
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    first_response_due_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    first_response_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_response_due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    next_response_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution_due_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    total_paused_seconds: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    status: Mapped[str] = mapped_column(String(24), default="running", nullable=False)
    warning_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    breach_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )


class InboxSlaEvent(Base):
    __tablename__ = "inbox_sla_events"
    __table_args__ = (
        UniqueConstraint("clock_id", "event_key", name="uq_inbox_sla_events_key"),
        Index("ix_inbox_sla_events_conversation", "conversation_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    clock_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("inbox_sla_clocks.id", ondelete="CASCADE"),
        nullable=False,
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("inbox_conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_key: Mapped[str] = mapped_column(String(160), nullable=False)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
