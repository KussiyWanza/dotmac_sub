import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, String, Text, UniqueConstraint
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
    __table_args__ = (
        UniqueConstraint(
            "evidence_source",
            "source_id",
            name="uq_subscription_lifecycle_events_source_identity",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscriptions.id", ondelete="RESTRICT"),
        nullable=False,
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
    # A grade alone is not admission. Migration 468 defaulted every insert to
    # ``transition_evidence``, including generic/admin writes. Revision 474
    # closes that hole: only the lifecycle evidence participant supplies a
    # trusted source, identity, effective time, recorded time and fingerprint.
    # Everything else defaults to an unsupported observation.
    evidence_grade: Mapped[str] = mapped_column(
        String(32), nullable=False, default="unsupported_observation"
    )
    evidence_source: Mapped[str] = mapped_column(
        String(40), nullable=False, default="untrusted_observation"
    )
    source_id: Mapped[str | None] = mapped_column(String(160))
    evidence_fingerprint: Mapped[str | None] = mapped_column(String(71))
    effective_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    subscription = relationship("Subscription", back_populates="lifecycle_events")
