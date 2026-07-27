import enum
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import sqlalchemy.orm as sa_orm
from geoalchemy2 import Geometry
from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import (
    Mapped,
    mapped_column,
    relationship,
    synonym,
)

from app.db import Base
from app.models.catalog import BillingMode
from app.models.organization import Organization  # noqa: F401 (mapper registry)
from app.models.party import Party
from app.models.subscription_engine import SettingValueType


class Gender(enum.Enum):
    unknown = "unknown"
    female = "female"
    male = "male"
    non_binary = "non_binary"
    other = "other"


class ContactMethod(enum.Enum):
    email = "email"
    phone = "phone"
    sms = "sms"
    push = "push"


class SubscriberStatus(enum.Enum):
    """Account status — mirrors the imported customer status lifecycle.

    Imported status mapping (1:1):
      new      → new (signed up, not yet activated)
      active   → active (paying, service running)
      blocked  → blocked (temporarily blocked — non-payment or violation)
      disabled → disabled (administratively paused; explicit re-enable required)
      deleted  → canceled (soft-deleted, record preserved for audit)

    DotMac-only statuses:
      suspended — generic suspension (local origin)
      delinquent — past due, pre-suspension
    """

    new = "new"  # Signed up, awaiting activation
    active = "active"  # Active, paying subscriber
    blocked = "blocked"  # Temporarily blocked
    suspended = "suspended"  # Generic suspension (DotMac-native)
    disabled = "disabled"  # Administratively paused; explicit re-enable required
    canceled = "canceled"  # Terminated / soft-deleted
    delinquent = "delinquent"  # Past due, pre-suspension


class PartyStatus(enum.Enum):
    """Lifecycle status for the unified party model (CRM ``people.party_status``).

    Keeps prospects created by the party-identity backfill
    party backfill (leads/quotes/referrals persons with no account) out of
    billing/RADIUS sweeps. Stored as String(20) + app enum per sub convention.
    """

    lead = "lead"  # Prospect, minimal info
    contact = "contact"  # Known individual, verified
    customer = "customer"  # Converted (accepted quote/signed up)
    subscriber = "subscriber"  # Active billing account


# --- Deprecated aliases for backwards compatibility ---
AccountStatus = SubscriberStatus  # Alias for legacy code


class UserType(enum.Enum):
    """Classification for admin/system user accounts."""

    system_user = "system_user"
    customer = "customer"
    reseller = "reseller"
    # External contractor principals. They authenticate through the same
    # ``system_users`` table as staff, so without this marker a vendor login is
    # indistinguishable from an employee login — and staff screens, grants, and
    # audits cannot tell them apart. Vendor capability is resolved by
    # ``app.services.field.vendor_capabilities``, never by staff RBAC.
    vendor = "vendor"


class SubscriberCategory(enum.Enum):
    residential = "residential"
    business = "business"
    government = "government"
    ngo = "ngo"


class AddressType(enum.Enum):
    service = "service"
    billing = "billing"
    mailing = "mailing"


class ChannelType(enum.Enum):
    """Communication channel types."""

    email = "email"
    phone = "phone"
    sms = "sms"
    whatsapp = "whatsapp"


class NINVerificationStatus(enum.Enum):
    pending = "pending"
    success = "success"
    failed = "failed"


