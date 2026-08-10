import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.db import Base

FIELD_MATERIAL_STATUSES = ("required", "reserved", "used")
FIELD_MATERIAL_REQUEST_STATUSES = (
    "draft",
    "submitted",
    "approved",
    "rejected",
    "issued",
    "fulfilled",
    "canceled",
    "accepted_by_erp",
    "pending_stock",
    "sync_failed",
)
FIELD_MATERIAL_REQUEST_PRIORITIES = ("low", "medium", "high", "urgent")


class FieldInventoryItem(Base):
    """ERP catalogue facts plus Sub-owned field-request eligibility."""

    __tablename__ = "field_inventory_items"
    __table_args__ = (
        Index("ix_field_inventory_items_sku", "sku"),
        Index("ix_field_inventory_items_name", "name"),
        UniqueConstraint(
            "source_system", "source_item_id", name="uq_field_inventory_source_item"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    crm_item_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    source_system: Mapped[str] = mapped_column(
        String(40), default="dotmac_erp", nullable=False
    )
    source_item_id: Mapped[str | None] = mapped_column(String(80))
    sku: Mapped[str | None] = mapped_column(String(80))
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    unit: Mapped[str | None] = mapped_column(String(40))
    category_code: Mapped[str | None] = mapped_column(String(120))
    category_name: Mapped[str | None] = mapped_column(String(160))
    source_is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    field_request_eligible: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    track_serial_numbers: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_payload_hash: Mapped[str | None] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )


class FieldInventoryWarehouse(Base):
    """Rebuildable ERP warehouse catalogue used by material-request forms."""

    __tablename__ = "field_inventory_warehouses"
    __table_args__ = (
        UniqueConstraint(
            "source_system", "source_warehouse_id", name="uq_field_warehouse_source"
        ),
        UniqueConstraint(
            "source_system", "code", name="uq_field_warehouse_source_code"
        ),
        Index("ix_field_inventory_warehouses_name", "name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_system: Mapped[str] = mapped_column(String(40), nullable=False)
    source_warehouse_id: Mapped[str] = mapped_column(String(80), nullable=False)
    code: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    source_is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    source_payload_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )


class FieldWorkOrderMaterial(Base):
    """Allocated material line for a CRM-synced field work-order mirror."""

    __tablename__ = "field_work_order_materials"
    __table_args__ = (
        Index(
            "ix_field_work_order_materials_mirror",
            "work_order_mirror_id",
            "created_at",
        ),
        Index("ix_field_work_order_materials_item", "item_id"),
        Index("ix_field_work_order_materials_crm_material_id", "crm_material_id"),
        Index("ix_field_work_order_materials_status", "status"),
        CheckConstraint(
            "allocated_quantity >= 0",
            name="ck_field_work_order_materials_allocated_nonnegative",
        ),
        CheckConstraint(
            "consumed_quantity >= 0",
            name="ck_field_work_order_materials_consumed_nonnegative",
        ),
        CheckConstraint(
            "consumed_quantity <= allocated_quantity",
            name="ck_field_work_order_materials_consumed_lte_allocated",
        ),
        CheckConstraint(
            "status IN ('required', 'reserved', 'used')",
            name="ck_field_work_order_materials_status",
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
    crm_material_id: Mapped[str | None] = mapped_column(String(64))
    item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("field_inventory_items.id"), nullable=False
    )
    allocated_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    consumed_quantity: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="required", nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    work_order_mirror = relationship("WorkOrder")
    item = relationship("FieldInventoryItem")


class FieldMaterialRequest(Base):
    """Contextual Sub material need; ERP alone owns issuance."""

    __tablename__ = "field_material_requests"
    __table_args__ = (
        Index(
            "ix_field_material_requests_mirror", "work_order_mirror_id", "created_at"
        ),
        Index("ix_field_material_requests_status", "status"),
        Index("ix_field_material_requests_requested_by", "requested_by_technician_id"),
        Index(
            "ix_field_material_requests_support_reference",
            "support_reference",
        ),
        Index("ix_field_material_requests_client_ref", "client_ref", unique=True),
        CheckConstraint(
            "status IN ('draft', 'submitted', 'approved', 'rejected', 'issued', "
            "'fulfilled', 'canceled', 'accepted_by_erp', 'pending_stock', "
            "'sync_failed')",
            name="ck_field_material_requests_status",
        ),
        CheckConstraint(
            "fulfillment_channel IN ('manual', 'erp')",
            name="ck_field_material_requests_fulfillment_channel",
        ),
        CheckConstraint(
            "ticket_id IS NOT NULL OR project_id IS NOT NULL OR "
            "project_task_id IS NOT NULL OR work_order_mirror_id IS NOT NULL",
            name="ck_field_material_requests_has_context",
        ),
        CheckConstraint(
            "priority IN ('low', 'medium', 'high', 'urgent')",
            name="ck_field_material_requests_priority",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    work_order_mirror_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("work_order.id", ondelete="CASCADE"),
        nullable=True,
    )
    ticket_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("support_tickets.id", ondelete="RESTRICT")
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT")
    )
    project_task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_tasks.id", ondelete="RESTRICT")
    )
    crm_material_request_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    requested_by_technician_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("technician_profiles.id"), nullable=True
    )
    requested_by_person_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    requested_by_system_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("system_users.id")
    )
    status: Mapped[str] = mapped_column(String(30), default="draft", nullable=False)
    priority: Mapped[str] = mapped_column(String(20), default="medium", nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    source_warehouse_code: Mapped[str | None] = mapped_column(String(100))
    fulfillment_channel: Mapped[str] = mapped_column(
        String(20), default="manual", nullable=False
    )
    required_by: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)
    # Replaceable back-office support projection. The provider-specific adapter
    # writes these values; the service-workflow owner only interprets the
    # provider-neutral outcome.
    support_system: Mapped[str | None] = mapped_column(String(40))
    support_reference: Mapped[str | None] = mapped_column(String(120))
    support_status: Mapped[str | None] = mapped_column(String(40))
    # Idempotency token for create (mobile retry-safety); mirrors expense.
    client_ref: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_to_erp_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fulfilled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    work_order_mirror = relationship("WorkOrder")
    ticket = relationship("Ticket", foreign_keys=[ticket_id])
    project = relationship("Project", foreign_keys=[project_id])
    project_task = relationship("ProjectTask", foreign_keys=[project_task_id])
    requested_by_technician = relationship("TechnicianProfile")
    requested_by_system_user = relationship("SystemUser")
    items = relationship(
        "FieldMaterialRequestItem",
        back_populates="material_request",
        cascade="all, delete-orphan",
    )


class FieldMaterialRequestItem(Base):
    __tablename__ = "field_material_request_items"
    __table_args__ = (
        Index(
            "ix_field_material_request_items_request",
            "material_request_id",
            "created_at",
        ),
        Index("ix_field_material_request_items_item", "item_id"),
        CheckConstraint(
            "quantity > 0",
            name="ck_field_material_request_items_quantity_positive",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    material_request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("field_material_requests.id", ondelete="CASCADE"),
        nullable=False,
    )
    item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("field_inventory_items.id"), nullable=False
    )
    source_item_id_snapshot: Mapped[str | None] = mapped_column(String(80))
    sku_snapshot: Mapped[str | None] = mapped_column(String(80))
    name_snapshot: Mapped[str | None] = mapped_column(String(160))
    unit_snapshot: Mapped[str | None] = mapped_column(String(40))
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    serial_numbers: Mapped[list | None] = mapped_column(JSON)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    material_request = relationship("FieldMaterialRequest", back_populates="items")
    item = relationship("FieldInventoryItem")
