import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.db import Base

FIELD_EXPENSE_STATUSES = (
    "draft",
    "submitted",
    "approved",
    "rejected",
    "paid",
    "canceled",
)


class FieldExpenseRequest(Base):
    """Staff expense request attached to an authoritative work order."""

    __tablename__ = "field_expense_requests"
    __table_args__ = (
        Index("ix_field_expense_requests_mirror", "work_order_mirror_id", "created_at"),
        Index("ix_field_expense_requests_requested_by", "requested_by_technician_id"),
        Index("ix_field_expense_requests_status", "status"),
        Index("ix_field_expense_requests_client_ref", "client_ref", unique=True),
        CheckConstraint(
            "status IN ('draft', 'submitted', 'approved', 'rejected', 'paid', 'canceled')",
            name="ck_field_expense_requests_status",
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
    crm_expense_request_id: Mapped[str | None] = mapped_column(String(64), unique=True)
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
    purpose: Mapped[str] = mapped_column(String(500), nullable=False)
    expense_date: Mapped[date | None] = mapped_column(Date)
    currency: Mapped[str] = mapped_column(String(3), default="NGN", nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    rejection_reason: Mapped[str | None] = mapped_column(String(500))
    expense_system: Mapped[str | None] = mapped_column(String(40))
    expense_claim_reference: Mapped[str | None] = mapped_column(String(120))
    expense_claim_number: Mapped[str | None] = mapped_column(String(60))
    expense_claim_status: Mapped[str | None] = mapped_column(String(40))
    client_ref: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
    requested_by_technician = relationship("TechnicianProfile")
    requested_by_system_user = relationship("SystemUser")
    items = relationship(
        "FieldExpenseRequestItem",
        back_populates="expense_request",
        cascade="all, delete-orphan",
    )

    @property
    def total_amount(self) -> Decimal:
        return sum((item.amount for item in self.items), Decimal("0"))


class FieldExpenseRequestItem(Base):
    __tablename__ = "field_expense_request_items"
    __table_args__ = (
        Index(
            "ix_field_expense_request_items_request",
            "expense_request_id",
            "created_at",
        ),
        Index("ix_field_expense_request_items_category", "category_code"),
        Index(
            "ix_field_expense_request_items_receipt_attachment", "receipt_attachment_id"
        ),
        CheckConstraint(
            "amount > 0",
            name="ck_field_expense_request_items_amount_positive",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    expense_request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("field_expense_requests.id", ondelete="CASCADE"),
        nullable=False,
    )
    category_code: Mapped[str] = mapped_column(String(30), nullable=False)
    category_name: Mapped[str | None] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    expense_date: Mapped[date | None] = mapped_column(Date)
    vendor_name: Mapped[str | None] = mapped_column(String(200))
    receipt_url: Mapped[str | None] = mapped_column(String(500))
    receipt_attachment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("field_attachments.id", ondelete="SET NULL")
    )
    notes: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    expense_request = relationship("FieldExpenseRequest", back_populates="items")
    receipt_attachment = relationship("FieldAttachment")