class Reseller(Base):
    """Reseller/partner who manages subscribers.

    A single row with `is_house=True` represents the main company itself, so
    every Subscriber transitively belongs to a Reseller and a BillingAccount.
    """

    __tablename__ = "resellers"
    __table_args__ = (
        CheckConstraint(
            "(party_id IS NULL AND party_bound_at IS NULL AND "
            "party_binding_source IS NULL AND party_binding_reason IS NULL) OR "
            "(party_id IS NOT NULL AND party_bound_at IS NOT NULL AND "
            "party_binding_source IS NOT NULL AND "
            "party_binding_reason IS NOT NULL AND "
            "length(trim(party_binding_source)) > 0 AND "
            "length(trim(party_binding_reason)) > 0)",
            name="ck_resellers_party_binding_evidence",
        ),
        UniqueConstraint("party_id", name="uq_resellers_party_id"),
        Index(
            "uq_resellers_one_house",
            "is_house",
            unique=True,
            postgresql_where=text("is_house"),
            sqlite_where=text("is_house"),
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
    contact_email: Mapped[str | None] = mapped_column(String(255))
    contact_phone: Mapped[str | None] = mapped_column(String(40))
    # Reseller-level dunning policy override (applies to the reseller's accounts
    # unless an account sets its own). See Subscriber.policy_set_id.
    policy_set_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("policy_sets.id")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_house: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    # C-2: catalog visibility override. NULL = inherit the global
    # ``reseller_default_catalog_open`` setting; True = this reseller sees ONLY
    # offers explicitly assigned to it (assigned-only); False = open (all
    # unrestricted offers, today's behavior).
    restrict_to_assigned_offers: Mapped[bool | None] = mapped_column(Boolean)
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    subscribers = relationship("Subscriber", back_populates="reseller")
    party = relationship("Party", back_populates="reseller_profile")
    billing_account = relationship(
        "BillingAccount", back_populates="reseller", uselist=False
    )


class Subscriber(Base):
    """Subscriber service/billing account with legacy identity fields.

    ``party_id`` is the canonical identity binding when populated. The copied
    name/contact columns remain compatibility data until their own verified
    cutover; they do not become a second identity authority.
    """

    __tablename__ = "subscribers"
    __table_args__ = (
        Index(
            "uq_subscribers_splynx_customer_id",
            "splynx_customer_id",
            unique=True,
            postgresql_where=text("splynx_customer_id IS NOT NULL"),
        ),
        Index(
            "uq_subscribers_crm_subscriber_id",
            "crm_subscriber_id",
            unique=True,
            postgresql_where=text("crm_subscriber_id IS NOT NULL"),
        ),
        CheckConstraint(
            "(party_id IS NULL AND party_bound_at IS NULL AND "
            "party_binding_source IS NULL AND party_binding_reason IS NULL) OR "
            "(party_id IS NOT NULL AND party_bound_at IS NOT NULL AND "
            "party_binding_source IS NOT NULL AND "
            "party_binding_reason IS NOT NULL AND "
            "length(trim(party_binding_source)) > 0 AND "
            "length(trim(party_binding_reason)) > 0)",
            name="ck_subscribers_party_binding_evidence",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # Canonical identity binding. One Party may own several service/billing
    # accounts; this column is deliberately not unique. The evidence columns
    # make the first reviewed binding attributable and are immutable through
    # the party.registry command (repointing is a later reviewed slice).
    party_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("parties.id", ondelete="RESTRICT"),
        index=True,
    )
    party_bound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    party_binding_source: Mapped[str | None] = mapped_column(String(80))
    party_binding_reason: Mapped[str | None] = mapped_column(Text)

    # === Identity Fields (from Person) ===
    first_name: Mapped[str] = mapped_column(String(80), nullable=False)
    last_name: Mapped[str] = mapped_column(String(80), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(120))
    avatar_url: Mapped[str | None] = mapped_column(String(512))
    company_name: Mapped[str | None] = mapped_column(String(160))
    legal_name: Mapped[str | None] = mapped_column(String(200))
    tax_id: Mapped[str | None] = mapped_column(String(80))
    domain: Mapped[str | None] = mapped_column(String(120))
    website: Mapped[str | None] = mapped_column(String(255))

    # Email is *contact information*, not an identity. It is intentionally
    # non-unique: many customers under one reseller legitimately share a
    # contact address (e.g. the reseller owner's email). Login identity lives
    # in UserCredential.username (and RADIUS); ownership in reseller_id.
    email: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    phone: Mapped[str | None] = mapped_column(String(40))
    nin: Mapped[str | None] = mapped_column(String(11))

    date_of_birth: Mapped[date | None] = mapped_column(Date)
    gender: Mapped[Gender] = mapped_column(Enum(Gender), default=Gender.unknown)

    preferred_contact_method: Mapped[ContactMethod | None] = mapped_column(
        Enum(ContactMethod)
    )
    locale: Mapped[str | None] = mapped_column(String(16))
    timezone: Mapped[str | None] = mapped_column(String(64))

    # Contact address
    address_line1: Mapped[str | None] = mapped_column(String(120))
    address_line2: Mapped[str | None] = mapped_column(String(120))
    city: Mapped[str | None] = mapped_column(String(80))
    region: Mapped[str | None] = mapped_column(String(80))
    # Local Government Area — an NCC administrative unit, required per row of
    # the quarterly complaints return. Captured, never derived: validated
    # against ``ncc_location`` for its ``region`` at the capture boundary, and
    # left blank when unknown rather than guessed from address text.
    lga: Mapped[str | None] = mapped_column(String(80))
    postal_code: Mapped[str | None] = mapped_column(String(20))
    country_code: Mapped[str | None] = mapped_column(String(2))

    # Service location (POP site determines the NAS/IP pool for provisioning)
    pop_site_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pop_sites.id", ondelete="SET NULL"), index=True
    )

    # === Account Fields (from Subscriber + SubscriberAccount) ===
    subscriber_number: Mapped[str | None] = mapped_column(String(80), unique=True)
    account_number: Mapped[str | None] = mapped_column(String(80))
    account_start_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    status: Mapped[SubscriberStatus] = mapped_column(
        Enum(SubscriberStatus), default=SubscriberStatus.active
    )
    lifecycle_override_status: Mapped[SubscriberStatus | None] = mapped_column(
        Enum(SubscriberStatus)
    )
    lifecycle_override_reason: Mapped[str | None] = mapped_column(String(200))
    lifecycle_override_source: Mapped[str | None] = mapped_column(String(120))
    lifecycle_override_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    user_type: Mapped[UserType] = mapped_column(
        Enum(UserType), default=UserType.system_user
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    marketing_opt_in: Mapped[bool] = mapped_column(Boolean, default=False)

    # === Reseller ===
    reseller_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("resellers.id")
    )

    # === Billing Fields (from SubscriberAccount) ===
    tax_rate_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tax_rates.id")
    )
    # Account-level dunning policy override. Resolution order for an account is
    # account -> reseller -> offer -> general default (by billing mode).
    policy_set_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("policy_sets.id")
    )
    billing_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    captive_redirect_enabled: Mapped[bool] = mapped_column(Boolean, default=False)

    # Billing address (can differ from contact address)
    billing_name: Mapped[str | None] = mapped_column(String(160))
    billing_address_line1: Mapped[str | None] = mapped_column(String(160))
    billing_address_line2: Mapped[str | None] = mapped_column(String(120))
    billing_city: Mapped[str | None] = mapped_column(String(80))
    billing_region: Mapped[str | None] = mapped_column(String(80))
    billing_postal_code: Mapped[str | None] = mapped_column(String(20))
    billing_country_code: Mapped[str | None] = mapped_column(String(2))

    # Payment settings
    payment_method: Mapped[str | None] = mapped_column(String(80))
    deposit: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    billing_mode: Mapped[BillingMode] = mapped_column(
        Enum(BillingMode), default=BillingMode.prepaid
    )
    billing_day: Mapped[int | None] = mapped_column(Integer)  # Day of month for billing
    payment_due_days: Mapped[int | None] = mapped_column(Integer)  # Days after invoice
    grace_period_days: Mapped[int | None] = mapped_column(Integer)

    # Prepaid/balance settings
    min_balance: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    prepaid_low_balance_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    prepaid_deactivation_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    # Cached MRR (updated by nightly snapshot task)
    mrr_total: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2), default=Decimal("0")
    )

    # === Common Fields ===
    notes: Mapped[str | None] = mapped_column(Text)
    splynx_customer_id: Mapped[int | None] = mapped_column(Integer)
    # Direct link to the DotMac Omni CRM subscriber record. Replaces the
    # splynx_customer_id -> CRM external_id resolution chain for subscribers
    # that have it; backfilled from CRM, persisted lazily on first resolve.
    crm_subscriber_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # Party lifecycle status ported from CRM people.party_status.
    # PartyStatus values; NULL means the row predates the party
    # backfill and is treated as an ordinary subscriber.
    party_status: Mapped[str | None] = mapped_column(String(20))
    # B2B organization link (ported CRM party model, doc 02 ). Replaces
    # the flattened company_name/legal_name/tax_id/domain fields over time.
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), index=True
    )
    # CRM-owned account-matrix field: the sales order that created
    # this account. Real FK since the native sales migration landed the
    # sales_orders table; backfilled from CRM via link key 2.
    sales_order_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sales_orders.id"), index=True
    )
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

    # === Relationships ===
    reseller = relationship("Reseller", back_populates="subscribers")
    tax_rate = relationship("TaxRate")
    organization = relationship("Organization")
    party: Mapped[Party | None] = relationship(
        "Party", back_populates="subscriber_accounts"
    )

    addresses = relationship(
        "Address", back_populates="subscriber", cascade="all, delete-orphan"
    )
    custom_fields = relationship(
        "SubscriberCustomField",
        back_populates="subscriber",
        cascade="all, delete-orphan",
    )
    channels = relationship(
        "SubscriberChannel", back_populates="subscriber", cascade="all, delete-orphan"
    )
    contacts = relationship(
        "SubscriberContact", back_populates="subscriber", cascade="all, delete-orphan"
    )
    nin_verifications = relationship(
        "SubscriberNINVerification",
        back_populates="subscriber",
        cascade="all, delete-orphan",
        order_by="SubscriberNINVerification.created_at.desc()",
    )

    # Service relationships
    subscriptions = relationship("Subscription", back_populates="subscriber")
    access_credentials = relationship("AccessCredential", back_populates="subscriber")
    service_orders = relationship("ServiceOrder", back_populates="subscriber")
    cpe_devices = relationship("CPEDevice", back_populates="subscriber")
    ip_assignments = relationship("IPAssignment", back_populates="subscriber")
    ont_assignments = relationship("OntAssignment", back_populates="subscriber")
    dunning_cases = relationship("DunningCase", back_populates="subscriber")

    @property
    def full_name(self) -> str:
        """Return full name."""
        return f"{self.first_name} {self.last_name}"

    @property
    def is_business(self) -> bool:
        return self.category == SubscriberCategory.business

    @property
    def business_name(self) -> str:
        return (
            self.company_name
            or self.display_name
            or self.full_name.strip()
            or self.email
        )

    @property
    def name(self) -> str:
        return (
            self.business_name
            if self.is_business
            else (self.full_name.strip() or self.email)
        )

    @property
    def category(self) -> SubscriberCategory:
        """Normalized subscriber category stored in metadata."""
        raw = (self.metadata_ or {}).get("subscriber_category")
        if isinstance(raw, str):
            try:
                return SubscriberCategory(raw)
            except ValueError:
                pass
        return SubscriberCategory.residential

    @category.setter
    def category(self, value: SubscriberCategory | str | None) -> None:
        if isinstance(value, SubscriberCategory):
            normalized = value.value
        elif isinstance(value, str):
            normalized = value.strip().lower()
            try:
                normalized = SubscriberCategory(normalized).value
            except ValueError:
                normalized = SubscriberCategory.residential.value
        else:
            normalized = SubscriberCategory.residential.value

        metadata = dict(self.metadata_ or {})
        metadata["subscriber_category"] = normalized
        self.metadata_ = metadata


