import enum
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class ServiceType(enum.Enum):
    residential = "residential"
    business = "business"


class AccessType(enum.Enum):
    fiber = "fiber"
    fixed_wireless = "fixed_wireless"
    dsl = "dsl"
    cable = "cable"


class PriceBasis(enum.Enum):
    flat = "flat"
    usage = "usage"
    tiered = "tiered"
    hybrid = "hybrid"


class BillingCycle(enum.Enum):
    daily = "daily"
    weekly = "weekly"
    monthly = "monthly"
    quarterly = "quarterly"
    annual = "annual"


def billing_cycle_noun(cycle: "BillingCycle | None") -> str:
    """Human cadence noun for invoice line descriptions (e.g. 'quarterly')."""
    return {
        BillingCycle.daily: "daily",
        BillingCycle.weekly: "weekly",
        BillingCycle.monthly: "monthly",
        BillingCycle.quarterly: "quarterly",
        BillingCycle.annual: "annual",
    }.get(cycle or BillingCycle.monthly, "monthly")


def billing_cycle_suffix(cycle: "BillingCycle | None") -> str:
    """Price-summary suffix for a cadence (e.g. '/qtr', '/yr')."""
    return {
        BillingCycle.daily: "/day",
        BillingCycle.weekly: "/wk",
        BillingCycle.monthly: "/mo",
        BillingCycle.quarterly: "/qtr",
        BillingCycle.annual: "/yr",
    }.get(cycle or BillingCycle.monthly, "/mo")


class ContractTerm(enum.Enum):
    month_to_month = "month_to_month"
    twelve_month = "twelve_month"
    twentyfour_month = "twentyfour_month"


class PriceType(enum.Enum):
    recurring = "recurring"
    one_time = "one_time"
    usage = "usage"


class PriceUnit(enum.Enum):
    day = "day"
    week = "week"
    month = "month"
    year = "year"
    gb = "gb"
    tb = "tb"
    item = "item"


class GuaranteedSpeedType(enum.Enum):
    none = "none"
    relative = "relative"
    fixed = "fixed"


class ProrationPolicy(enum.Enum):
    immediate = "immediate"
    next_cycle = "next_cycle"
    none = "none"


class SuspensionAction(enum.Enum):
    none = "none"
    throttle = "throttle"
    suspend = "suspend"
    reject = "reject"


class RefundPolicy(enum.Enum):
    none = "none"
    prorated = "prorated"
    full_within_days = "full_within_days"


class DunningAction(enum.Enum):
    notify = "notify"
    throttle = "throttle"
    suspend = "suspend"
    reject = "reject"


class AddOnType(enum.Enum):
    static_ip = "static_ip"
    router_rental = "router_rental"
    install_fee = "install_fee"
    premium_support = "premium_support"
    extra_ip = "extra_ip"
    managed_wifi = "managed_wifi"
    custom = "custom"


class ServiceHandoffType(enum.Enum):
    """How the service is handed to the customer at their edge.

    A dedicated circuit is the same product whichever of these it uses — only
    the handoff differs, so these are NOT separate plan families. Transit is
    dedicated delivered over BGP rather than a static address; a layer-2 clear
    channel is dedicated capacity with no IP layer at all, sold to a party who
    runs their own addressing across it.
    """

    #: Our address space, statically assigned. The default for internet access.
    static_ip = "static_ip"
    #: BGP session to the customer's own ASN, announcing their own prefixes.
    bgp = "bgp"
    #: Point-to-point layer-2 clear channel. No IP is provided or routed.
    layer2_clear_channel = "layer2_clear_channel"


class PlanCategory(enum.Enum):
    internet = "internet"
    recurring = "recurring"
    one_time = "one_time"
    bundle = "bundle"


class OfferStatus(enum.Enum):
    active = "active"
    inactive = "inactive"
    archived = "archived"


class BillingMode(enum.Enum):
    prepaid = "prepaid"
    postpaid = "postpaid"


PLAN_FAMILY_VALUES = ("unlimited", "dedicated", "home_flex")


class AccessState(enum.Enum):
    """Network access state — single source of truth for what RADIUS does
    with a subscriber. See docs/FINANCIAL_ACCESS_ENFORCEMENT.md.

    Derived from ``SubscriptionStatus`` + persisted enforcement locks through
    the canonical walled-garden policy
    via ``app.services.radius_access_state.derive_access_state``. Stored as
    a string column rather than a PG enum so future states (e.g.
    ``throttled``, ``trial_expired``) can be added by code change alone.

    Maps to the canonical RADIUS projection:
      active     → dotmac-active     (normal customer)
      suspended  → dotmac-suspended  (hard block, Auth-Type := Reject)
      captive    → dotmac-captive    (soft block, captive portal)
      terminated → no radusergroup row (auth not-found)
    """

    active = "active"
    suspended = "suspended"
    captive = "captive"
    terminated = "terminated"


class SubscriptionStatus(enum.Enum):
    """Service/subscription status — mirrors the imported service status lifecycle.

    Imported status mapping (1:1):
      pending  → pending (awaiting activation / provisioning)
      active   → active (service running, can connect)
      blocked  → blocked (temporarily blocked — non-payment)
      stopped  → stopped (manually paused by admin, different from block)
      disabled → disabled (administratively paused; explicit re-enable required)
      hidden   → hidden (historical, not visible to customer)
      deleted  → canceled (soft-deleted, record preserved)

    DotMac-only statuses:
      suspended — generic suspension (local origin)
      archived  — generic archive (local origin)
      expired   — contract/prepaid period ended
    """

    pending = "pending"  # Awaiting activation / provisioning
    active = "active"  # Service running, subscriber can connect
    blocked = "blocked"  # Temporarily blocked
    suspended = "suspended"  # Generic suspension (DotMac-native)
    stopped = "stopped"  # Manually paused by admin
    disabled = "disabled"  # Administratively paused; explicit re-enable required
    hidden = "hidden"  # Not visible to customer
    archived = "archived"  # Generic archive (DotMac-native)
    canceled = "canceled"  # Soft-deleted / fully terminated
    expired = "expired"  # Contract/prepaid period ended


class NasVendor(enum.Enum):
    """Supported NAS device vendors."""

    mikrotik = "mikrotik"
    huawei = "huawei"
    ubiquiti = "ubiquiti"
    cisco = "cisco"
    juniper = "juniper"
    cambium = "cambium"
    nokia = "nokia"
    zte = "zte"
    other = "other"


