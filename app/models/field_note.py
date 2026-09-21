import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.db import Base


class FieldWorkOrderNote(Base):
    """Native field note attached to an authoritative work order by FK."""

    __tablename__ = "field_work_order_notes"
    __table_args__ = (
        Index(
            "ix_field_work_order_notes_mirror_created",
            "work_order_mirror_id",
            "created_at",
        ),
        Index("ix_field_work_order_notes_author_technician", "author_technician_id"),
        Index(
            "uq_field_work_order_notes_author_client_ref",
            "author_system_user_id",
            "client_ref",
            unique=True,
            postgresql_where=text("client_ref IS NOT NULL"),
            sqlite_where=text("client_ref IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    work_order_mirror_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("work_order.id", ondelete="CASCADE"),
        nullable=False,
    )
    author_technician_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("technician_profiles.id"), nullable=False
    )
    author_person_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    author_system_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("system_users.id")
    )
    client_ref: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    author_name: Mapped[str | None] = mapped_column(String(160))
    body: Mapped[str] = mapped_column(Text, nullable=False)
    is_internal: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    attachments: Mapped[list | None] = mapped_column(JSON)
    # Provenance for imported notes: CRM-origin notes carry
    # {"source": "crm", "crm_note_id": ..., ...} — crm_note_id doubles as the
    # importer's dedupe marker. Native notes leave this NULL.
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    work_order_mirror = relationship("WorkOrder")
    author_technician = relationship("TechnicianProfile")
    author_system_user = relationship("SystemUser")
    attachments_ = relationship("FieldAttachment", back_populates="note")
