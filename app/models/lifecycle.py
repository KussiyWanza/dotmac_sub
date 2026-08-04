import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.catalog import SubscriptionStatus


class LifecycleEventType(enum.Enum):
    activate = "activate"
    suspend = "suspend"
    resume = "resume"
    cancel = "cancel"
    upgrade = "upgrade"
    downgrade = "downgrade"
    change_address = "change_address"
    other = "other"


class SubscriptionLifecycleEvent(Base):
    __tablename__ = "subscription_lifecycle_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscriptions.id"), nullable=False
    )
    event_type: Mapped[LifecycleEventType] = mapped_column(
        Enum(LifecycleEventType), default=LifecycleEventType.other
    )
    from_status: Mapped[SubscriptionStatus | None] = mapped_column(
        Enum(SubscriptionStatus)
    )
    to_status: Mapped[SubscriptionStatus | None] = mapped_column(
        Enum(SubscriptionStatus)
    )
    reason: Mapped[str | None] = mapped_column(String(200))
    notes: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)
    actor: Mapped[str | None] = mapped_column(String(120))
    # What this row can be trusted for. Rows written before migration 468 were
    # mutable for their whole life, so they are graded
    # ``unsupported_pre_cutover`` and a contractual score resting on them is
    # reported incomplete rather than silently computed. A database trigger
    # rejects UPDATE and DELETE from 468 onward; this table is append-only.
    evidence_grade: Mapped[str] = mapped_column(
        String(32), nullable=False, default="transition_evidence"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    subscription = relationship("Subscription", back_populates="lifecycle_events")
