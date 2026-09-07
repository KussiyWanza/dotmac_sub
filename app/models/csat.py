import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class CsatSourceType(enum.Enum):
    support_ticket = "support_ticket"
    inbox_conversation = "inbox_conversation"


class CsatRequestStatus(enum.Enum):
    pending = "pending"
    submitted = "submitted"
    expired = "expired"


class SupportCsatRequest(Base):
    """Durable CSAT request and response for one support resolution cycle."""

    __tablename__ = "support_csat_requests"
    __table_args__ = (
        UniqueConstraint(
            "source_type",
            "source_id",
            "resolution_cycle_key",
            name="uq_support_csat_resolution_cycle",
        ),
        CheckConstraint(
            "source_type IN ('support_ticket', 'inbox_conversation')",
            name="ck_support_csat_source_type",
        ),
        CheckConstraint(
            "status IN ('pending', 'submitted', 'expired')",
            name="ck_support_csat_status",
        ),
        CheckConstraint(
            "rating IS NULL OR (rating >= 1 AND rating <= 5)",
            name="ck_support_csat_rating",
        ),
        CheckConstraint(
            "(status = 'submitted' AND rating IS NOT NULL AND submitted_at IS NOT NULL) "
            "OR (status <> 'submitted' AND submitted_at IS NULL)",
            name="ck_support_csat_submission_state",
        ),
        Index("ix_support_csat_source", "source_type", "source_id"),
        Index("ix_support_csat_status_resolution", "status", "resolution_at"),
        Index("ix_support_csat_submitted", "submitted_at"),
        Index("ix_support_csat_customer", "customer_id"),
        Index("ix_support_csat_agent_submitted", "agent_person_id", "submitted_at"),
        Index("ix_support_csat_team_submitted", "service_team_id", "submitted_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source_reference: Mapped[str | None] = mapped_column(String(120))
    resolution_cycle_key: Mapped[str] = mapped_column(String(180), nullable=False)

    customer_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    customer_account_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    customer_person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    customer_display_name: Mapped[str | None] = mapped_column(String(180))
    customer_email: Mapped[str | None] = mapped_column(String(255))

    agent_person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    agent_display_name: Mapped[str | None] = mapped_column(String(180))
    service_team_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    service_team_name: Mapped[str | None] = mapped_column(String(180))

    status: Mapped[str] = mapped_column(
        String(24),
        default=CsatRequestStatus.pending.value,
        nullable=False,
    )
    rating: Mapped[int | None] = mapped_column(Integer)
    comment: Mapped[str | None] = mapped_column(Text)
    resolution_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_by: Mapped[str | None] = mapped_column(String(180))
    submission_channel: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )
