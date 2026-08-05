"""Native leads/pipeline, quotes, and sales-orders verticals ported from the
CRM.

CRM shapes (``dotmac_crm/app/models/crm/sales.py`` and
``dotmac_crm/app/models/sales_order.py``) carried verbatim with the sub
conventions applied — table names drop the ``crm_`` prefix:
``crm_pipelines``→``pipelines``, ``crm_pipeline_stages``→``pipeline_stages``,
``crm_leads``→``leads``, ``crm_quotes``→``quotes``,
``crm_quote_line_items``→``quote_line_items``; ``sales_orders`` /
``sales_order_lines`` keep their names.

* PG enums become String columns + app-level enums.
* Quote and SalesOrder customer FKs re-point at ``subscribers.id``. Revision
  345 makes Lead Party-first: ``party_id`` is reviewed identity and nullable
  ``subscriber_id`` is later account context.
* Staff FKs are dropped, UUIDs carried verbatim:
  ``quotes.owner_person_id`` (staff map for display) and the not-yet-native
  ``owner_agent_id`` columns on leads/sales orders (→ ``crm_agents``).
* ``leads.campaign_id``/``campaign_recipient_id`` project only native Sub
  campaign origins. External provider IDs live in immutable structured origin
  capture; revision 355 materializes deferred campaign FKs.
* ``quote_line_items.inventory_item_id`` / ``sales_order_lines
  .inventory_item_id`` are plain UUIDs while inventory remains externally owned.
* The legacy Subscriber/pipeline open-Lead index remains for compatibility;
  revision 355 adds the Party/pipeline equivalent for canonical new writes.
* ``sales_orders.order_number`` continues the CRM ``SO-%06d`` sequence via
  sub's existing ``document_sequences`` (the backfill inserts the CRM row's
  ``next_value``, ).

CRM UUID PKs are kept verbatim by the import. The ``quotes`` table
coexists with ``quote_mirror`` until the contract PR.
"""

import enum
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class LeadStatus(enum.Enum):
    new = "new"
    contacted = "contacted"
    qualified = "qualified"
    proposal = "proposal"
    negotiation = "negotiation"
    won = "won"
    lost = "lost"


class LeadCaptureMethod(enum.StrEnum):
    ad_lead_form_webhook = "ad_lead_form_webhook"
    landing_page = "landing_page"
    portal = "portal"
    agent_declared = "agent_declared"
    campaign_response = "campaign_response"
    referral = "referral"
    reviewed_import = "reviewed_import"
    inbox_form = "inbox_form"


class LeadSourcePlatform(enum.StrEnum):
    meta = "meta"
    google = "google"
    website = "website"
    portal = "portal"
    agent = "agent"
    referral = "referral"
    sub_campaign = "sub_campaign"
    legacy_import = "legacy_import"
    team_inbox = "team_inbox"


class QuoteStatus(enum.Enum):
    draft = "draft"
    sent = "sent"
    accepted = "accepted"
    rejected = "rejected"
    expired = "expired"


class QuoteDeliveryRequestStatus(enum.StrEnum):
    queued = "queued"
    suppressed = "suppressed"


class SalesOrderStatus(enum.Enum):
    draft = "draft"
    confirmed = "confirmed"
    paid = "paid"
    fulfilled = "fulfilled"
    cancelled = "cancelled"


class SalesOrderPaymentStatus(enum.Enum):
    pending = "pending"
    partial = "partial"
    paid = "paid"
    waived = "waived"


class Pipeline(Base):
    __tablename__ = "pipelines"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    stages = relationship("PipelineStage", back_populates="pipeline")
    leads = relationship("Lead", back_populates="pipeline")


class PipelineStage(Base):
    __tablename__ = "pipeline_stages"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    pipeline_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pipelines.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    default_probability: Mapped[int] = mapped_column(Integer, default=50)
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    pipeline = relationship("Pipeline", back_populates="stages")
    leads = relationship("Lead", back_populates="stage")


