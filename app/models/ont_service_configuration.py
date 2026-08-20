"""Assignment-scoped ONT customer-service configuration lifecycle."""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class OntServiceConfigurationPhase(enum.Enum):
    """Delivery meaning for one exact assignment configuration revision."""

    saved = "saved"
    queued = "queued"
    applying = "applying"
    readback_pending = "readback_pending"
    delivered_unverified = "delivered_unverified"
    verified = "verified"
    failed = "failed"
    superseded = "superseded"
    retired = "retired"


class OntServiceConfigurationHead(Base):
    """The current service-configuration revision for one exact assignment."""

    __tablename__ = "ont_service_configuration_heads"
    __table_args__ = (
        UniqueConstraint("assignment_id", name="uq_ont_service_config_head_assignment"),
        Index("ix_ont_service_config_head_ont", "ont_unit_id"),
        Index("ix_ont_service_config_head_latest_operation", "latest_operation_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    ont_unit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ont_units.id", ondelete="RESTRICT"),
        nullable=False,
    )
    assignment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ont_assignments.id", ondelete="RESTRICT"),
        nullable=False,
    )
    current_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    latest_operation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("network_operations.id", ondelete="SET NULL"),
    )
    phase: Mapped[OntServiceConfigurationPhase] = mapped_column(
        Enum(
            OntServiceConfigurationPhase,
            name="ontserviceconfigurationphase",
            native_enum=False,
            create_constraint=True,
        ),
        nullable=False,
        default=OntServiceConfigurationPhase.saved,
        server_default=OntServiceConfigurationPhase.saved.value,
    )
    waiting_reason: Mapped[str | None] = mapped_column(String(160))
    failure_code: Mapped[str | None] = mapped_column(String(160))
    failure_message: Mapped[str | None] = mapped_column(Text)
    last_retry_idempotency_key: Mapped[str | None] = mapped_column(String(160))
    last_retry_operation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("network_operations.id", ondelete="SET NULL"),
    )
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    revisions: Mapped[list[OntServiceConfigurationRevision]] = relationship(
        "OntServiceConfigurationRevision",
        back_populates="head",
        cascade="all, delete-orphan",
        order_by="OntServiceConfigurationRevision.revision",
    )


class OntServiceConfigurationRevision(Base):
    """Immutable admission identity and mutable delivery outcome for a revision."""

    __tablename__ = "ont_service_configuration_revisions"
    __table_args__ = (
        UniqueConstraint("head_id", "revision", name="uq_ont_service_config_revision"),
        UniqueConstraint(
            "head_id", "idempotency_key", name="uq_ont_service_config_idempotency"
        ),
        UniqueConstraint("operation_id", name="uq_ont_service_config_operation"),
        Index("ix_ont_service_config_revision_assignment", "assignment_id"),
        Index("ix_ont_service_config_revision_phase", "phase"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    head_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ont_service_configuration_heads.id", ondelete="CASCADE"),
        nullable=False,
    )
    assignment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ont_assignments.id", ondelete="RESTRICT"),
        nullable=False,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    section: Mapped[str] = mapped_column(String(40), nullable=False)
    command_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    desired_change_evidence: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict
    )
    operation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("network_operations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    phase: Mapped[OntServiceConfigurationPhase] = mapped_column(
        Enum(
            OntServiceConfigurationPhase,
            name="ontserviceconfigurationphase",
            native_enum=False,
            create_constraint=False,
        ),
        nullable=False,
        default=OntServiceConfigurationPhase.saved,
        server_default=OntServiceConfigurationPhase.saved.value,
    )
    waiting_reason: Mapped[str | None] = mapped_column(String(160))
    failure_code: Mapped[str | None] = mapped_column(String(160))
    failure_message: Mapped[str | None] = mapped_column(Text)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    head: Mapped[OntServiceConfigurationHead] = relationship(
        "OntServiceConfigurationHead", back_populates="revisions"
    )
