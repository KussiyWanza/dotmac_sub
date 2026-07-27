"""Durable ERP billing export evidence (ADR 0007 Phase 7, section 11).

Sub owns the operational source facts; Dotmac ERP owns the general ledger,
account mappings, journals, and statements. This table records the transport
between them: one versioned, idempotent payload per committed Sub owner
output, with durable delivery and acknowledgement evidence.

ERP downtime does not roll back Sub cash, documents, entitlement, or access —
an undelivered export simply stays pending. A rejection is durable reviewable
evidence, never a silent drop, and missing or ambiguous mappings fail closed
in ERP rather than being guessed here.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    DateTime,
    Enum,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ErpBillingFlow(enum.Enum):
    """Money-path flows Sub may project to ERP."""

    invoice = "invoice"
    credit_note = "credit_note"
    payment = "payment"
    refund_or_reversal = "refund_or_reversal"
    tax_withholding = "tax_withholding"
    correction = "correction"


class ErpExportStatus(enum.Enum):
    """Delivery lifecycle of one export row."""

    pending = "pending"
    delivered = "delivered"
    acknowledged = "acknowledged"
    rejected = "rejected"


class ErpBillingExport(Base):
    """One idempotent versioned billing payload bound for Dotmac ERP."""

    __tablename__ = "erp_billing_exports"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_erp_billing_export_idempotency"),
        Index("ix_erp_billing_export_status", "status"),
        Index("ix_erp_billing_export_source", "source_kind", "source_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    flow: Mapped[ErpBillingFlow] = mapped_column(
        Enum(ErpBillingFlow, name="erpbillingflow"), nullable=False
    )
    source_kind: Mapped[str] = mapped_column(String(80), nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source_event_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    payload_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)

    status: Mapped[ErpExportStatus] = mapped_column(
        Enum(ErpExportStatus, name="erpexportstatus"),
        nullable=False,
        default=ErpExportStatus.pending,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # ERP's own identity for the accepted document, recorded structurally.
    erp_reference: Mapped[str | None] = mapped_column(String(160))

    command_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    correlation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
