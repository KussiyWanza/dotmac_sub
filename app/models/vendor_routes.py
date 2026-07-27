"""Native vendor route domain ported from the CRM maps capability.

Ports ``dotmac_crm/app/models/vendor.py`` — the vendor installation-project /
quote / as-built domain that carries the fiber ``route_geom`` map geometry —
natively into Sub, so the temporary vendor project-stub relay can be retired once
the tables are backfilled.

Sub conventions applied (same house idiom as ``app/models/project.py``):

* **CRM UUID PKs are kept verbatim** (the backfill upserts ON CONFLICT on the
  CRM id).
* CRM PG enums become **String columns + app-level enums** (exact CRM
  vocabularies preserved).
* ``installation_projects.project_id`` is a **real FK to sub's now-native
  ``projects.id``**. ``buildout_project_id`` → ``buildout_projects.id``
  and the route ``fiber_segment_id`` columns → ``fiber_segments.id`` are real
  FKs too (all native in sub).
* **Customer-party** columns re-point at sub ``subscribers.id``
  (``installation_projects.subscriber_id``).
* **Staff / CRM-person** columns (``created_by_person_id``,
  ``reviewed_by_person_id``, ``submitted_by_person_id``, note authors, and
  ``vendor_users.person_id``) become plain UUIDs — sub has no ``people`` table
  (§ FK-clash rule). ``installation_projects.address_id`` stays a plain UUID
  (CRM already dropped its FK; CRM address UUIDs have no sub correspondence).
* ``route_geom`` uses ``geoalchemy2.Geometry('LINESTRING', srid=4326)`` exactly
  like ``network.py``'s ``FiberSegment``.

Reconciliation with the pre-existing vendor-authentication support
(``app/models/field_vendor.py``): those ``field_vendors`` / ``field_vendor_users``
/ ``field_vendor_device_tokens`` tables are the vendor **auth mirror** (mobile
login + device tokens, keyed to ``system_users`` with a ``crm_vendor_id`` string
pointer). They remain a separate auth projection, but both profiles now have
an additive canonical Party link and must converge on the same Party. The native
``vendors`` / ``vendor_users`` tables created here are the CRM-UUID-keyed
**domain of record** that the route/installation-project tables reference; the
bridge is ``field_vendors.crm_vendor_id == vendors.id``. The genuinely-new part
of this port is the installation-project / quote / route surface.

Not ported (out of scope for maps §A — not in the port list): CRM's
``VendorPurchaseInvoice`` / ``VendorPurchaseInvoiceLineItem`` and the
``VendorPurchaseInvoiceStatus`` enum.

The CRM ``quote_line_items`` table clashes with sub's existing sales
``quote_line_items`` (``app/models/sales.py``); the vendor line-item table is
renamed to ``project_quote_line_items`` (class ``ProjectQuoteLineItem``) to
avoid the collision — same house pattern as project.py's ``TaskStatus`` rename.
"""

import enum
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from geoalchemy2 import Geometry
from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Enums — String columns + app enums, exact CRM vocabularies
# ---------------------------------------------------------------------------


class VendorAssignmentType(enum.Enum):
    bidding = "bidding"
    direct = "direct"


class InstallationProjectStatus(enum.Enum):
    draft = "draft"
    open_for_bidding = "open_for_bidding"
    quoted = "quoted"
    approved = "approved"
    in_progress = "in_progress"
    completed = "completed"
    verified = "verified"
    assigned = "assigned"


class ProjectQuoteStatus(enum.Enum):
    draft = "draft"
    submitted = "submitted"
    under_review = "under_review"
    approved = "approved"
    rejected = "rejected"
    revision_requested = "revision_requested"


class VendorPurchaseInvoiceStatus(enum.Enum):
    draft = "draft"
    submitted = "submitted"
    under_review = "under_review"
    approved = "approved"
    rejected = "rejected"
    revision_requested = "revision_requested"


class ProposedRouteRevisionStatus(enum.Enum):
    draft = "draft"
    submitted = "submitted"
    accepted = "accepted"
    rejected = "rejected"


class VariationType(enum.Enum):
    scope_change = "scope_change"
    route_deviation = "route_deviation"
    material_change = "material_change"
    additional_work = "additional_work"
    reduction = "reduction"


class AsBuiltRouteStatus(enum.Enum):
    submitted = "submitted"
    under_review = "under_review"
    accepted = "accepted"
    rejected = "rejected"


# ---------------------------------------------------------------------------
# Vendor identity (CRM-UUID-keyed domain of record)
# ---------------------------------------------------------------------------


