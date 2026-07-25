"""Service extensions: bulk validity compensation for outages.

An extension records an outage window and a scope (whole network, POP site,
NAS device, or an explicit subscriber list). When applied, it records an exact
service grant from the later of the existing billing anchor and application
time, then projects ``next_billing_at`` to the grant end. Capped plans keep
their calendar-month allowance — this extends validity, never data.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class ServiceExtensionScope(enum.Enum):
    network = "network"
    pop_site = "pop_site"
    nas_device = "nas_device"
    subscribers = "subscribers"


class ServiceExtensionStatus(enum.Enum):
    pending = "pending"
    applied = "applied"
    canceled = "canceled"


class ServiceExtensionAnchorBasis(enum.Enum):
    """Why an extension grant starts at its recorded boundary."""

    existing_billing_anchor = "existing_billing_anchor"
    application_time = "application_time"
    legacy_previous_anchor = "legacy_previous_anchor"


class ServiceExtension(Base):
    __tablename__ = "service_extensions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    window_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    window_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    days: Mapped[int] = mapped_column(Integer, nullable=False)
    scope_type: Mapped[ServiceExtensionScope] = mapped_column(
        Enum(ServiceExtensionScope), nullable=False
    )
    scope_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # Explicit subscriber id list for scope_type=subscribers.
    scope_subscriber_ids: Mapped[list[str] | None] = mapped_column(JSON)
    status: Mapped[ServiceExtensionStatus] = mapped_column(
        Enum(ServiceExtensionStatus),
        default=ServiceExtensionStatus.pending,
        nullable=False,
    )
    affected_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    resumed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    still_suspended_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    created_by: Mapped[str | None] = mapped_column(String(64))
    applied_by: Mapped[str | None] = mapped_column(String(64))
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    canceled_by: Mapped[str | None] = mapped_column(String(64))
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    create_idempotency_key_sha256: Mapped[str | None] = mapped_column(String(64))
    create_fingerprint_sha256: Mapped[str | None] = mapped_column(String(64))
    create_command_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    create_correlation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    apply_idempotency_key_sha256: Mapped[str | None] = mapped_column(String(64))
    apply_command_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    apply_correlation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    cancel_idempotency_key_sha256: Mapped[str | None] = mapped_column(String(64))
    cancel_command_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    cancel_correlation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    entries = relationship(
        "ServiceExtensionEntry", back_populates="extension", lazy="noload"
    )


class ServiceExtensionEntry(Base):
    __tablename__ = "service_extension_entries"
    __table_args__ = (
        UniqueConstraint(
            "extension_id",
            "subscription_id",
            name="uq_service_extension_entries_extension_subscription",
        ),
        Index("ix_service_extension_entries_extension", "extension_id"),
        Index("ix_service_extension_entries_subscription", "subscription_id"),
        # Dunning-shield lookup path (bulk_extension_shield_reasons).
        Index(
            "ix_service_extension_entries_subscriber_created",
            "subscriber_id",
            "created_at",
        ),
        Index(
            "ix_service_extension_entries_subscriber_grant_end",
            "subscriber_id",
            "grant_ends_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    extension_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_extensions.id"), nullable=False
    )
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscriptions.id"), nullable=False
    )
    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=False
    )
    previous_next_billing_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    grant_starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    grant_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    anchor_basis: Mapped[ServiceExtensionAnchorBasis | None] = mapped_column(
        Enum(ServiceExtensionAnchorBasis, native_enum=False, length=40)
    )
    new_next_billing_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    extension = relationship("ServiceExtension", back_populates="entries")