class SubscriberChannel(Base):
    """Additional communication channels for a subscriber."""

    __tablename__ = "subscriber_channels"
    __table_args__ = (
        UniqueConstraint(
            "subscriber_id",
            "channel_type",
            "address",
            name="uq_subscriber_channels_subscriber_type_address",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=False
    )
    channel_type: Mapped[ChannelType] = mapped_column(Enum(ChannelType), nullable=False)
    address: Mapped[str] = mapped_column(String(255), nullable=False)
    label: Mapped[str | None] = mapped_column(String(60))
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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

    subscriber = relationship("Subscriber", back_populates="channels")


class SubscriberContact(Base):
    """Linked contact for a subscriber account.

    Contacts are account-level people, not independent subscriber accounts.
    """

    __tablename__ = "subscriber_contacts"
    __table_args__ = (
        CheckConstraint(
            "(person_party_id IS NULL AND party_bound_at IS NULL AND "
            "party_binding_source IS NULL AND party_binding_reason IS NULL) OR "
            "(person_party_id IS NOT NULL AND party_bound_at IS NOT NULL AND "
            "party_binding_source IS NOT NULL AND "
            "party_binding_reason IS NOT NULL AND "
            "length(trim(party_binding_source)) > 0 AND "
            "length(trim(party_binding_reason)) > 0)",
            name="ck_subscriber_contacts_party_binding_evidence",
        ),
        UniqueConstraint(
            "subscriber_id",
            "person_party_id",
            name="uq_subscriber_contacts_subscriber_person_party",
        ),
        Index("ix_subscriber_contacts_subscriber_id", "subscriber_id"),
        Index("ix_subscriber_contacts_email", "email"),
        Index("ix_subscriber_contacts_phone", "phone"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    person_party_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("parties.id", ondelete="RESTRICT")
    )
    party_bound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    party_binding_source: Mapped[str | None] = mapped_column(String(80))
    party_binding_reason: Mapped[str | None] = mapped_column(Text)
    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscribers.id", ondelete="CASCADE"),
        nullable=False,
    )
    full_name: Mapped[str | None] = mapped_column(String(160))
    phone: Mapped[str | None] = mapped_column(String(40))
    email: Mapped[str | None] = mapped_column(String(255))
    whatsapp: Mapped[str | None] = mapped_column(String(80))
    facebook: Mapped[str | None] = mapped_column(String(160))
    instagram: Mapped[str | None] = mapped_column(String(160))
    x_handle: Mapped[str | None] = mapped_column(String(160))
    telegram: Mapped[str | None] = mapped_column(String(160))
    linkedin: Mapped[str | None] = mapped_column(String(160))
    other_social: Mapped[str | None] = mapped_column(Text)
    relationship: Mapped[str | None] = mapped_column(String(80))
    contact_type: Mapped[str] = mapped_column(String(40), default="general")
    is_billing_contact: Mapped[bool] = mapped_column(Boolean, default=False)
    is_authorized: Mapped[bool] = mapped_column(Boolean, default=False)
    receives_notifications: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    subscriber = sa_orm.relationship("Subscriber", back_populates="contacts")
    person_party = sa_orm.relationship("Party", foreign_keys=[person_party_id])
    relationship_projections = sa_orm.relationship(
        "SubscriberContactRelationshipProjection",
        back_populates="subscriber_contact",
        cascade="all, delete-orphan",
    )
    contact_point_projections = sa_orm.relationship(
        "SubscriberContactPointProjection",
        back_populates="subscriber_contact",
        cascade="all, delete-orphan",
    )


class SubscriberNINVerification(Base):
    """Mono NIN verification attempts for a subscriber."""

    __tablename__ = "subscriber_nin_verifications"
    __table_args__ = (
        Index("ix_subscriber_nin_verifications_subscriber_id", "subscriber_id"),
        Index("ix_subscriber_nin_verifications_nin", "nin"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscribers.id", ondelete="CASCADE"),
        nullable=False,
    )
    nin: Mapped[str] = mapped_column(String(11), nullable=False)
    status: Mapped[NINVerificationStatus] = mapped_column(
        Enum(
            NINVerificationStatus,
            name="subscriber_nin_verification_status",
            values_callable=lambda enum_cls: [item.value for item in enum_cls],
        ),
        default=NINVerificationStatus.pending,
        nullable=False,
    )
    is_match: Mapped[bool | None] = mapped_column(Boolean)
    match_score: Mapped[int | None] = mapped_column(Integer)
    mono_response: Mapped[dict | None] = mapped_column(JSONB)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    subscriber = relationship("Subscriber", back_populates="nin_verifications")


class SubscriberCustomField(Base):
    """Custom fields for subscribers."""

    __tablename__ = "subscriber_custom_fields"
    __table_args__ = (
        UniqueConstraint(
            "subscriber_id", "key", name="uq_subscriber_custom_fields_subscriber_key"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(120), nullable=False)
    value_type: Mapped[SettingValueType] = mapped_column(
        Enum(SettingValueType), default=SettingValueType.string
    )
    value_text: Mapped[str | None] = mapped_column(Text)
    value_json: Mapped[dict | None] = mapped_column(JSON)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    subscriber = relationship("Subscriber", back_populates="custom_fields")


class Address(Base):
    """Service/installation addresses for subscribers."""

    __tablename__ = "addresses"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=False
    )
    tax_rate_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tax_rates.id")
    )
    address_type: Mapped[AddressType] = mapped_column(
        Enum(AddressType), default=AddressType.service
    )
    label: Mapped[str | None] = mapped_column(String(120))
    address_line1: Mapped[str] = mapped_column(String(120), nullable=False)
    address_line2: Mapped[str | None] = mapped_column(String(120))
    city: Mapped[str | None] = mapped_column(String(80))
    region: Mapped[str | None] = mapped_column(String(80))
    # Local Government Area — see ``Subscriber.lga``. Captured and validated
    # against ``region``; blank when unknown.
    lga: Mapped[str | None] = mapped_column(String(80))
    postal_code: Mapped[str | None] = mapped_column(String(20))
    country_code: Mapped[str | None] = mapped_column(String(2))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    geom = mapped_column(Geometry("POINT", srid=4326), nullable=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    subscriber = relationship("Subscriber", back_populates="addresses")
    tax_rate = relationship("TaxRate")


# --- Compatibility aliases ---
# SubscriberAccount is now represented by Subscriber.
SubscriberAccount = Subscriber


class ResellerUser(Base):
    """Reseller user linkage model.

    Maps to the active reseller_users table while preserving
    subscriber_id/person_id compatibility across callers. Reviewed rows may
    also link one Person Party and one explicit reseller PartyMembership;
    existing authentication remains authoritative until cutover.
    """

    __tablename__ = "reseller_users"
    __table_args__ = (
        CheckConstraint(
            "(person_party_id IS NULL AND party_membership_id IS NULL AND "
            "party_bound_at IS NULL AND party_binding_source IS NULL AND "
            "party_binding_reason IS NULL) OR "
            "(person_party_id IS NOT NULL AND party_membership_id IS NOT NULL AND "
            "party_bound_at IS NOT NULL AND party_binding_source IS NOT NULL AND "
            "party_binding_reason IS NOT NULL AND "
            "length(trim(party_binding_source)) > 0 AND "
            "length(trim(party_binding_reason)) > 0)",
            name="ck_reseller_users_party_binding_evidence",
        ),
        UniqueConstraint(
            "reseller_id",
            "person_party_id",
            name="uq_reseller_users_reseller_person_party",
        ),
        UniqueConstraint(
            "party_membership_id",
            name="uq_reseller_users_party_membership_id",
        ),
        {"extend_existing": True},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    person_party_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("parties.id", ondelete="RESTRICT")
    )
    party_membership_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("party_memberships.id", ondelete="RESTRICT")
    )
    party_bound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    party_binding_source: Mapped[str | None] = mapped_column(String(80))
    party_binding_reason: Mapped[str | None] = mapped_column(Text)
    # DB column name is person_id in legacy schemas.
    subscriber_id: Mapped[uuid.UUID | None] = mapped_column(
        "person_id",
        UUID(as_uuid=True),
        ForeignKey("subscribers.id", ondelete="CASCADE"),
    )
    # Backwards-compatible alias used by older code/tests.
    person_id: Mapped[uuid.UUID | None] = synonym("subscriber_id")
    reseller_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("resellers.id", ondelete="CASCADE")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Layer 3: a reseller portal login as a first-class identity rather than a
    # fake Subscriber. These carry the login's own contact/display info so no
    # subscriber row is needed; subscriber_id (person_id) above is kept only for
    # historical linkage during the transition and is now optional.
    email: Mapped[str | None] = mapped_column(String(255))
    full_name: Mapped[str | None] = mapped_column(String(160))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    person_party = relationship("Party", foreign_keys=[person_party_id])
    party_membership = relationship(
        "PartyMembership", foreign_keys=[party_membership_id]
    )