class ConnectionType(enum.Enum):
    """Network connection/authentication protocol type."""

    pppoe = "pppoe"  # Point-to-Point Protocol over Ethernet
    dhcp = "dhcp"  # Dynamic Host Configuration Protocol (no auth)
    ipoe = "ipoe"  # IP over Ethernet (DHCP + RADIUS Option 82)
    static = "static"  # Static IP assignment
    hotspot = "hotspot"  # Web portal login (MikroTik specific)


class NasDeviceStatus(enum.Enum):
    """NAS device operational status."""

    active = "active"
    maintenance = "maintenance"
    offline = "offline"
    decommissioned = "decommissioned"


class HealthStatus(enum.Enum):
    unknown = "unknown"
    healthy = "healthy"
    degraded = "degraded"
    unhealthy = "unhealthy"


class ProvisioningLogStatus(enum.Enum):
    pending = "pending"
    running = "running"
    success = "success"
    failed = "failed"
    timeout = "timeout"


class ExecutionMethod(enum.Enum):
    ssh = "ssh"
    api = "api"
    radius_coa = "radius_coa"


class DiscountType(enum.Enum):
    percentage = "percentage"
    # Legacy value kept for backward compatibility with older imported data.
    percent = "percent"
    fixed = "fixed"


class ConfigBackupMethod(enum.Enum):
    """Methods for backing up device configuration."""

    ssh = "ssh"
    api = "api"
    tftp = "tftp"
    ftp = "ftp"
    snmp = "snmp"


class ProvisioningAction(enum.Enum):
    """Types of provisioning actions."""

    create_user = "create_user"
    delete_user = "delete_user"
    suspend_user = "suspend_user"
    unsuspend_user = "unsuspend_user"
    change_speed = "change_speed"
    change_ip = "change_ip"
    reset_session = "reset_session"
    get_user_info = "get_user_info"
    backup_config = "backup_config"
    restore_config = "restore_config"


class RegionZone(Base):
    __tablename__ = "region_zones"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    code: Mapped[str | None] = mapped_column(String(40))
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    offers = relationship("CatalogOffer", back_populates="region_zone")


class PolicySet(Base):
    __tablename__ = "policy_sets"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    proration_policy: Mapped[ProrationPolicy] = mapped_column(
        Enum(ProrationPolicy), default=ProrationPolicy.immediate
    )
    downgrade_policy: Mapped[ProrationPolicy] = mapped_column(
        Enum(ProrationPolicy), default=ProrationPolicy.next_cycle
    )
    trial_days: Mapped[int | None] = mapped_column(Integer)
    trial_card_required: Mapped[bool] = mapped_column(Boolean, default=False)
    grace_days: Mapped[int | None] = mapped_column(Integer)
    suspension_action: Mapped[SuspensionAction] = mapped_column(
        Enum(SuspensionAction), default=SuspensionAction.suspend
    )
    refund_policy: Mapped[RefundPolicy] = mapped_column(
        Enum(RefundPolicy), default=RefundPolicy.none
    )
    refund_window_days: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    dunning_steps = relationship("PolicyDunningStep", back_populates="policy_set")
    offers = relationship("CatalogOffer", back_populates="policy_set")


class PolicyDunningStep(Base):
    __tablename__ = "policy_dunning_steps"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    policy_set_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("policy_sets.id"), nullable=False
    )
    day_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[DunningAction] = mapped_column(Enum(DunningAction), nullable=False)
    note: Mapped[str | None] = mapped_column(String(200))

    policy_set = relationship("PolicySet", back_populates="dunning_steps")


class UsageAllowance(Base):
    __tablename__ = "usage_allowances"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    included_gb: Mapped[int | None] = mapped_column(Integer)
    overage_rate: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    overage_cap_gb: Mapped[int | None] = mapped_column(Integer)
    throttle_rate_mbps: Mapped[int | None] = mapped_column(Integer)
    # Unused allowance carries into next period's quota bucket (capped at one
    # period's included_gb). Sourced from imported fup_limits.rollover_data.
    rollover_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    offers = relationship("CatalogOffer", back_populates="usage_allowance")


class SlaProfile(Base):
    __tablename__ = "sla_profiles"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    uptime_percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    response_time_hours: Mapped[int | None] = mapped_column(Integer)
    resolution_time_hours: Mapped[int | None] = mapped_column(Integer)
    credit_percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    offers = relationship("CatalogOffer", back_populates="sla_profile")


class AddOn(Base):
    __tablename__ = "add_ons"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    addon_type: Mapped[AddOnType] = mapped_column(
        Enum(AddOnType), default=AddOnType.custom
    )
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # IP add-on metadata. ip_is_public distinguishes a billable public block
    # from the default private /32 every account ships with (which is never an
    # add-on); ip_prefix_length is the block size (e.g. 29, 30, 32).
    ip_is_public: Mapped[bool] = mapped_column(Boolean, default=False)
    ip_prefix_length: Mapped[int | None] = mapped_column(Integer)

    # Data top-up: GB granted to the subscription's quota bucket on purchase
    # (null for non-data add-ons). Sourced from imported cap_tariff.
    grant_gb: Mapped[int | None] = mapped_column(Integer)
    # Top-up validity in days; null means it expires at the end of the billing
    # period it was bought in. Sourced from imported cap_tariff.validity.
    validity_days: Mapped[int | None] = mapped_column(Integer)

    # Provenance for the legacy importer — "custom:8" / "one_time:3" /
    # "cap_tariff:1". Unique so re-running the import updates rather than
    # duplicates.
    splynx_source: Mapped[str | None] = mapped_column(String(40), unique=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    offer_links = relationship("OfferAddOn", back_populates="add_on")
    prices = relationship("AddOnPrice", back_populates="add_on")


class CatalogOffer(Base):
    __tablename__ = "catalog_offers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    code: Mapped[str | None] = mapped_column(String(60))
    service_type: Mapped[ServiceType] = mapped_column(Enum(ServiceType), nullable=False)
    access_type: Mapped[AccessType] = mapped_column(Enum(AccessType), nullable=False)
    price_basis: Mapped[PriceBasis] = mapped_column(Enum(PriceBasis), nullable=False)
    billing_cycle: Mapped[BillingCycle] = mapped_column(
        Enum(BillingCycle), default=BillingCycle.monthly
    )
    billing_mode: Mapped[BillingMode] = mapped_column(
        Enum(BillingMode), default=BillingMode.prepaid
    )
    contract_term: Mapped[ContractTerm] = mapped_column(
        Enum(ContractTerm), default=ContractTerm.month_to_month
    )
    region_zone_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("region_zones.id")
    )
    usage_allowance_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("usage_allowances.id")
    )
    sla_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sla_profiles.id")
    )
    policy_set_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("policy_sets.id")
    )
    splynx_tariff_id: Mapped[int | None] = mapped_column(Integer)
    splynx_service_name: Mapped[str | None] = mapped_column(String(160))
    splynx_tax_id: Mapped[int | None] = mapped_column(Integer)
    with_vat: Mapped[bool] = mapped_column(Boolean, default=False)
    vat_percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    speed_download_mbps: Mapped[int | None] = mapped_column(Integer)
    speed_upload_mbps: Mapped[int | None] = mapped_column(Integer)
    guaranteed_speed_limit_at: Mapped[int | None] = mapped_column(Integer)
    guaranteed_speed: Mapped[GuaranteedSpeedType] = mapped_column(
        Enum(GuaranteedSpeedType), default=GuaranteedSpeedType.none
    )
    aggregation: Mapped[int | None] = mapped_column(Integer)
    priority: Mapped[str | None] = mapped_column(String(40))
    available_for_services: Mapped[bool] = mapped_column(Boolean, default=True)
    show_on_customer_portal: Mapped[bool] = mapped_column(Boolean, default=True)
    olt_profile_auto_sync_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    plan_category: Mapped[PlanCategory] = mapped_column(
        Enum(PlanCategory, name="plancategory", create_constraint=False),
        default=PlanCategory.internet,
        server_default="internet",
    )
    hide_on_admin_portal: Mapped[bool] = mapped_column(Boolean, default=False)
    service_description: Mapped[str | None] = mapped_column(Text)
    burst_profile: Mapped[str | None] = mapped_column(String(120))
    prepaid_period: Mapped[str | None] = mapped_column(String(40))
    plan_family: Mapped[str | None] = mapped_column(String(40))
    allowed_change_plan_ids: Mapped[str | None] = mapped_column(Text)
    status: Mapped[OfferStatus] = mapped_column(
        Enum(OfferStatus), default=OfferStatus.active
    )
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Default ONT provisioning profile for fiber offers
    default_ont_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ont_provisioning_profiles.id")
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    region_zone = relationship("RegionZone", back_populates="offers")
    usage_allowance = relationship("UsageAllowance", back_populates="offers")
    sla_profile = relationship("SlaProfile", back_populates="offers")
    policy_set = relationship("PolicySet", back_populates="offers")
    prices = relationship("OfferPrice", back_populates="offer")
    add_on_links = relationship("OfferAddOn", back_populates="offer")
    radius_profiles = relationship("OfferRadiusProfile", back_populates="offer")
    subscriptions = relationship("Subscription", back_populates="offer")
    versions = relationship("OfferVersion", back_populates="offer")
    default_ont_profile = relationship("OntProvisioningProfile")

    # Availability controls from imported tariff junction tables.
    reseller_availability = relationship(
        "OfferResellerAvailability",
        back_populates="offer",
        cascade="all, delete-orphan",
    )
    location_availability = relationship(
        "OfferLocationAvailability",
        back_populates="offer",
        cascade="all, delete-orphan",
    )
    category_availability = relationship(
        "OfferCategoryAvailability",
        back_populates="offer",
        cascade="all, delete-orphan",
    )
    billing_mode_availability = relationship(
        "OfferBillingModeAvailability",
        back_populates="offer",
        cascade="all, delete-orphan",
    )


class OfferVersion(Base):
    __tablename__ = "offer_versions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    offer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("catalog_offers.id"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    code: Mapped[str | None] = mapped_column(String(60))
    service_type: Mapped[ServiceType] = mapped_column(Enum(ServiceType), nullable=False)
    access_type: Mapped[AccessType] = mapped_column(Enum(AccessType), nullable=False)
    price_basis: Mapped[PriceBasis] = mapped_column(Enum(PriceBasis), nullable=False)
    billing_cycle: Mapped[BillingCycle] = mapped_column(
        Enum(BillingCycle), default=BillingCycle.monthly
    )
    contract_term: Mapped[ContractTerm] = mapped_column(
        Enum(ContractTerm), default=ContractTerm.month_to_month
    )
    region_zone_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("region_zones.id")
    )
    usage_allowance_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("usage_allowances.id")
    )
    sla_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sla_profiles.id")
    )
    policy_set_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("policy_sets.id")
    )
    status: Mapped[OfferStatus] = mapped_column(
        Enum(OfferStatus), default=OfferStatus.active
    )
    description: Mapped[str | None] = mapped_column(Text)
    effective_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    effective_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    offer = relationship("CatalogOffer", back_populates="versions")
    region_zone = relationship("RegionZone")
    usage_allowance = relationship("UsageAllowance")
    sla_profile = relationship("SlaProfile")
    policy_set = relationship("PolicySet")
    prices = relationship("OfferVersionPrice", back_populates="offer_version")
    subscriptions = relationship("Subscription", back_populates="offer_version")


class OfferVersionPrice(Base):
    __tablename__ = "offer_version_prices"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    offer_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("offer_versions.id"), nullable=False
    )
    price_type: Mapped[PriceType] = mapped_column(
        Enum(PriceType), default=PriceType.recurring
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="NGN")
    billing_cycle: Mapped[BillingCycle | None] = mapped_column(Enum(BillingCycle))
    unit: Mapped[PriceUnit | None] = mapped_column(Enum(PriceUnit))
    description: Mapped[str | None] = mapped_column(String(200))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    offer_version = relationship("OfferVersion", back_populates="prices")


class OfferAddOn(Base):
    __tablename__ = "offer_add_ons"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    offer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("catalog_offers.id"), nullable=False
    )
    add_on_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("add_ons.id"), nullable=False
    )
    is_required: Mapped[bool] = mapped_column(Boolean, default=False)
    min_quantity: Mapped[int | None] = mapped_column(Integer)
    max_quantity: Mapped[int | None] = mapped_column(Integer)

    offer = relationship("CatalogOffer", back_populates="add_on_links")
    add_on = relationship("AddOn", back_populates="offer_links")


class OfferPrice(Base):
    __tablename__ = "offer_prices"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    offer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("catalog_offers.id"), nullable=False
    )
    price_type: Mapped[PriceType] = mapped_column(
        Enum(PriceType), default=PriceType.recurring
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="NGN")
    billing_cycle: Mapped[BillingCycle | None] = mapped_column(Enum(BillingCycle))
    unit: Mapped[PriceUnit | None] = mapped_column(Enum(PriceUnit))
    description: Mapped[str | None] = mapped_column(String(200))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    offer = relationship("CatalogOffer", back_populates="prices")


class AddOnPrice(Base):
    __tablename__ = "add_on_prices"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    add_on_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("add_ons.id"), nullable=False
    )
    price_type: Mapped[PriceType] = mapped_column(
        Enum(PriceType), default=PriceType.recurring
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="NGN")
    billing_cycle: Mapped[BillingCycle | None] = mapped_column(Enum(BillingCycle))
    unit: Mapped[PriceUnit | None] = mapped_column(Enum(PriceUnit))
    description: Mapped[str | None] = mapped_column(String(200))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    add_on = relationship("AddOn", back_populates="prices")


class BandwidthPriceBand(Base):
    """One speed band and its rate per Mbps, for rule-driven bandwidth quoting.

    Dedicated circuits are sold at arbitrary speeds, so pricing them from a
    ``CatalogOffer`` row per speed is what produced a catalog with duplicate
    speeds at incompatible prices — and a 500 Mbps circuit priced below a
    300 Mbps one. A band set replaces those rows with a rule sales can quote
    from at any speed.

    Bands are half-open ``[speed_from_mbps, speed_to_mbps)`` with an open top
    band (``speed_to_mbps IS NULL``) meaning "and above". A partial unique
    index stops two live bands starting at the same speed, and
    ``bandwidth_pricing.validate_band_set`` rejects overlaps, gaps, a closed
    top and a second open top — so "the rate at N Mbps" has exactly one answer.

    No effective-dating: a band set is a *sales aid*, not a contract. The
    contracted figure is captured on ``QuoteLineItem.unit_price`` when the
    quote is raised, so re-rating a band never rewrites an issued quote.
    """

    __tablename__ = "bandwidth_price_bands"
    __table_args__ = (
        CheckConstraint(
            "plan_family IN ('unlimited', 'dedicated', 'home_flex')",
            name="ck_bandwidth_price_bands_family_vocab",
        ),
        CheckConstraint("speed_from_mbps >= 0", name="ck_bandwidth_price_bands_from"),
        CheckConstraint(
            "speed_to_mbps IS NULL OR speed_to_mbps > speed_from_mbps",
            name="ck_bandwidth_price_bands_range",
        ),
        CheckConstraint(
            "rate_per_mbps >= 0", name="ck_bandwidth_price_bands_rate_sign"
        ),
        # Partial on is_active: two live bands must not start at the same
        # speed, but retiring a band and re-cutting the ladder from the same
        # boundary is ordinary repricing and must stay possible.
        Index(
            "uq_bandwidth_price_bands_family_from",
            "plan_family",
            "speed_from_mbps",
            unique=True,
            sqlite_where=text("is_active = 1"),
            postgresql_where=text("is_active"),
        ),
        Index("ix_bandwidth_price_bands_family", "plan_family", "speed_from_mbps"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    plan_family: Mapped[str] = mapped_column(String(40), nullable=False)
    speed_from_mbps: Mapped[int] = mapped_column(Integer, nullable=False)
    speed_to_mbps: Mapped[int | None] = mapped_column(Integer)
    rate_per_mbps: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="NGN")
    description: Mapped[str | None] = mapped_column(String(200))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class Subscription(Base):
    __tablename__ = "subscriptions"
    __table_args__ = (
        Index(
            "ix_subscriptions_subscriber_id_status",
            "subscriber_id",
            "status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=False
    )
    offer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("catalog_offers.id"), nullable=False
    )
    offer_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("offer_versions.id")
    )
    service_address_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("addresses.id")
    )
    bundle_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscription_bundles.id"),
        nullable=True,
        index=True,
    )

    # Provisioning - which NAS handles this subscription
    provisioning_nas_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("nas_devices.id")
    )
    # Override RADIUS profile (instead of offer's default)
    radius_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("radius_profiles.id")
    )

    status: Mapped[SubscriptionStatus] = mapped_column(
        Enum(SubscriptionStatus), default=SubscriptionStatus.pending
    )
    # access_state is the RADIUS-facing state, derived from `status` +
    # canonical persisted access restriction.
    # See docs/FINANCIAL_ACCESS_ENFORCEMENT.md.
    access_state: Mapped[str | None] = mapped_column(String(20))
    billing_mode: Mapped[BillingMode] = mapped_column(
        Enum(BillingMode), default=BillingMode.prepaid
    )
    contract_term: Mapped[ContractTerm] = mapped_column(
        Enum(ContractTerm), default=ContractTerm.month_to_month
    )
    # Contracted billing cadence owned by this subscription (SOT). Nullable:
    # NULL => inherit the offer/version price cadence (fallback). New contracts
    # captured from the sales order set this explicitly; existing rows are
    # backfilled to their currently-resolved cadence by migration 310.
    billing_cycle: Mapped[BillingCycle | None] = mapped_column(Enum(BillingCycle))
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_billing_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(String(200))
    splynx_service_id: Mapped[int | None] = mapped_column(Integer)
    router_id: Mapped[int | None] = mapped_column(Integer)
    service_description: Mapped[str | None] = mapped_column(Text)
    quantity: Mapped[int | None] = mapped_column(Integer)
    unit: Mapped[str | None] = mapped_column(String(40))
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    discount: Mapped[bool] = mapped_column(Boolean, default=False)
    discount_value: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    discount_type: Mapped[DiscountType | None] = mapped_column(
        Enum(DiscountType, values_callable=lambda x: [e.value for e in x]),
    )
    discount_start_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    discount_end_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    discount_description: Mapped[str | None] = mapped_column(String(512), nullable=True)
    service_status_raw: Mapped[str | None] = mapped_column(String(40))
    login: Mapped[str | None] = mapped_column(String(120))
    ipv4_address: Mapped[str | None] = mapped_column(String(64))
    ipv6_address: Mapped[str | None] = mapped_column(String(128))
    # OBSERVED framed address from live RADIUS accounting (display/diagnostics
    # only). Kept SEPARATE from ipv4_address/ipv6_address, which are the
    # DESIRED/served IP owned by the IP assignment + connectivity reconciler.
    # Splitting observed from desired stops the live IP overwriting the desired
    # IP and being re-emitted by the RADIUS sweep — see
    # docs/FINANCIAL_ACCESS_ENFORCEMENT.md.
    last_seen_framed_ipv4: Mapped[str | None] = mapped_column(String(64))
    last_seen_framed_ipv6: Mapped[str | None] = mapped_column(String(128))
    mac_address: Mapped[str | None] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    subscriber = relationship("Subscriber", back_populates="subscriptions")
    offer = relationship("CatalogOffer", back_populates="subscriptions")
    offer_version = relationship("OfferVersion", back_populates="subscriptions")
    service_address = relationship("Address")
    provisioning_nas_device = relationship("NasDevice", back_populates="subscriptions")
    radius_profile = relationship("RadiusProfile", back_populates="subscriptions")
    add_ons = relationship("SubscriptionAddOn", back_populates="subscription")
    service_orders = relationship("ServiceOrder", back_populates="subscription")
    lifecycle_events = relationship(
        "SubscriptionLifecycleEvent",
        back_populates="subscription",
        passive_deletes=True,
    )
    ip_assignments = relationship("IPAssignment", back_populates="subscription")
    bandwidth_samples = relationship("BandwidthSample", back_populates="subscription")
    usage_charges = relationship("UsageCharge", back_populates="subscription")
    quota_buckets = relationship("QuotaBucket", back_populates="subscription")


