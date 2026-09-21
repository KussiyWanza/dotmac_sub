import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

FIELD_PRESENCE_STATUSES = ("off_shift", "on_shift", "break", "busy")


class FieldTechPresence(Base):
    __tablename__ = "field_tech_presence"
    __table_args__ = (
        Index("ix_field_tech_presence_technician_id", "technician_id", unique=True),
        Index("ix_field_tech_presence_person_id", "person_id"),
        Index("ix_field_tech_presence_status", "status"),
        Index("ix_field_tech_presence_last_location_at", "last_location_at"),
        CheckConstraint(
            "status IN ('off_shift', 'on_shift', 'break', 'busy')",
            name="ck_field_tech_presence_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    technician_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("technician_profiles.id"), nullable=False
    )
    person_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="off_shift", nullable=False)
    location_sharing_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    last_latitude: Mapped[float | None] = mapped_column(Float)
    last_longitude: Mapped[float | None] = mapped_column(Float)
    last_location_accuracy_m: Mapped[float | None] = mapped_column(Float)
    last_location_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    technician = relationship("TechnicianProfile")


class FieldTechLocationPing(Base):
    __tablename__ = "field_tech_location_pings"
    __table_args__ = (
        Index(
            "ix_field_tech_location_pings_technician_received",
            "technician_id",
            "received_at",
        ),
        Index(
            "ix_field_tech_location_pings_person_received", "person_id", "received_at"
        ),
        Index("ix_field_tech_location_pings_received_at", "received_at"),
        Index("ix_field_tech_location_pings_crm_work_order_id", "crm_work_order_id"),
        # Scoped, not global, uniqueness. FieldJobEvent.client_event_id (the
        # precedent this mirrors) uses a bare global-unique index on the
        # client-supplied id alone, which lets one client's key collide with
        # a DIFFERENT technician's row and wrongly deny that unrelated
        # technician's ping. A ping only needs replay-safety within the
        # technician that sent it, so this is deliberately a composite
        # (technician_id, client_observation_id) unique index instead of a
        # bare global unique constraint — a documented deviation from the
        # FieldJobEvent shape, not an oversight; that precedent's own
        # global-unique index carries this same latent weakness. The column
        # is nullable, and Postgres unique indexes never treat two NULLs as
        # equal, so pings from app builds that predate this field keep
        # today's no-dedup behavior unchanged.
        Index(
            "ix_field_tech_location_pings_technician_client_observation",
            "technician_id",
            "client_observation_id",
            unique=True,
        ),
        CheckConstraint(
            "latitude >= -90 AND latitude <= 90",
            name="ck_field_tech_location_pings_lat_range",
        ),
        CheckConstraint(
            "longitude >= -180 AND longitude <= 180",
            name="ck_field_tech_location_pings_lng_range",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    technician_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("technician_profiles.id"), nullable=False
    )
    person_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    crm_work_order_id: Mapped[str | None] = mapped_column(String(64))
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    accuracy_m: Mapped[float | None] = mapped_column(Float)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    source: Mapped[str] = mapped_column(String(32), default="mobile", nullable=False)
    # Nullable: a stable client-supplied identifier that lets a mobile retry
    # after an ambiguous network failure (timeout after the server actually
    # committed) be recognized as a replay instead of a new row. Old/already-
    # shipped app builds omit it, and that path is unaffected — see the
    # composite unique index above and record_ping's dedup check.
    client_observation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    technician = relationship("TechnicianProfile")