class Vendor(Base):
    __tablename__ = "vendors"
    __table_args__ = (
        CheckConstraint(
            "(party_id IS NULL AND party_bound_at IS NULL AND "
            "party_binding_source IS NULL AND party_binding_reason IS NULL) OR "
            "(party_id IS NOT NULL AND party_bound_at IS NOT NULL AND "
            "party_binding_source IS NOT NULL AND "
            "party_binding_reason IS NOT NULL AND "
            "length(trim(party_binding_source)) > 0 AND "
            "length(trim(party_binding_reason)) > 0)",
            name="ck_vendors_party_binding_evidence",
        ),
        UniqueConstraint("party_id", name="uq_vendors_party_id"),
        UniqueConstraint(
            "supplier_system",
            "supplier_reference",
            name="uq_vendors_supplier_system_reference",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    party_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("parties.id", ondelete="RESTRICT")
    )
    party_bound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    party_binding_source: Mapped[str | None] = mapped_column(String(80))
    party_binding_reason: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    code: Mapped[str | None] = mapped_column(String(60), unique=True)
    contact_name: Mapped[str | None] = mapped_column(String(160))
    contact_email: Mapped[str | None] = mapped_column(String(255))
    contact_phone: Mapped[str | None] = mapped_column(String(40))
    license_number: Mapped[str | None] = mapped_column(String(120))
    service_area: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)
    supplier_system: Mapped[str | None] = mapped_column(String(40))
    supplier_reference: Mapped[str | None] = mapped_column(String(100))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    users = relationship("VendorUser", back_populates="vendor")
    party = relationship("Party", back_populates="vendor_profile")
    quotes = relationship("ProjectQuote", back_populates="vendor")
    purchase_invoices = relationship("VendorPurchaseInvoice", back_populates="vendor")


class VendorUser(Base):
    __tablename__ = "vendor_users"
    __table_args__ = (
        UniqueConstraint(
            "vendor_id", "person_id", name="uq_vendor_users_vendor_person"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    vendor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vendors.id"), nullable=False
    )
    # CRM person UUID — no ``people`` table in sub, carried as a plain UUID.
    person_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    role: Mapped[str | None] = mapped_column(String(60))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    vendor = relationship("Vendor", back_populates="users")


# ---------------------------------------------------------------------------
# Installation projects + quotes
# ---------------------------------------------------------------------------


class InstallationProject(Base):
    __tablename__ = "installation_projects"
    __table_args__ = (
        UniqueConstraint("project_id", name="uq_installation_projects_project"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Real FK to Sub's native projects table.
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False
    )
    buildout_project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("buildout_projects.id")
    )
    # Customer party → sub subscriber.
    subscriber_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), index=True
    )
    # CRM address UUID — no sub correspondence, plain UUID (CRM dropped its FK).
    address_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    assigned_vendor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vendors.id"), index=True
    )
    assignment_type: Mapped[str | None] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(
        String(40), default=InstallationProjectStatus.draft.value, nullable=False
    )
    bidding_open_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bidding_close_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    procurement_system: Mapped[str | None] = mapped_column(String(40))
    procurement_order_reference: Mapped[str | None] = mapped_column(
        String(100), index=True
    )
    procurement_delivery_status: Mapped[str | None] = mapped_column(String(40))
    procurement_delivery_error: Mapped[str | None] = mapped_column(String(500))
    procurement_delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    approved_quote_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        # Mutual FK with project_quotes.project_id — break the create_all cycle.
        ForeignKey(
            "project_quotes.id",
            use_alter=True,
            name="fk_installation_projects_approved_quote",
        ),
    )
    # Staff person — no FK, plain UUID.
    created_by_person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    project = relationship("Project")
    buildout_project = relationship("BuildoutProject")
    subscriber = relationship("Subscriber")
    assigned_vendor = relationship("Vendor")
    approved_quote = relationship("ProjectQuote", foreign_keys=[approved_quote_id])
    quotes = relationship(
        "ProjectQuote",
        back_populates="project",
        primaryjoin="InstallationProject.id == ProjectQuote.project_id",
    )
    project_notes = relationship("InstallationProjectNote", back_populates="project")
    as_built_routes = relationship("AsBuiltRoute", back_populates="project")
    purchase_invoices = relationship("VendorPurchaseInvoice", back_populates="project")
    lifecycle_events = relationship(
        "InstallationProjectLifecycleEvent",
        back_populates="project",
        order_by="InstallationProjectLifecycleEvent.occurred_at",
    )