class SubscriptionBundle(Base):
    __tablename__ = "subscription_bundles"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=False, index=True
    )
    label: Mapped[str | None] = mapped_column(String(160))
    anchor_subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscriptions.id"), nullable=True
    )
    is_dedicated: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(32), default="active")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class SubscriptionAddOn(Base):
    __tablename__ = "subscription_add_ons"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscriptions.id"), nullable=False
    )
    add_on_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("add_ons.id"), nullable=False
    )
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    account_adjustment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account_adjustments.id", ondelete="RESTRICT"),
        nullable=True,
        unique=True,
    )
    purchase_preview_fingerprint: Mapped[str | None] = mapped_column(String(64))
    purchase_idempotency_key: Mapped[str | None] = mapped_column(String(120))

    subscription = relationship("Subscription", back_populates="add_ons")
    add_on = relationship("AddOn")
    account_adjustment = relationship(
        "AccountAdjustment", back_populates="subscription_add_on"
    )


class ServiceHandoff(Base):
    """How one subscription is delivered at the customer edge, for the NOC.

    Transit and layer-2 clear channel are not separate products: both are a
    dedicated circuit differing only in what we hand over. Modelling them as
    plan families would fork the catalog for a delivery detail — the pattern
    that already produced customer-named offer rows. Modelling them as an
    untyped blob on the sales order would leave provisioning facts with no
    schema and no owner. So the commercial product stays one offer, and the
    delivery specification lives here as typed, constrained state.

    The sales order captures the requirement; this row is where it lands and
    is read from. One row per subscription — a service has one handoff.

    Each type carries only its own fields, enforced in the database: a BGP
    handoff without an ASN cannot be provisioned, and a clear channel with an
    IP handoff is a contradiction.
    """

    __tablename__ = "service_handoffs"
    __table_args__ = (
        # Exactly the fields the type needs, and none it cannot use.
        CheckConstraint(
            "(handoff_type = 'static_ip' AND customer_asn IS NULL "
            " AND announced_prefixes IS NULL AND a_end_description IS NULL "
            " AND b_end_description IS NULL) "
            "OR (handoff_type = 'bgp' AND customer_asn IS NOT NULL "
            " AND a_end_description IS NULL AND b_end_description IS NULL) "
            "OR (handoff_type = 'layer2_clear_channel' AND customer_asn IS NULL "
            " AND announced_prefixes IS NULL "
            " AND a_end_description IS NOT NULL AND b_end_description IS NOT NULL)",
            name="ck_service_handoffs_fields_match_type",
        ),
        # 16-bit and 32-bit ASN space, excluding 0 and the 16-bit/32-bit
        # reserved-last values.
        CheckConstraint(
            "customer_asn IS NULL OR (customer_asn > 0 AND customer_asn < 4294967295)",
            name="ck_service_handoffs_asn_range",
        ),
        CheckConstraint(
            "vlan_id IS NULL OR (vlan_id > 0 AND vlan_id < 4095)",
            name="ck_service_handoffs_vlan_range",
        ),
        Index("ix_service_handoffs_type", "handoff_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscriptions.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    handoff_type: Mapped[ServiceHandoffType] = mapped_column(
        Enum(ServiceHandoffType, name="servicehandofftype", create_constraint=False),
        nullable=False,
        default=ServiceHandoffType.static_ip,
    )

    # --- BGP ---
    customer_asn: Mapped[int | None] = mapped_column(BigInteger)
    #: Newline-separated CIDRs the customer will announce. Free text rather
    #: than a child table until the NOC needs per-prefix state (accepted,
    #: filtered, RPKI status) — at which point it becomes one.
    announced_prefixes: Mapped[str | None] = mapped_column(Text)
    peer_ip: Mapped[str | None] = mapped_column(String(64))

    # --- layer-2 clear channel ---
    a_end_description: Mapped[str | None] = mapped_column(String(200))
    b_end_description: Mapped[str | None] = mapped_column(String(200))
    vlan_id: Mapped[int | None] = mapped_column(Integer)

    #: What the NOC needs that the typed fields do not carry.
    noc_notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    subscription = relationship("Subscription")


class NasDevice(Base):
    """
    Network Access Server (NAS) device for subscriber authentication.

    NAS devices are routers, OLTs, or access points that:
    - Authenticate subscribers via RADIUS
    - Enforce bandwidth/QoS profiles
    - Can be provisioned with subscriber credentials
    """

    __tablename__ = "nas_devices"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # Basic Information
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    code: Mapped[str | None] = mapped_column(String(60), unique=True)
    vendor: Mapped[NasVendor] = mapped_column(
        Enum(NasVendor, values_callable=lambda x: [e.value for e in x]),
        default=NasVendor.other,
    )
    model: Mapped[str | None] = mapped_column(String(120))
    serial_number: Mapped[str | None] = mapped_column(String(120))
    firmware_version: Mapped[str | None] = mapped_column(String(80))
    description: Mapped[str | None] = mapped_column(Text)

    # Location
    pop_site_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pop_sites.id")
    )
    rack_position: Mapped[str | None] = mapped_column(String(40))

    # Network Configuration (renamed from ip_address for clarity)
    ip_address: Mapped[str | None] = mapped_column(
        String(64)
    )  # Keep for backward compat
    management_ip: Mapped[str | None] = mapped_column(String(64))
    management_port: Mapped[int | None] = mapped_column(Integer, default=120)
    nas_ip: Mapped[str | None] = mapped_column(String(64))  # IP used in RADIUS requests

    # RADIUS Configuration
    shared_secret: Mapped[str | None] = mapped_column(String(512))  # Keep existing
    coa_port: Mapped[int | None] = mapped_column(Integer, default=3799)

    # Management Credentials
    ssh_username: Mapped[str | None] = mapped_column(String(120))
    ssh_password: Mapped[str | None] = mapped_column(String(512))
    ssh_key: Mapped[str | None] = mapped_column(Text)
    ssh_verify_host_key: Mapped[bool] = mapped_column(Boolean, default=False)
    api_username: Mapped[str | None] = mapped_column(String(120))
    api_password: Mapped[str | None] = mapped_column(String(512))
    api_token: Mapped[str | None] = mapped_column(Text)
    api_url: Mapped[str | None] = mapped_column(String(500))
    api_verify_tls: Mapped[bool] = mapped_column(Boolean, default=False)
    # RouterOS API port for the bandwidth poller (8728 plaintext / 8729 API-SSL).
    # First-class column replacing the brittle ``mikrotik_api_port:NNNN`` tag.
    mikrotik_api_port: Mapped[int | None] = mapped_column(Integer)

    # SNMP Configuration
    snmp_community: Mapped[str | None] = mapped_column(String(512))
    snmp_version: Mapped[str | None] = mapped_column(String(10), default="2c")
    snmp_port: Mapped[int | None] = mapped_column(Integer, default=161)

    # Connection Types (JSON array of ConnectionType values)
    supported_connection_types: Mapped[list | None] = mapped_column(
        JSONB, default=lambda: ["pppoe"]
    )
    default_connection_type: Mapped[ConnectionType | None] = mapped_column(
        Enum(ConnectionType, values_callable=lambda x: [e.value for e in x])
    )

    # Configuration Backup Settings
    backup_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    backup_method: Mapped[ConfigBackupMethod | None] = mapped_column(
        Enum(ConfigBackupMethod, values_callable=lambda x: [e.value for e in x])
    )
    backup_schedule: Mapped[str | None] = mapped_column(String(60))  # cron expression
    last_backup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Status
    status: Mapped[NasDeviceStatus] = mapped_column(
        Enum(NasDeviceStatus, values_callable=lambda x: [e.value for e in x]),
        default=NasDeviceStatus.active,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Capacity tracking
    max_concurrent_subscribers: Mapped[int | None] = mapped_column(Integer)
    current_subscriber_count: Mapped[int] = mapped_column(Integer, default=0)

    # Health tracking
    health_status: Mapped[HealthStatus] = mapped_column(
        Enum(HealthStatus, values_callable=lambda x: [e.value for e in x]),
        default=HealthStatus.unknown,
    )
    last_health_check_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    # Metadata
    notes: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list | None] = mapped_column(JSONB)

    # Link to network_device for monitoring integration
    network_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("network_devices.id")
    )

    # Zabbix monitoring integration
    zabbix_host_id: Mapped[str | None] = mapped_column(String(20))
    zabbix_last_sync_at: Mapped[datetime | None] = mapped_column(
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

    # Relationships
    pop_site = relationship("PopSite", back_populates="nas_devices")
    network_device = relationship("NetworkDevice", back_populates="nas_device")
    radius_clients = relationship("RadiusClient", back_populates="nas_device")
    config_backups = relationship("NasConfigBackup", back_populates="nas_device")
    provisioning_logs = relationship("ProvisioningLog", back_populates="nas_device")
    subscriptions = relationship(
        "Subscription", back_populates="provisioning_nas_device"
    )
    connection_rules = relationship(
        "NasConnectionRule",
        back_populates="nas_device",
        cascade="all, delete-orphan",
        order_by="NasConnectionRule.priority.asc()",
    )


class NasConnectionRule(Base):
    """Per-router connection rules for PPPoE/DHCP assignment and shaping."""

    __tablename__ = "nas_connection_rules"
    __table_args__ = (
        UniqueConstraint(
            "nas_device_id", "name", name="uq_nas_connection_rules_device_name"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    nas_device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("nas_devices.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    connection_type: Mapped[ConnectionType | None] = mapped_column(
        Enum(ConnectionType, values_callable=lambda x: [e.value for e in x])
    )
    ip_assignment_mode: Mapped[str | None] = mapped_column(String(40))
    rate_limit_profile: Mapped[str | None] = mapped_column(String(120))
    match_expression: Mapped[str | None] = mapped_column(String(255))
    priority: Mapped[int] = mapped_column(Integer, default=100)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    nas_device = relationship("NasDevice", back_populates="connection_rules")


class RadiusProfile(Base):
    """
    RADIUS profile defining authentication and authorization attributes.

    Profiles define speed limits, VLAN assignments, session controls, and other
    settings projected to the appropriate RADIUS check or reply tables.
    """

    __tablename__ = "radius_profiles"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    code: Mapped[str | None] = mapped_column(String(80), unique=True)
    vendor: Mapped[NasVendor] = mapped_column(
        Enum(NasVendor, values_callable=lambda x: [e.value for e in x]),
        default=NasVendor.other,
    )
    connection_type: Mapped[ConnectionType | None] = mapped_column(
        Enum(ConnectionType, values_callable=lambda x: [e.value for e in x])
    )
    description: Mapped[str | None] = mapped_column(Text)

    # Bandwidth Settings (in Kbps)
    download_speed: Mapped[int | None] = mapped_column(Integer)  # Kbps
    upload_speed: Mapped[int | None] = mapped_column(Integer)  # Kbps
    burst_download: Mapped[int | None] = mapped_column(Integer)
    burst_upload: Mapped[int | None] = mapped_column(Integer)
    burst_threshold: Mapped[int | None] = mapped_column(Integer)
    burst_time: Mapped[int | None] = mapped_column(Integer)  # seconds

    # VLAN Settings
    vlan_id: Mapped[int | None] = mapped_column(Integer)
    inner_vlan_id: Mapped[int | None] = mapped_column(Integer)  # QinQ

    # IP Pool Settings
    ip_pool_name: Mapped[str | None] = mapped_column(String(120))
    ipv6_pool_name: Mapped[str | None] = mapped_column(String(120))

    # Session Settings
    session_timeout: Mapped[int | None] = mapped_column(Integer)  # seconds
    idle_timeout: Mapped[int | None] = mapped_column(Integer)
    simultaneous_use: Mapped[int | None] = mapped_column(Integer, default=1)

    # MikroTik-specific (convenience fields)
    mikrotik_rate_limit: Mapped[str | None] = mapped_column(String(255))
    mikrotik_address_list: Mapped[str | None] = mapped_column(String(120))

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    attributes = relationship("RadiusAttribute", back_populates="profile")
    offer_links = relationship("OfferRadiusProfile", back_populates="profile")
    # access_credentials now has TWO FKs to radius_profiles: the live
    # radius_profile_id, and pre_throttle_radius_profile_id which remembers what a
    # collections throttle replaced. Name the join column explicitly so this
    # relationship keeps meaning "credentials currently ON this profile".
    access_credentials = relationship(
        "AccessCredential",
        back_populates="radius_profile",
        foreign_keys="AccessCredential.radius_profile_id",
    )
    subscriptions = relationship("Subscription", back_populates="radius_profile")


class RadiusAttribute(Base):
    __tablename__ = "radius_attributes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("radius_profiles.id"), nullable=False
    )
    attribute: Mapped[str] = mapped_column(String(120), nullable=False)
    operator: Mapped[str | None] = mapped_column(String(10))
    value: Mapped[str] = mapped_column(String(255), nullable=False)

    profile = relationship("RadiusProfile", back_populates="attributes")


class OfferRadiusProfile(Base):
    __tablename__ = "offer_radius_profiles"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    offer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("catalog_offers.id"), nullable=False
    )
    profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("radius_profiles.id"), nullable=False
    )

    offer = relationship("CatalogOffer", back_populates="radius_profiles")
    profile = relationship("RadiusProfile", back_populates="offer_links")


