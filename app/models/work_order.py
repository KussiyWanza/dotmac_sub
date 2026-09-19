"""Work-order headers and their field-execution activity.

Sub owns work orders. ``public_id`` is the identity; ``project_id``,
``project_task_id`` and ``origin_ticket_id`` are native relationships; ``crm_work_order_id`` is nullable import
provenance and is NULL for natively created work orders; ``crm_project_id`` is
legacy project provenance and never a native join key. Field activity (worklogs, notes,
attachments, materials, movements, fiber tests, chat, job events) hangs off
``work_order.id`` and has no upstream to rebuild from, so this table is
authoritative storage, not a cache.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, true
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON, DateTime

from app.db import Base


def _default_public_id(context) -> str:
    """Seed public_id from the CRM provenance ref when a row is imported
    (matching migration 328's backfill), else mint a native ``sub-`` id."""
    crm_id = context.get_current_parameters().get("crm_work_order_id")
    return crm_id or f"sub-{uuid.uuid4().hex}"


class WorkOrder(Base):
    """A customer job or a shared infrastructure recovery job."""

    __tablename__ = "work_order"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    public_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
        index=True,
        default=_default_public_id,
    )
    crm_work_order_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True, index=True
    )
    # Customer jobs have a subscriber. Shared outage jobs deliberately do not;
    # their audience is the immutable outage scope revision.
    subscriber_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscribers.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    work_order_kind: Mapped[str] = mapped_column(
        String(20), nullable=False, default="customer", server_default="customer"
    )
    # Native project scope. ``crm_project_id`` below remains import provenance;
    # business policy and joins use this FK.
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    # Optional execution scope. One project task may own many field visits;
    # this root FK is written only by operations.work_order_commands.
    project_task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "project_tasks.id",
            name="fk_work_order_project_task_id_project_tasks",
            ondelete="RESTRICT",
        ),
        nullable=True,
        index=True,
    )
    # Native support provenance. A ticket can explicitly issue many work orders;
    # CRM ticket identifiers remain import provenance in ``crm_ticket_id``.
    origin_ticket_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("support_tickets.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    # The work-order command owner is the sole writer for this policy. Vendor
    # project verification consumes it but never changes it.
    requires_as_built_evidence: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=true(),
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    description: Mapped[str | None] = mapped_column(Text)
    # Values are declared by app.services.field.work_order_status.WorkOrderStatus.
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="scheduled")
    work_type: Mapped[str | None] = mapped_column(String(20))
    priority: Mapped[str | None] = mapped_column(String(20))
    crm_ticket_id: Mapped[str | None] = mapped_column(String(64), index=True)
    crm_project_id: Mapped[str | None] = mapped_column(String(64), index=True)
    assigned_to_crm_person_id: Mapped[str | None] = mapped_column(
        String(64), index=True
    )
    assigned_to_name: Mapped[str | None] = mapped_column(String(160))
    technician_name: Mapped[str | None] = mapped_column(String(160))
    technician_phone: Mapped[str | None] = mapped_column(String(40))
    address: Mapped[str | None] = mapped_column(String(255))
    scheduled_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scheduled_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    estimated_arrival_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    estimated_duration_minutes: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    total_active_seconds: Mapped[int | None] = mapped_column(Integer)
    required_skills: Mapped[list | None] = mapped_column(JSON)
    tags: Mapped[list | None] = mapped_column(JSON)
    access_notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)
    work_order_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    project = relationship("Project", back_populates="work_orders")
    project_task = relationship("ProjectTask", back_populates="work_orders")
    origin_ticket = relationship("Ticket", foreign_keys=[origin_ticket_id])

    @property
    def technician_rating(self) -> int | None:
        """Canonical customer rating projected from typed metadata."""
        metadata = self.metadata_ if isinstance(self.metadata_, dict) else {}
        feedback = metadata.get("technician_rating")
        rating = feedback.get("rating") if isinstance(feedback, dict) else None
        return int(rating) if isinstance(rating, int | float) else None
