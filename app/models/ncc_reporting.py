"""Durable evidence for scheduled NCC complaints-report deliveries."""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class NccWeeklyReportRunStatus(enum.StrEnum):
    """Owner-visible state of one scheduled local-date occurrence."""

    failed = "failed"
    queued = "queued"


class NccWeeklyReportRun(Base):
    """Immutable workbook artifact plus durable queue/failure evidence."""

    __tablename__ = "ncc_weekly_report_runs"
    __table_args__ = (
        UniqueConstraint(
            "schedule_key",
            "scheduled_local_date",
            name="uq_ncc_weekly_report_runs_occurrence",
        ),
        CheckConstraint("row_count >= 0", name="ck_ncc_weekly_runs_row_count"),
        CheckConstraint(
            "not_filable_count >= 0 AND not_filable_count <= row_count",
            name="ck_ncc_weekly_runs_not_filable_count",
        ),
        CheckConstraint(
            "(status = 'queued' AND artifact_content IS NOT NULL "
            "AND artifact_sha256 IS NOT NULL AND notification_id IS NOT NULL) "
            "OR (status = 'failed' AND failure_code IS NOT NULL)",
            name="ck_ncc_weekly_runs_state_evidence",
        ),
        Index("ix_ncc_weekly_report_runs_created", "created_at"),
        Index("ix_ncc_weekly_report_runs_notification", "notification_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    schedule_key: Mapped[str] = mapped_column(String(80), nullable=False)
    scheduled_local_date: Mapped[date] = mapped_column(Date, nullable=False)
    schedule_timezone: Mapped[str] = mapped_column(String(80), nullable=False)
    scheduled_local_time: Mapped[str] = mapped_column(String(8), nullable=False)
    window_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    window_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    configuration_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[NccWeeklyReportRunStatus] = mapped_column(
        Enum(NccWeeklyReportRunStatus, name="nccweeklyreportrunstatus"),
        nullable=False,
    )
    artifact_filename: Mapped[str | None] = mapped_column(String(255))
    artifact_content_type: Mapped[str | None] = mapped_column(String(160))
    artifact_content: Mapped[bytes | None] = mapped_column(LargeBinary)
    artifact_sha256: Mapped[str | None] = mapped_column(String(64))
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    not_filable_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    notification_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("notifications.id", ondelete="SET NULL"),
        nullable=True,
    )
    failure_code: Mapped[str | None] = mapped_column(String(120))
    failure_detail: Mapped[str | None] = mapped_column(Text)
    command_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    correlation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
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

    notification = relationship("Notification")