class AccessCredential(Base):
    __tablename__ = "access_credentials"
    __table_args__ = (
        UniqueConstraint("username", name="uq_access_credentials_username"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=False
    )
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscriptions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    username: Mapped[str] = mapped_column(String(120), nullable=False)
    secret_hash: Mapped[str | None] = mapped_column(String(512))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_auth_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    radius_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("radius_profiles.id")
    )
    # The profile this credential carried before a collections throttle replaced
    # it. Persisted because the throttle is a TEMPORARY override that has to be
    # undone exactly, and the customer's real speed is not otherwise recoverable:
    # the offer's profile is only a guess, and an admin credential-level override
    # would be silently discarded by it. NULL when the credential is not throttled.
    pre_throttle_radius_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("radius_profiles.id"), nullable=True
    )
    # IPoE/DHCP Option 82 relay agent fields
    circuit_id: Mapped[str | None] = mapped_column(String(255))
    remote_id: Mapped[str | None] = mapped_column(String(255))
    # Connection type override (if credential uses different type than NAS default)
    connection_type: Mapped[ConnectionType | None] = mapped_column(
        Enum(ConnectionType, values_callable=lambda x: [e.value for e in x]),
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    subscriber = relationship("Subscriber", back_populates="access_credentials")
    subscription = relationship("Subscription")
    radius_profile = relationship(
        "RadiusProfile",
        back_populates="access_credentials",
        foreign_keys=[radius_profile_id],
    )
    radius_users = relationship("RadiusUser", back_populates="access_credential")


# =============================================================================
# NAS CONFIGURATION BACKUP
# =============================================================================


class NasConfigBackup(Base):
    """
    Configuration backup for a NAS device.

    Stores full device configuration with version tracking
    for diff comparison and rollback capability.
    """

    __tablename__ = "nas_config_backups"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    nas_device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("nas_devices.id"), nullable=False
    )

    # Configuration Content
    config_content: Mapped[str] = mapped_column(Text, nullable=False)
    config_hash: Mapped[str | None] = mapped_column(String(64))  # SHA256 hash
    config_format: Mapped[str | None] = mapped_column(String(40))  # rsc, txt, json
    config_size_bytes: Mapped[int | None] = mapped_column(Integer)

    # Backup Metadata
    backup_method: Mapped[ConfigBackupMethod | None] = mapped_column(
        Enum(ConfigBackupMethod, values_callable=lambda x: [e.value for e in x])
    )
    is_scheduled: Mapped[bool] = mapped_column(Boolean, default=False)
    is_manual: Mapped[bool] = mapped_column(Boolean, default=True)

    # Change Detection
    has_changes: Mapped[bool] = mapped_column(Boolean, default=False)
    changes_summary: Mapped[str | None] = mapped_column(Text)

    # Status
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    keep_forever: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    created_by: Mapped[str | None] = mapped_column(String(120))

    # Relationships
    nas_device = relationship("NasDevice", back_populates="config_backups")


