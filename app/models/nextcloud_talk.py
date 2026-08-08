"""Staff identity and direct-room projections for Nextcloud Talk delivery."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class NextcloudTalkStaffAccount(Base):
    __tablename__ = "nextcloud_talk_staff_accounts"
    __table_args__ = (
        UniqueConstraint(
            "system_user_id",
            "integration_installation_id",
            name="uq_nextcloud_talk_staff_account_user_installation",
        ),
        UniqueConstraint(
            "integration_installation_id",
            "nextcloud_username_normalized",
            name="uq_nextcloud_talk_staff_account_installation_username",
        ),
        Index(
            "ix_nextcloud_talk_staff_accounts_installation_active",
            "integration_installation_id",
            "is_active",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    system_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("system_users.id", ondelete="CASCADE"),
        nullable=False,
    )
    integration_installation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("integration_installations.id", ondelete="CASCADE"),
        nullable=False,
    )
    nextcloud_username: Mapped[str] = mapped_column(String(255), nullable=False)
    nextcloud_username_normalized: Mapped[str] = mapped_column(
        String(255), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[str | None] = mapped_column(String(160))
    updated_by: Mapped[str | None] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    system_user = relationship("SystemUser")
    integration_installation = relationship("IntegrationInstallation")


class NextcloudTalkNotificationRoom(Base):
    __tablename__ = "nextcloud_talk_notification_rooms"
    __table_args__ = (
        UniqueConstraint(
            "system_user_id",
            "integration_installation_id",
            name="uq_nextcloud_talk_room_user_installation",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    system_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("system_users.id", ondelete="CASCADE"),
        nullable=False,
    )
    integration_installation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("integration_installations.id", ondelete="CASCADE"),
        nullable=False,
    )
    invite_target: Mapped[str] = mapped_column(String(255), nullable=False)
    room_token: Mapped[str] = mapped_column(String(255), nullable=False)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failure_code: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    system_user = relationship("SystemUser")
    integration_installation = relationship("IntegrationInstallation")