class InstallationProjectLifecycleEvent(Base):
    """Append-only official evidence for the vendor project lifecycle."""

    __tablename__ = "installation_project_lifecycle_events"
    __table_args__ = (
        CheckConstraint(
            "from_status <> to_status",
            name="ck_installation_project_lifecycle_status_change",
        ),
        Index(
            "ix_installation_project_lifecycle_project_occurred",
            "project_id",
            "occurred_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, unique=True, index=True
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("installation_projects.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # NULL only for intake decisions taken before a vendor exists — publishing
    # a project for competitive bidding is exactly that. Every transition from
    # ``assigned``/``approved`` onward carries the vendor it was decided about.
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("vendors.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    from_status: Mapped[str] = mapped_column(String(40), nullable=False)
    to_status: Mapped[str] = mapped_column(String(40), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(40), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    reason: Mapped[str | None] = mapped_column(Text)
    decision_context: Mapped[dict | None] = mapped_column(JSON)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )

    project = relationship("InstallationProject", back_populates="lifecycle_events")
    vendor = relationship("Vendor")


class InstallationProjectLifecycleEventImmutableError(RuntimeError):
    pass


@event.listens_for(InstallationProjectLifecycleEvent, "before_update")
def _reject_installation_lifecycle_event_update(*_args: object) -> None:
    raise InstallationProjectLifecycleEventImmutableError(
        "Installation-project lifecycle evidence is append-only"
    )


@event.listens_for(InstallationProjectLifecycleEvent, "before_delete")
def _reject_installation_lifecycle_event_delete(*_args: object) -> None:
    raise InstallationProjectLifecycleEventImmutableError(
        "Installation-project lifecycle evidence is append-only"
    )


class ProjectQuote(Base):
    __tablename__ = "project_quotes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("installation_projects.id"),
        nullable=False,
        index=True,
    )
    vendor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vendors.id"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String(40), default=ProjectQuoteStatus.draft.value, nullable=False
    )
    currency: Mapped[str] = mapped_column(String(3), default="NGN", nullable=False)
    subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    vat_rate_percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    tax_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Staff persons — no FK, plain UUIDs.
    reviewed_by_person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    review_notes: Mapped[str | None] = mapped_column(Text)
    created_by_person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    project = relationship(
        "InstallationProject", back_populates="quotes", foreign_keys=[project_id]
    )
    vendor = relationship("Vendor", back_populates="quotes")
    line_items = relationship("ProjectQuoteLineItem", back_populates="quote")
    route_revisions = relationship("ProposedRouteRevision", back_populates="quote")


class ProjectQuoteLineItem(Base):
    # CRM table ``quote_line_items`` renamed → ``project_quote_line_items`` to
    # avoid the clash with sub's sales ``quote_line_items`` (app/models/sales.py).
    __tablename__ = "project_quote_line_items"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    quote_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_quotes.id"), nullable=False
    )
    item_type: Mapped[str | None] = mapped_column(String(80))
    description: Mapped[str | None] = mapped_column(Text)
    cable_type: Mapped[str | None] = mapped_column(String(120))
    fiber_count: Mapped[int | None] = mapped_column(Integer)
    splice_count: Mapped[int | None] = mapped_column(Integer)
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=Decimal("1.000"))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    notes: Mapped[str | None] = mapped_column(Text)
    # Unique client-supplied id so an offline mobile add that retries doesn't
    # duplicate the line (mirrors FieldAttachment.client_ref).
    client_ref: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), unique=True, index=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    quote = relationship("ProjectQuote", back_populates="line_items")


