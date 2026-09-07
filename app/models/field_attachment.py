import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

FIELD_ATTACHMENT_KINDS = ("photo", "signature", "document")


class FieldAttachment(Base):
    """Private field evidence attached to an authoritative work order."""

    __tablename__ = "field_attachments"
    __table_args__ = (
        Index(
            "ix_field_attachments_mirror_created",
            "work_order_mirror_id",
            "created_at",
        ),
        Index("ix_field_attachments_note_id", "note_id"),
        Index("ix_field_attachments_client_ref", "client_ref", unique=True),
        Index("ix_field_attachments_asset", "asset_type", "asset_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    work_order_mirror_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("work_order.id", ondelete="CASCADE"),
        nullable=False,
    )
    note_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("field_work_order_notes.id", ondelete="SET NULL")
    )
    stored_file_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("stored_files.id"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(30), default="photo", nullable=False)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    signer_name: Mapped[str | None] = mapped_column(String(160))
    uploaded_by_technician_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("technician_profiles.id"), nullable=True
    )
    uploaded_by_person_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    uploaded_by_system_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("system_users.id")
    )
    client_ref: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    asset_type: Mapped[str | None] = mapped_column(String(60))
    asset_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    work_order_mirror = relationship("WorkOrder")
    note = relationship("FieldWorkOrderNote", back_populates="attachments_")
    stored_file = relationship("StoredFile")
    uploaded_by_technician = relationship("TechnicianProfile")
    uploaded_by_system_user = relationship("SystemUser")
