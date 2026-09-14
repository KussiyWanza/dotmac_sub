import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    # Note: settings imported inline below to avoid circular imports
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.types import EncryptedJSON


class Tr069Event(enum.Enum):
    boot = "boot"
    bootstrap = "bootstrap"
    periodic = "periodic"
    value_change = "value_change"
    connection_request = "connection_request"
    transfer_complete = "transfer_complete"
    diagnostics_complete = "diagnostics_complete"


class Tr069JobStatus(enum.Enum):
    queued = "queued"
    running = "running"
    pending = "pending"
    succeeded = "succeeded"
    failed = "failed"
    unverified = "unverified"
    canceled = "canceled"


class Tr069AcsServer(Base):
    __tablename__ = "tr069_acs_servers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    cwmp_url: Mapped[str | None] = mapped_column(String(255))
    cwmp_username: Mapped[str | None] = mapped_column(String(120))
    cwmp_password: Mapped[str | None] = mapped_column(String(512))
    connection_request_username: Mapped[str | None] = mapped_column(String(120))
    connection_request_password: Mapped[str | None] = mapped_column(String(512))
    base_url: Mapped[str] = mapped_column(String(255), nullable=False)
    periodic_inform_interval: Mapped[int] = mapped_column(
        Integer,
        default=lambda: (
            __import__(
                "app.config", fromlist=["settings"]
            ).settings.tr069_periodic_inform_interval
        ),
        nullable=False,
    )  # seconds, default from settings.tr069_periodic_inform_interval
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    devices = relationship("Tr069CpeDevice", back_populates="acs_server")


class Tr069CpeDevice(Base):
    __tablename__ = "tr069_cpe_devices"
    __table_args__ = (
        Index(
            "uq_tr069_cpe_devices_active_ont_unit_id",
            "ont_unit_id",
            unique=True,
            postgresql_where=text("is_active AND ont_unit_id IS NOT NULL"),
        ),
        Index(
            "uq_tr069_cpe_devices_active_genieacs_device_id",
            "genieacs_device_id",
            unique=True,
            postgresql_where=text("is_active AND genieacs_device_id IS NOT NULL"),
        ),
        Index(
            "uq_tr069_cpe_devices_active_cpe_device_id",
            "cpe_device_id",
            unique=True,
            postgresql_where=text("is_active AND cpe_device_id IS NOT NULL"),
            sqlite_where=text("is_active AND cpe_device_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    acs_server_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tr069_acs_servers.id"), nullable=False
    )
    ont_unit_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ont_units.id")
    )
    cpe_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cpe_devices.id")
    )
    serial_number: Mapped[str | None] = mapped_column(String(120))
    oui: Mapped[str | None] = mapped_column(String(8))
    product_class: Mapped[str | None] = mapped_column(String(120))
    genieacs_device_id: Mapped[str | None] = mapped_column(String(255), index=True)
    connection_request_url: Mapped[str | None] = mapped_column(String(255))
    last_inform_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    acs_server = relationship("Tr069AcsServer", back_populates="devices")
    ont_unit = relationship("OntUnit")
    cpe_device = relationship("CPEDevice")
    sessions = relationship("Tr069Session", back_populates="device")
    parameters = relationship("Tr069Parameter", back_populates="device")
    jobs = relationship("Tr069Job", back_populates="device")


class Tr069Session(Base):
    __tablename__ = "tr069_sessions"
    __table_args__ = (
        Index("ix_tr069_sessions_device_started_at", "device_id", "started_at"),
        Index("ix_tr069_sessions_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tr069_cpe_devices.id"), nullable=False
    )
    event_type: Mapped[Tr069Event] = mapped_column(Enum(Tr069Event), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(120))
    inform_payload: Mapped[dict | None] = mapped_column(JSON)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    device = relationship("Tr069CpeDevice", back_populates="sessions")


class Tr069Parameter(Base):
    __tablename__ = "tr069_parameters"
    __table_args__ = (
        UniqueConstraint("device_id", "name", name="uq_tr069_parameters_device_name"),
        Index("ix_tr069_parameters_device_updated_at", "device_id", "updated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tr069_cpe_devices.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    value: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    device = relationship("Tr069CpeDevice", back_populates="parameters")


class Tr069Job(Base):
    __tablename__ = "tr069_jobs"
    __table_args__ = (
        UniqueConstraint(
            "network_operation_id",
            name="uq_tr069_jobs_network_operation_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tr069_cpe_devices.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    command: Mapped[str] = mapped_column(String(160), nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSON)
    secure_payload: Mapped[dict | None] = mapped_column(EncryptedJSON)
    network_operation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("network_operations.id", ondelete="RESTRICT"),
        nullable=True,
    )
    external_task_ids: Mapped[list[str] | None] = mapped_column(JSON)
    status: Mapped[Tr069JobStatus] = mapped_column(
        Enum(Tr069JobStatus), default=Tr069JobStatus.queued
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    device = relationship("Tr069CpeDevice", back_populates="jobs")
    network_operation = relationship("NetworkOperation")