class VendorPurchaseInvoice(Base):
    """Vendor bill originated in Sub and posted to ERP's AP subledger."""

    __tablename__ = "vendor_purchase_invoices"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "vendor_id",
            name="uq_vendor_purchase_invoice_project_vendor",
        ),
        UniqueConstraint(
            "vendor_id",
            "invoice_number",
            name="uq_vendor_purchase_invoice_vendor_number",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    invoice_number: Mapped[str | None] = mapped_column(String(80), index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("installation_projects.id"),
        nullable=False,
        index=True,
    )
    vendor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vendors.id"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String(40), default=VendorPurchaseInvoiceStatus.draft.value, nullable=False
    )
    currency: Mapped[str] = mapped_column(String(3), default="NGN", nullable=False)
    tax_rate_percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    subtotal: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0.00"), nullable=False
    )
    tax_total: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0.00"), nullable=False
    )
    total: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0.00"), nullable=False
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_by_system_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("system_users.id")
    )
    review_notes: Mapped[str | None] = mapped_column(Text)
    created_by_system_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("system_users.id")
    )
    attachment_stored_file_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("stored_files.id")
    )
    payables_system: Mapped[str | None] = mapped_column(String(40))
    procurement_order_reference: Mapped[str | None] = mapped_column(
        String(100), index=True
    )
    payables_document_reference: Mapped[str | None] = mapped_column(
        String(100), index=True
    )
    payables_document_status: Mapped[str | None] = mapped_column(String(40))
    payment_status: Mapped[str | None] = mapped_column(String(40))
    payment_total_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    payment_amount_paid: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    payment_balance_due: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    payment_observed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    payment_source_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    payment_observation_error: Mapped[str | None] = mapped_column(String(500))
    payables_submission_error: Mapped[str | None] = mapped_column(String(500))
    payables_submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    payables_attachment_submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    project = relationship("InstallationProject", back_populates="purchase_invoices")
    vendor = relationship("Vendor", back_populates="purchase_invoices")
    reviewed_by = relationship("SystemUser", foreign_keys=[reviewed_by_system_user_id])
    created_by = relationship("SystemUser", foreign_keys=[created_by_system_user_id])
    attachment = relationship("StoredFile", foreign_keys=[attachment_stored_file_id])
    line_items = relationship(
        "VendorPurchaseInvoiceLineItem",
        back_populates="invoice",
        cascade="all, delete-orphan",
    )


class VendorPurchaseInvoiceLineItem(Base):
    __tablename__ = "vendor_purchase_invoice_line_items"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("vendor_purchase_invoices.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    item_type: Mapped[str | None] = mapped_column(String(80))
    description: Mapped[str | None] = mapped_column(Text)
    quantity: Mapped[Decimal] = mapped_column(
        Numeric(12, 3), default=Decimal("1.000"), nullable=False
    )
    unit_price: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0.00"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0.00"), nullable=False
    )
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    invoice = relationship("VendorPurchaseInvoice", back_populates="line_items")


# ---------------------------------------------------------------------------
# Routes — proposed revisions + as-built (carry the route_geom LINESTRING)
# ---------------------------------------------------------------------------


class ProposedRouteRevision(Base):
    __tablename__ = "proposed_route_revisions"
    __table_args__ = (
        UniqueConstraint(
            "quote_id", "revision_number", name="uq_proposed_route_quote_revision"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    quote_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_quotes.id"), nullable=False
    )
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(40), default=ProposedRouteRevisionStatus.draft.value, nullable=False
    )
    route_geom = mapped_column(Geometry("LINESTRING", srid=4326), nullable=True)
    length_meters: Mapped[float | None] = mapped_column(Float)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Staff persons — no FK, plain UUIDs.
    submitted_by_person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_by_person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    review_notes: Mapped[str | None] = mapped_column(Text)
    fiber_segment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fiber_segments.id")
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    quote = relationship("ProjectQuote", back_populates="route_revisions")
    fiber_segment = relationship("FiberSegment")
    review_events = relationship(
        "ProposedRouteRevisionReviewEvent",
        back_populates="revision",
        order_by="ProposedRouteRevisionReviewEvent.occurred_at",
    )


