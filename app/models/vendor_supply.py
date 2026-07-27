"""Materials released to a vendor, and money advanced to one.

Both are project-anchored vendor obligations that the existing models could not
represent:

* ``FieldMaterialRequest`` is work-order scoped and requires a
  ``TechnicianProfile`` requester, so a contractor drawing cable for a buildout
  project had no path to request anything.
* Nothing modelled an advance at all — no mobilisation payment, no retention,
  no drawdown against an approved quote.

Both follow the replaceable back-office boundary
(``docs/BACKOFFICE_INTEGRATION_BOUNDARY.md``): Sub owns the decision and its
evidence, the configured provider owns the stock issue or the money movement,
and Sub stores a provider-neutral reference plus an explicit source-system name
as correlation evidence — never delegated decision authority.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _now() -> datetime:
    return datetime.now(UTC)


class VendorMaterialReleaseStatus(enum.Enum):
    draft = "draft"
    requested = "requested"
    approved = "approved"
    rejected = "rejected"
    issued = "issued"
    canceled = "canceled"


class VendorAdvanceStatus(enum.Enum):
    requested = "requested"
    approved = "approved"
    rejected = "rejected"
    # ``settled`` is an observation of the payables provider, never a Sub
    # decision: Sub records that it asked, the provider records that it paid.
    settled = "settled"
    canceled = "canceled"


class VendorMaterialRelease(Base):
    """One request to release Dotmac-owned material to a vendor for a project."""

    __tablename__ = "vendor_material_releases"
    __table_args__ = (
        Index("ix_vendor_material_releases_project", "project_id", "status"),
        Index("ix_vendor_material_releases_vendor", "vendor_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("installation_projects.id", ondelete="RESTRICT"),
        nullable=False,
    )
    vendor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("vendors.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        default=VendorMaterialReleaseStatus.draft.value,
        nullable=False,
    )
    requested_by_person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_by_person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_notes: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)

    # Replaceable back-office projection. The configured provider owns the stock
    # issue; these three columns are correlation evidence with an explicit
    # source system, never delegated authority.
    support_system: Mapped[str | None] = mapped_column(String(40))
    support_reference: Mapped[str | None] = mapped_column(String(120))
    support_status: Mapped[str | None] = mapped_column(String(40))
    support_observed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    project = relationship("InstallationProject")
    vendor = relationship("Vendor")
    items = relationship(
        "VendorMaterialReleaseItem",
        back_populates="release",
        cascade="all, delete-orphan",
    )


class VendorMaterialReleaseItem(Base):
    __tablename__ = "vendor_material_release_items"
    __table_args__ = (
        CheckConstraint(
            "quantity > 0", name="ck_vendor_material_release_items_quantity_positive"
        ),
        Index("ix_vendor_material_release_items_release", "release_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    release_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("vendor_material_releases.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Provider-neutral item identity: Sub does not hold the stock catalogue.
    item_code: Mapped[str | None] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    unit: Mapped[str | None] = mapped_column(String(40))
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    issued_quantity: Mapped[int | None] = mapped_column(Integer)
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )

    release = relationship("VendorMaterialRelease", back_populates="items")


class VendorAdvance(Base):
    """One advance of money to a vendor against approved project work.

    The advance draws against the approved quote, which is what makes it
    bounded: Sub refuses to advance more than the work is worth. Payment and
    any netting against the vendor's later invoice belong to the payables
    provider — Sub records the decision and observes the outcome.
    """

    __tablename__ = "vendor_advances"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_vendor_advances_amount_positive"),
        Index("ix_vendor_advances_project", "project_id", "status"),
        Index("ix_vendor_advances_vendor", "vendor_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("installation_projects.id", ondelete="RESTRICT"),
        nullable=False,
    )
    vendor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("vendors.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # The approved quote the advance draws against — the ceiling that bounds it.
    quote_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("project_quotes.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(20), default=VendorAdvanceStatus.requested.value, nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)

    requested_by_person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_by_person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_notes: Mapped[str | None] = mapped_column(Text)

    # Payables provider observation. Sub never computes settlement or netting.
    payables_system: Mapped[str | None] = mapped_column(String(40))
    payables_reference: Mapped[str | None] = mapped_column(String(120))
    payables_status: Mapped[str | None] = mapped_column(String(40))
    payables_observed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    project = relationship("InstallationProject")
    vendor = relationship("Vendor")
    quote = relationship("ProjectQuote")
