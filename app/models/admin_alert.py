import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.network_monitoring import AlertSeverity, AlertStatus


class AdminAlert(Base):
    __tablename__ = "admin_alerts"
    __table_args__ = (
        UniqueConstraint("fingerprint", name="uq_admin_alerts_fingerprint"),
        Index("ix_admin_alerts_category", "category"),
        Index("ix_admin_alerts_category_status", "category", "status"),
        Index("ix_admin_alerts_source", "source"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    category: Mapped[str] = mapped_column(String(40), nullable=False)
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(180), nullable=False)
    severity: Mapped[AlertSeverity] = mapped_column(
        Enum(AlertSeverity), default=AlertSeverity.warning, nullable=False
    )
    status: Mapped[AlertStatus] = mapped_column(
        Enum(AlertStatus), default=AlertStatus.open, nullable=False
    )
    title: Mapped[str] = mapped_column(String(180), nullable=False)
    summary: Mapped[str | None] = mapped_column(String(255))
    details: Mapped[dict | None] = mapped_column(MutableDict.as_mutable(JSON()))
    target_url: Mapped[str | None] = mapped_column(String(255))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
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

    notifications = relationship("AdminNotification", back_populates="alert")


class AdminNotification(Base):
    __tablename__ = "admin_notifications"
    __table_args__ = (
        UniqueConstraint(
            "alert_id",
            "system_user_id",
            name="uq_admin_notifications_alert_user",
        ),
        Index("ix_admin_notifications_user_read", "system_user_id", "read_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    alert_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("admin_alerts.id", ondelete="CASCADE"),
        nullable=True,
    )
    source_notification_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("notifications.id", ondelete="SET NULL"),
        nullable=True,
        unique=True,
    )
    system_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("system_users.id", ondelete="CASCADE"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(180), nullable=False)
    body: Mapped[str | None] = mapped_column(Text)
    target_url: Mapped[str] = mapped_column(String(255), nullable=False)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    alert = relationship("AdminAlert", back_populates="notifications")
    source_notification = relationship("Notification")
    system_user = relationship("SystemUser")