class ProposedRouteRevisionReviewEvent(Base):
    """Append-only official evidence for staff proposed-route decisions."""

    __tablename__ = "proposed_route_revision_review_events"
    __table_args__ = (
        CheckConstraint(
            "from_status <> to_status",
            name="ck_proposed_route_review_event_status_change",
        ),
        Index(
            "ix_proposed_route_review_event_revision_occurred",
            "revision_id",
            "occurred_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, unique=True, index=True
    )
    revision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("proposed_route_revisions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    quote_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("project_quotes.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("installation_projects.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    vendor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("vendors.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    from_status: Mapped[str] = mapped_column(String(40), nullable=False)
    to_status: Mapped[str] = mapped_column(String(40), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(40), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    reason: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )

    revision = relationship("ProposedRouteRevision", back_populates="review_events")
    quote = relationship("ProjectQuote")
    project = relationship("InstallationProject")
    vendor = relationship("Vendor")


class ProposedRouteRevisionReviewEventImmutableError(RuntimeError):
    pass


@event.listens_for(ProposedRouteRevisionReviewEvent, "before_update")
def _reject_proposed_route_review_event_update(*_args: object) -> None:
    raise ProposedRouteRevisionReviewEventImmutableError(
        "Proposed-route review evidence is append-only"
    )


@event.listens_for(ProposedRouteRevisionReviewEvent, "before_delete")
def _reject_proposed_route_review_event_delete(*_args: object) -> None:
    raise ProposedRouteRevisionReviewEventImmutableError(
        "Proposed-route review evidence is append-only"
    )


class AsBuiltRoute(Base):
    __tablename__ = "as_built_routes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("installation_projects.id"),
        nullable=False,
        index=True,
    )
    proposed_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("proposed_route_revisions.id")
    )
    status: Mapped[str] = mapped_column(
        String(40), default=AsBuiltRouteStatus.submitted.value, nullable=False
    )
    route_geom = mapped_column(Geometry("LINESTRING", srid=4326), nullable=True)
    actual_length_meters: Mapped[float | None] = mapped_column(Float)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Staff persons — no FK, plain UUIDs.
    submitted_by_person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_by_person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    review_notes: Mapped[str | None] = mapped_column(Text)
    fiber_segment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fiber_segments.id")
    )
    report_file_path: Mapped[str | None] = mapped_column(String(500))
    report_file_name: Mapped[str | None] = mapped_column(String(255))
    report_generated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    variation_type: Mapped[str | None] = mapped_column(String(40))
    variation_reason: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    work_order_ref: Mapped[str | None] = mapped_column(String(120))
    backoffice_sync_status: Mapped[str | None] = mapped_column(String(40))
    backoffice_reference: Mapped[str | None] = mapped_column(String(120))
    backoffice_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    project = relationship("InstallationProject", back_populates="as_built_routes")
    proposed_revision = relationship("ProposedRouteRevision")
    fiber_segment = relationship("FiberSegment")
    line_items = relationship(
        "AsBuiltLineItem", back_populates="as_built", cascade="all, delete-orphan"
    )
    review_events = relationship(
        "AsBuiltRouteReviewEvent",
        back_populates="as_built",
        order_by="AsBuiltRouteReviewEvent.occurred_at",
    )


class AsBuiltRouteReviewEvent(Base):
    """Append-only official evidence for staff as-built review decisions."""

    __tablename__ = "as_built_route_review_events"
    __table_args__ = (
        CheckConstraint(
            "from_status <> to_status",
            name="ck_as_built_review_event_status_change",
        ),
        Index(
            "ix_as_built_review_event_route_occurred",
            "as_built_id",
            "occurred_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, unique=True, index=True
    )
    as_built_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("as_built_routes.id", ondelete="RESTRICT"),
        nullable=False,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("installation_projects.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    vendor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("vendors.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    from_status: Mapped[str] = mapped_column(String(40), nullable=False)
    to_status: Mapped[str] = mapped_column(String(40), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(40), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    reason: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )

    as_built = relationship("AsBuiltRoute", back_populates="review_events")
    project = relationship("InstallationProject")
    vendor = relationship("Vendor")


class AsBuiltRouteReviewEventImmutableError(RuntimeError):
    pass


@event.listens_for(AsBuiltRouteReviewEvent, "before_update")
def _reject_as_built_review_event_update(*_args: object) -> None:
    raise AsBuiltRouteReviewEventImmutableError(
        "As-built review evidence is append-only"
    )


@event.listens_for(AsBuiltRouteReviewEvent, "before_delete")
def _reject_as_built_review_event_delete(*_args: object) -> None:
    raise AsBuiltRouteReviewEventImmutableError(
        "As-built review evidence is append-only"
    )


class AsBuiltLineItem(Base):
    __tablename__ = "as_built_line_items"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    as_built_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("as_built_routes.id"),
        nullable=False,
        index=True,
    )
    item_type: Mapped[str | None] = mapped_column(String(80))
    description: Mapped[str | None] = mapped_column(Text)
    cable_type: Mapped[str | None] = mapped_column(String(120))
    fiber_count: Mapped[int | None] = mapped_column(Integer)
    splice_count: Mapped[int | None] = mapped_column(Integer)
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=Decimal("1.000"))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    as_built = relationship("AsBuiltRoute", back_populates="line_items")


class InstallationProjectNote(Base):
    __tablename__ = "installation_project_notes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("installation_projects.id"),
        nullable=False,
        index=True,
    )
    # Staff person — no FK, plain UUID.
    author_person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    body: Mapped[str] = mapped_column(Text, nullable=False)
    is_internal: Mapped[bool] = mapped_column(Boolean, default=False)
    attachments: Mapped[list | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    project = relationship("InstallationProject", back_populates="project_notes")