class Lead(Base):
    """Sales opportunity linked to a Party before an account is required.

    ``subscriber_id`` remains a nullable compatibility/account link. New
    Party-first capture does not create a fake Subscriber; quote/account
    conversion attaches a reviewed Subscriber later.
    """

    __tablename__ = "leads"
    __table_args__ = (
        Index("ix_leads_campaign_id", "campaign_id"),
        Index("ix_leads_party_id", "party_id"),
        Index("ix_leads_reseller_id", "reseller_id"),
        # Native lead scans by subscriber (migration 251).
        Index("ix_leads_subscriber_id", "subscriber_id"),
        CheckConstraint(
            "party_id IS NOT NULL OR subscriber_id IS NOT NULL",
            name="ck_leads_party_or_subscriber",
        ),
        CheckConstraint(
            "(party_id IS NULL AND party_bound_at IS NULL AND "
            "party_binding_source IS NULL AND party_binding_reason IS NULL) OR "
            "(party_id IS NOT NULL AND party_bound_at IS NOT NULL AND "
            "party_binding_source IS NOT NULL AND party_binding_reason IS NOT NULL "
            "AND length(trim(party_binding_source)) > 0 AND "
            "length(trim(party_binding_reason)) > 0)",
            name="ck_leads_party_binding_evidence",
        ),
        CheckConstraint(
            "(subscriber_id IS NULL AND subscriber_linked_at IS NULL AND "
            "subscriber_link_source IS NULL AND subscriber_link_reason IS NULL) OR "
            "(subscriber_id IS NOT NULL AND subscriber_linked_at IS NULL AND "
            "subscriber_link_source IS NULL AND subscriber_link_reason IS NULL) OR "
            "(subscriber_id IS NOT NULL AND subscriber_linked_at IS NOT NULL AND "
            "subscriber_link_source IS NOT NULL AND subscriber_link_reason IS NOT "
            "NULL AND length(trim(subscriber_link_source)) > 0 AND "
            "length(trim(subscriber_link_reason)) > 0)",
            name="ck_leads_subscriber_link_evidence",
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
    subscriber_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id")
    )
    # Optional external reseller ownership captured before an account exists.
    # ``None`` means the eventual Subscriber uses the canonical House reseller.
    reseller_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("resellers.id", ondelete="RESTRICT")
    )
    subscriber_linked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    subscriber_link_source: Mapped[str | None] = mapped_column(String(80))
    subscriber_link_reason: Mapped[str | None] = mapped_column(Text)
    pipeline_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pipelines.id")
    )
    stage_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pipeline_stages.id")
    )
    # CrmAgent UUID carried verbatim — inbox model, no FK.
    owner_agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    title: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(
        String(40), default=LeadStatus.new.value, nullable=False
    )
    estimated_value: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str | None] = mapped_column(String(3))
    probability: Mapped[int | None] = mapped_column(Integer)
    expected_close_date: Mapped[date | None] = mapped_column(Date)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lost_reason: Mapped[str | None] = mapped_column(String(200))
    # Normalized vocabulary lives in the sales service (LEAD_SOURCE_OPTIONS,
    # gaining "Portal" during the service port, ).
    lead_source: Mapped[str | None] = mapped_column(String(40))
    # Compatibility projection of a native Sub campaign origin. External ad
    # provider IDs live in LeadOriginCapture and are never forced into UUIDs.
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="RESTRICT")
    )
    campaign_recipient_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("campaign_recipients.id", ondelete="RESTRICT"),
    )
    region: Mapped[str | None] = mapped_column(String(80))
    address: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    subscriber = relationship("Subscriber", foreign_keys=[subscriber_id])
    party = relationship("Party", foreign_keys=[party_id])
    pipeline = relationship("Pipeline", back_populates="leads")
    stage = relationship("PipelineStage", back_populates="leads")
    quotes = relationship("Quote", back_populates="lead")
    origin_capture = relationship(
        "LeadOriginCapture",
        back_populates="lead",
        uselist=False,
        cascade="all, delete-orphan",
    )

    # Transient (non-persisted) flag set by the dedup path in Leads.create when
    # an existing open lead is returned instead of a new one being created, so
    # callers (e.g. the web route) can surface a distinct "existing lead"
    # notice. Ports with the model.
    dedup_returned_existing: bool = False

    @hybrid_property
    def contact_id(self):
        return self.subscriber_id

    @contact_id.expression  # type: ignore[no-redef]
    def contact_id(cls):
        return cls.subscriber_id

    @hybrid_property
    def weighted_value(self) -> Decimal | None:
        """Return estimated_value weighted by probability."""
        if self.estimated_value is None or self.probability is None:
            return None
        return self.estimated_value * Decimal(self.probability) / Decimal(100)


