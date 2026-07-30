"""Temporary, assignment-free ONT commissioning intent."""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class OntCommissioningState(enum.Enum):
    """Lifecycle owned by ``network.ont_commissioning``."""

    commissioning = "commissioning"
    authorizing = "authorizing"
    awaiting_acs = "awaiting_acs"
    management_ready = "management_ready"
    assigned = "assigned"
    provisioned = "provisioned"
    failed = "failed"
    cleanup_pending = "cleanup_pending"
    cleanup_running = "cleanup_running"
    expired = "expired"
    canceled = "canceled"


_ACTIVE_INTENT_STATES = (
    "'commissioning', 'authorizing', 'awaiting_acs', 'management_ready', "
    "'failed', 'cleanup_pending', 'cleanup_running'"
)


class OntCommissioningIntent(Base):
    """Exact, expiring authorization for management-only ONT bootstrap.

    This record is deliberately not an ``OntAssignment``. It never identifies a
    subscriber, subscription, customer internet service, PPPoE credential, or
    Wi-Fi intent.
    """

    __tablename__ = "ont_commissioning_intents"
    __table_args__ = (
        CheckConstraint(
            "length(trim(reason)) > 0",
            name="ck_ont_commissioning_reason_required",
        ),
        CheckConstraint(
            "expires_at > created_at",
            name="ck_ont_commissioning_expiry_after_creation",
        ),
        Index(
            "ix_ont_commissioning_state_expires",
            "state",
            "expires_at",
        ),
        Index(
            "uq_ont_commissioning_active_serial",
            "canonical_serial",
            unique=True,
            postgresql_where=text(f"state IN ({_ACTIVE_INTENT_STATES})"),
            sqlite_where=text(f"state IN ({_ACTIVE_INTENT_STATES})"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    autofind_candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("olt_autofind_candidates.id", ondelete="SET NULL"),
        nullable=True,
    )
    ont_unit_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ont_units.id", ondelete="SET NULL"),
        nullable=True,
    )
    olt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("olt_devices.id", ondelete="RESTRICT"),
        nullable=False,
    )
    latest_operation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("network_operations.id", ondelete="RESTRICT"),
        nullable=True,
    )
    cleanup_operation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("network_operations.id", ondelete="RESTRICT"),
        nullable=True,
    )
    canonical_serial: Mapped[str] = mapped_column(String(120), nullable=False)
    fsp: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[OntCommissioningState] = mapped_column(
        Enum(
            OntCommissioningState,
            name="ontcommissioningstate",
            native_enum=False,
            validate_strings=True,
        ),
        nullable=False,
        default=OntCommissioningState.commissioning,
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    reference: Mapped[str | None] = mapped_column(String(160))
    requested_by: Mapped[str] = mapped_column(String(160), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    device_authorized_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    management_ready_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provisioned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cleanup_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cleanup_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String(120))
    failure_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    autofind_candidate = relationship("OltAutofindCandidate")
    ont_unit = relationship("OntUnit", foreign_keys=[ont_unit_id])
    olt = relationship("OLTDevice")
    latest_operation = relationship(
        "NetworkOperation", foreign_keys=[latest_operation_id]
    )
    cleanup_operation = relationship(
        "NetworkOperation", foreign_keys=[cleanup_operation_id]
    )