# =============================================================================
# PROVISIONING TEMPLATE
# =============================================================================


class ProvisioningTemplate(Base):
    """
    Provisioning script templates for different vendors and actions.

    Templates use placeholders like {{username}}, {{password}}, {{speed_down}}
    that are replaced with actual values during provisioning.
    """

    __tablename__ = "provisioning_templates"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # Template Identity
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    code: Mapped[str | None] = mapped_column(String(80), unique=True)
    vendor: Mapped[NasVendor] = mapped_column(
        Enum(NasVendor, values_callable=lambda x: [e.value for e in x]), nullable=False
    )
    connection_type: Mapped[ConnectionType] = mapped_column(
        Enum(ConnectionType, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )
    action: Mapped[ProvisioningAction] = mapped_column(
        Enum(ProvisioningAction, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )

    # Template Content
    template_content: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    # Available Placeholders (documented for UI)
    placeholders: Mapped[list | None] = mapped_column(JSONB)
    # Example: ["username", "password", "speed_down", "speed_up", "ip_address", "mac_address"]

    # Execution Settings
    execution_method: Mapped[ExecutionMethod | None] = mapped_column(
        Enum(ExecutionMethod, values_callable=lambda x: [e.value for e in x]),
    )
    expected_output: Mapped[str | None] = mapped_column(Text)  # regex pattern
    timeout_seconds: Mapped[int | None] = mapped_column(Integer, default=30)

    # Status
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


# =============================================================================
# PROVISIONING LOG
# =============================================================================


class ProvisioningLog(Base):
    """
    Log of all provisioning actions executed on NAS devices.

    Provides audit trail and troubleshooting capability.
    """

    __tablename__ = "provisioning_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    nas_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("nas_devices.id")
    )
    subscriber_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id")
    )
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscriptions.id")
    )
    template_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("provisioning_templates.id")
    )

    # Action Details
    action: Mapped[ProvisioningAction] = mapped_column(
        Enum(ProvisioningAction, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )
    command_sent: Mapped[str | None] = mapped_column(Text)
    response_received: Mapped[str | None] = mapped_column(Text)

    # Status
    status: Mapped[ProvisioningLogStatus] = mapped_column(
        Enum(ProvisioningLogStatus, values_callable=lambda x: [e.value for e in x]),
        default=ProvisioningLogStatus.pending,
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    execution_time_ms: Mapped[int | None] = mapped_column(Integer)

    # Context
    triggered_by: Mapped[str | None] = mapped_column(String(120))  # user or system
    request_data: Mapped[dict | None] = mapped_column(JSONB)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    # Relationships
    nas_device = relationship("NasDevice", back_populates="provisioning_logs")
    subscription = relationship("Subscription")
    template = relationship("ProvisioningTemplate")


class SlaPolicyVersion(Base):
    """One immutable, effective-dated contractual SLA policy version.

    Owner: ``customer.service_level`` (OUTAGE_SLA_SPINE §4). Supersedes the
    mutable ``SlaProfile`` as the authority for what a customer is actually
    owed. A profile edit silently rewrote every historical score; a version
    here is append-only, so a period already scored keeps the terms that were
    in force when it was measured.

    Precedence is carried by ``source``, highest first: a subscription
    contract beats an account contract, which beats the subscribed offer
    version, which beats the internal measurement policy. Exactly one scope
    column is populated, and a CHECK constraint binds it to the matching
    source so a row cannot claim a precedence it has no scope for.

    ``policy_key`` is the stable identity across versions; ``version`` counts
    from 1 within it. Ranges are half-open ``[effective_from, effective_to)``
    with an open end meaning "still in force", and a PostgreSQL exclusion
    constraint forbids two versions of one policy overlapping in time — the
    invariant that makes "the policy in force at instant T" a single answer
    rather than a guess.

    Only ``internal_measurement`` may omit an availability target: it states
    what we measure, never what we promised. Every contractual source must
    name its target, because the design forbids inventing one.
    """

    __tablename__ = "sla_policy_versions"
    __table_args__ = (
        UniqueConstraint(
            "policy_key", "version", name="uq_sla_policy_versions_key_version"
        ),
        CheckConstraint("version >= 1", name="ck_sla_policy_versions_version"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_sla_policy_versions_range",
        ),
        CheckConstraint(
            "availability_target_percent IS NULL "
            "OR (availability_target_percent > 0 "
            "AND availability_target_percent <= 100)",
            name="ck_sla_policy_versions_target_bounds",
        ),
        # A contractual policy without a target would force the scorer to
        # invent one; only the internal measurement policy may be silent.
        CheckConstraint(
            "source = 'internal_measurement' "
            "OR availability_target_percent IS NOT NULL",
            name="ck_sla_policy_versions_contractual_target",
        ),
        # Exactly one scope, and it must match the claimed precedence.
        CheckConstraint(
            "(source = 'subscription_contract' AND subscription_id IS NOT NULL "
            " AND subscriber_id IS NULL AND offer_id IS NULL "
            " AND plan_family IS NULL) "
            "OR (source = 'account_contract' AND subscriber_id IS NOT NULL "
            " AND subscription_id IS NULL AND offer_id IS NULL "
            " AND plan_family IS NULL) "
            "OR (source = 'offer_version' AND offer_id IS NOT NULL "
            " AND subscription_id IS NULL AND subscriber_id IS NULL "
            " AND plan_family IS NULL) "
            "OR (source = 'plan_family' AND plan_family IS NOT NULL "
            " AND subscription_id IS NULL AND subscriber_id IS NULL "
            " AND offer_id IS NULL) "
            "OR (source = 'internal_measurement' AND subscription_id IS NULL "
            " AND subscriber_id IS NULL AND offer_id IS NULL "
            " AND plan_family IS NULL)",
            name="ck_sla_policy_versions_scope_matches_source",
        ),
        # The family scope is a closed vocabulary, enforced in the database so
        # a direct write cannot introduce a family the resolver cannot match.
        CheckConstraint(
            "plan_family IS NULL "
            "OR plan_family IN ('unlimited', 'dedicated', 'home_flex')",
            name="ck_sla_policy_versions_plan_family_vocab",
        ),
        UniqueConstraint(
            "command_fingerprint", name="uq_sla_policy_versions_fingerprint"
        ),
        # Database arbitration for concurrent reuse of one idempotency key:
        # the read-side check cannot serialise two processes on its own.
        UniqueConstraint(
            "command_idempotency_key",
            name="uq_sla_policy_versions_idempotency_key",
        ),
        Index("ix_sla_policy_versions_key", "policy_key", "version"),
        Index("ix_sla_policy_versions_subscription", "subscription_id"),
        Index("ix_sla_policy_versions_subscriber", "subscriber_id"),
        Index("ix_sla_policy_versions_offer", "offer_id"),
        Index("ix_sla_policy_versions_plan_family", "plan_family"),
        Index("ix_sla_policy_versions_effective", "effective_from", "effective_to"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    policy_key: Mapped[str] = mapped_column(String(120), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # subscription_contract | account_contract | offer_version |
    # internal_measurement  (SlaPolicySource in service_impact_contracts)
    source: Mapped[str] = mapped_column(String(30), nullable=False)
    # RESTRICT, never CASCADE: this table exists to preserve what a customer
    # was owed. Cascading a parent delete would erase the contractual history
    # a later compensation or dispute has to be settled against.
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscriptions.id", ondelete="RESTRICT")
    )
    subscriber_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id", ondelete="RESTRICT")
    )
    offer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("catalog_offers.id", ondelete="RESTRICT")
    )
    # A commercial-family default. Unlike the other scopes this is a closed
    # vocabulary (PLAN_FAMILY_VALUES), not a foreign key, so it carries no
    # RESTRICT concern — there is no parent row to delete.
    plan_family: Mapped[str | None] = mapped_column(String(40))
    effective_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    availability_target_percent: Mapped[Decimal | None] = mapped_column(Numeric(6, 3))
    calendar_timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, default="Africa/Lagos"
    )
    maintenance_excludable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    credit_percent_per_breach: Mapped[Decimal | None] = mapped_column(Numeric(6, 3))
    credit_cap_percent: Mapped[Decimal | None] = mapped_column(Numeric(6, 3))
    # Provenance: who established these terms and against what evidence.
    contract_reference: Mapped[str | None] = mapped_column(String(200))
    established_by: Mapped[str | None] = mapped_column(String(120))
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sla_policy_versions.id", ondelete="RESTRICT"),
    )
    # Durable replay evidence: a retry of the same intent returns the original
    # outcome instead of raising against the row it already created.
    command_fingerprint: Mapped[str | None] = mapped_column(String(80))
    command_idempotency_key: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