class LeadOriginCapture(Base):
    """Immutable, structured evidence for how a Lead entered Sub.

    Native Sub campaigns and external advertising campaigns are intentionally
    distinct. Provider IDs remain structured provenance; raw webhook payloads
    and contact PII do not belong here.
    """

    __tablename__ = "lead_origin_captures"
    __table_args__ = (
        UniqueConstraint("lead_id", name="uq_lead_origin_captures_lead_id"),
        UniqueConstraint(
            "integration_inbox_id",
            name="uq_lead_origin_captures_integration_inbox",
        ),
        CheckConstraint(
            "capture_method IN ('ad_lead_form_webhook', 'landing_page', 'portal', "
            "'agent_declared', 'campaign_response', 'referral', "
            "'reviewed_import', 'inbox_form')",
            name="ck_lead_origin_captures_method",
        ),
        CheckConstraint(
            "source_platform IN ('meta', 'google', 'website', 'portal', 'agent', "
            "'referral', 'sub_campaign', 'legacy_import', 'team_inbox')",
            name="ck_lead_origin_captures_platform",
        ),
        CheckConstraint(
            "campaign_recipient_id IS NULL OR campaign_id IS NOT NULL",
            name="ck_lead_origin_captures_recipient_campaign",
        ),
        CheckConstraint(
            "capture_method <> 'campaign_response' OR "
            "(campaign_id IS NOT NULL AND campaign_recipient_id IS NOT NULL AND "
            "source_platform = 'sub_campaign')",
            name="ck_lead_origin_captures_campaign_response",
        ),
        CheckConstraint(
            "capture_method <> 'ad_lead_form_webhook' OR "
            "(source_platform IN ('meta', 'google') AND "
            "external_campaign_id IS NOT NULL AND "
            "length(trim(external_campaign_id)) > 0)",
            name="ck_lead_origin_captures_ad_webhook",
        ),
        CheckConstraint(
            "(capture_method <> 'landing_page' OR source_platform = 'website') AND "
            "(capture_method <> 'portal' OR source_platform = 'portal') AND "
            "(capture_method <> 'agent_declared' OR source_platform = 'agent') AND "
            "(capture_method <> 'referral' OR source_platform = 'referral') AND "
            "(capture_method <> 'reviewed_import' OR "
            "source_platform = 'legacy_import') AND "
            "(capture_method <> 'inbox_form' OR source_platform = 'team_inbox')",
            name="ck_lead_origin_captures_method_platform",
        ),
        CheckConstraint(
            "(source_platform <> 'meta' OR lead_source IN "
            "('Facebook Ads', 'Instagram Ads')) AND "
            "(source_platform <> 'google' OR lead_source = 'Google') AND "
            "(source_platform <> 'website' OR lead_source = 'Website') AND "
            "(source_platform <> 'portal' OR lead_source = 'Portal') AND "
            "(source_platform <> 'referral' OR lead_source = 'Referrer') AND "
            "(source_platform <> 'team_inbox' OR lead_source IN "
            "('Whatsapp', 'Facebook', 'Instagram', 'Email', 'Website'))",
            name="ck_lead_origin_captures_platform_source",
        ),
        CheckConstraint(
            "length(trim(capture_source)) > 0 AND length(trim(capture_reason)) > 0",
            name="ck_lead_origin_captures_evidence",
        ),
        Index("ix_lead_origin_captures_campaign", "campaign_id"),
        Index(
            "ix_lead_origin_captures_external_campaign",
            "source_platform",
            "external_campaign_id",
        ),
        Index(
            "uq_lead_origin_captures_source_interaction",
            "source_platform",
            "source_interaction_id",
            unique=True,
            postgresql_where=text("source_interaction_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    lead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("leads.id", ondelete="CASCADE"),
        nullable=False,
    )
    integration_inbox_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "integration_inbox.id",
            name="fk_lead_origin_captures_integration_inbox",
            ondelete="RESTRICT",
        ),
    )
    source_interaction_id: Mapped[str | None] = mapped_column(String(240))
    capture_fingerprint: Mapped[str | None] = mapped_column(String(64))
    capture_method: Mapped[str] = mapped_column(String(40), nullable=False)
    source_platform: Mapped[str] = mapped_column(String(40), nullable=False)
    lead_source: Mapped[str] = mapped_column(String(40), nullable=False)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="RESTRICT")
    )
    campaign_recipient_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("campaign_recipients.id", ondelete="RESTRICT"),
    )
    external_campaign_id: Mapped[str | None] = mapped_column(String(200))
    external_ad_set_id: Mapped[str | None] = mapped_column(String(200))
    external_ad_id: Mapped[str | None] = mapped_column(String(200))
    external_form_id: Mapped[str | None] = mapped_column(String(200))
    external_click_id: Mapped[str | None] = mapped_column(String(255))
    utm_source: Mapped[str | None] = mapped_column(String(200))
    utm_medium: Mapped[str | None] = mapped_column(String(200))
    utm_campaign: Mapped[str | None] = mapped_column(String(200))
    utm_content: Mapped[str | None] = mapped_column(String(200))
    utm_term: Mapped[str | None] = mapped_column(String(200))
    landing_path: Mapped[str | None] = mapped_column(String(500))
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    capture_source: Mapped[str] = mapped_column(String(80), nullable=False)
    capture_reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    lead = relationship("Lead", back_populates="origin_capture")
    campaign = relationship("Campaign")
    campaign_recipient = relationship("CampaignRecipient")
    integration_inbox = relationship("IntegrationInbox")


class LeadOriginCaptureImmutableError(RuntimeError):
    """Raised when code attempts to rewrite official acquisition evidence."""


@event.listens_for(LeadOriginCapture, "before_update")
def _reject_lead_origin_capture_update(*_args: object) -> None:
    raise LeadOriginCaptureImmutableError("Lead origin evidence is append-only")


@event.listens_for(LeadOriginCapture, "before_delete")
def _reject_lead_origin_capture_delete(*_args: object) -> None:
    raise LeadOriginCaptureImmutableError("Lead origin evidence is append-only")


class Quote(Base):
    """Sales quote. ``project_type`` owns the implementation classification.

    ``metadata`` carries the remaining portal contract: source, install{...},
    feasibility{}, deposit_percent, estimate_provisional, pricing_mode, and
    deposit{...}. ``metadata.project_type`` is a compatibility projection;
    ``metadata.subscriber_external_id`` is legacy provenance."""

    __tablename__ = "quotes"
    __table_args__ = (
        # Native /me/quotes (+ reseller subtree) subscriber scan — partial on
        # is_active (migration 251). postgresql_where is ignored on sqlite.
        Index(
            "ix_quotes_subscriber_id",
            "subscriber_id",
            postgresql_where=text("is_active"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscriber_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=True
    )
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id")
    )
    # Staff person UUID (quote owner = lead's agent person) — no FK.
    owner_person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    status: Mapped[str] = mapped_column(
        String(20), default=QuoteStatus.draft.value, nullable=False
    )
    # Nullable only for imported legacy rows. Every native Quote authoring
    # command requires this value before the Quote can be accepted.
    project_type: Mapped[str | None] = mapped_column(String(60))
    currency: Mapped[str] = mapped_column(String(3), default="NGN")
    subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    # Applied tax rate percent (e.g. 7.5). When set, tax_total is auto-derived
    # from the subtotal on every recalculation; null = manual tax_total.
    tax_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    tax_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    subscriber = relationship("Subscriber", foreign_keys=[subscriber_id])
    lead = relationship("Lead", back_populates="quotes")
    line_items = relationship("QuoteLineItem", back_populates="quote")
    sales_order = relationship("SalesOrder", back_populates="quote", uselist=False)
    project = relationship(
        "Project",
        back_populates="quote",
        uselist=False,
        foreign_keys="Project.quote_id",
    )
    pdf_exports = relationship("QuotePdfExport", back_populates="quote")
    delivery_requests = relationship("QuoteDeliveryRequest", back_populates="quote")

    @property
    def person_id(self) -> uuid.UUID | None:
        """Return the authoritative recipient through ``Quote -> Lead -> Party``.

        Party identity is intentionally not duplicated on the Quote row.
        """

        return self.lead.party_id if self.lead is not None else None

    @hybrid_property
    def sales_order_id(self):
        return self.sales_order.id if self.sales_order else None

    @hybrid_property
    def contact_id(self):
        return self.subscriber_id

    @contact_id.expression  # type: ignore[no-redef]
    def contact_id(cls):
        return cls.subscriber_id


class QuotePdfExport(Base):
    """Immutable customer-facing Quote document artifact."""

    __tablename__ = "quote_pdf_exports"
    __table_args__ = (
        UniqueConstraint(
            "quote_id",
            "snapshot_fingerprint",
            name="uq_quote_pdf_exports_snapshot",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    quote_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("quotes.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    stored_file_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("stored_files.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    snapshot_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot: Mapped[dict] = mapped_column(
        MutableDict.as_mutable(JSON()), nullable=False
    )
    requested_by_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    quote = relationship("Quote", back_populates="pdf_exports")
    stored_file = relationship("StoredFile")


class QuoteDeliveryRequest(Base):
    """Durable, idempotent request to email one exact Quote PDF."""

    __tablename__ = "quote_delivery_requests"
    __table_args__ = (
        UniqueConstraint(
            "idempotency_key", name="uq_quote_delivery_requests_idempotency_key"
        ),
        CheckConstraint(
            "request_status IN ('queued', 'suppressed')",
            name="ck_quote_delivery_requests_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    quote_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("quotes.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    pdf_export_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("quote_pdf_exports.id", ondelete="RESTRICT"),
        nullable=False,
    )
    recipient_contact_point_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("party_contact_points.id", ondelete="RESTRICT"),
        nullable=False,
    )
    communication_intent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("communication_intents.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    requested_by_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_status: Mapped[str] = mapped_column(
        String(24),
        default=QuoteDeliveryRequestStatus.queued.value,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    quote = relationship("Quote", back_populates="delivery_requests")
    pdf_export = relationship("QuotePdfExport")
    recipient_contact_point = relationship("PartyContactPoint")
    communication_intent = relationship("CommunicationIntentRecord")


class QuoteLineItem(Base):
    """Quote line (CRM ``CrmQuoteLineItem``). ``amount`` stays server-derived;
    ``metadata.sub_offer_id`` is already a sub CatalogOffer id."""

    __tablename__ = "quote_line_items"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    quote_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("quotes.id"), nullable=False, index=True
    )
    # CRM inventory-item UUID carried verbatim — inventory is no FK.
    inventory_item_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=Decimal("1.000"))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    # Line discount percent (0-100); amount is net of discount.
    discount_percent: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), default=Decimal("0.00"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    quote = relationship("Quote", back_populates="line_items")


class SalesOrder(Base):
    """Sales order. The CRM person-mediated SO→sub link collapses to the
    first-class ``subscriber_id`` column."""

    __tablename__ = "sales_orders"
    __table_args__ = (
        UniqueConstraint("order_number", name="uq_sales_orders_order_number"),
        UniqueConstraint("quote_id", name="uq_sales_orders_quote_id"),
        # Native sales-order scans by subscriber (migration 251).
        Index("ix_sales_orders_subscriber_id", "subscriber_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    quote_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("quotes.id")
    )
    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=False
    )
    # Native SystemUser UUID selected from the Customer Experience directory.
    # Kept without an FK so imported historical CRM owner provenance remains readable.
    owner_agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    source: Mapped[str | None] = mapped_column(String(80))
    # SO-%06d via document_sequences key "sales_order_number".
    order_number: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(
        String(20), default=SalesOrderStatus.draft.value, nullable=False
    )
    payment_status: Mapped[str] = mapped_column(
        String(20), default=SalesOrderPaymentStatus.pending.value, nullable=False
    )
    currency: Mapped[str] = mapped_column(String(3), default="NGN")
    subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    tax_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    amount_paid: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0.00")
    )
    balance_due: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0.00")
    )
    payment_due_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deposit_required: Mapped[bool] = mapped_column(Boolean, default=False)
    deposit_paid: Mapped[bool] = mapped_column(Boolean, default=False)
    contract_signed: Mapped[bool] = mapped_column(Boolean, default=False)
    signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    subscriber = relationship("Subscriber", foreign_keys=[subscriber_id])
    quote = relationship("Quote", back_populates="sales_order")
    lines = relationship("SalesOrderLine", back_populates="sales_order")
    project = relationship(
        "Project",
        back_populates="sales_order",
        uselist=False,
        foreign_keys="Project.sales_order_id",
    )


class SalesOrderLine(Base):
    """Sales-order line. ``metadata`` carries sub_offer_id +
    selfcare_subscription_id/selfcare_subscription_invoice_id — after the
    native rewire these point at sub's own subscription/invoice rows."""

    __tablename__ = "sales_order_lines"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    sales_order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sales_orders.id"), nullable=False, index=True
    )
    # CRM inventory-item UUID carried verbatim — inventory is no FK.
    inventory_item_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=Decimal("1.000"))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    sales_order = relationship("SalesOrder", back_populates="lines")
