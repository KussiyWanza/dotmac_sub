from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    computed_field,
    model_validator,
)

from app.models.billing import LedgerEntryType, LedgerSource
from app.models.catalog import (
    AccessType,
    AddOnType,
    BillingCycle,
    BillingMode,
    ConfigBackupMethod,
    ConnectionType,
    ContractTerm,
    DiscountType,
    DunningAction,
    ExecutionMethod,
    GuaranteedSpeedType,
    NasDeviceStatus,
    NasVendor,
    OfferStatus,
    PlanCategory,
    PriceBasis,
    PriceType,
    PriceUnit,
    ProrationPolicy,
    ProvisioningAction,
    ProvisioningLogStatus,
    RefundPolicy,
    ServiceType,
    SubscriptionStatus,
    SuspensionAction,
)
from app.schemas.status_presentation import StatusPresentation


class UsageAllowanceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    included_gb: int | None = None
    overage_rate: Decimal | None = None
    overage_cap_gb: int | None = None
    throttle_rate_mbps: int | None = None
    is_active: bool


class UsageAllowanceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    included_gb: int | None = Field(default=None, ge=0)
    overage_rate: Decimal | None = Field(default=None, ge=0)
    overage_cap_gb: int | None = Field(default=None, ge=0)
    throttle_rate_mbps: int | None = Field(default=None, ge=1)
    is_active: bool = True


class UsageAllowanceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    included_gb: int | None = Field(default=None, ge=0)
    overage_rate: Decimal | None = Field(default=None, ge=0)
    overage_cap_gb: int | None = Field(default=None, ge=0)
    throttle_rate_mbps: int | None = Field(default=None, ge=1)
    is_active: bool | None = None


class SlaProfileRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    uptime_percent: Decimal | None = None
    response_time_hours: int | None = None
    resolution_time_hours: int | None = None
    credit_percent: Decimal | None = None
    notes: str | None = None
    is_active: bool


class SlaProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    uptime_percent: Decimal | None = Field(default=None, ge=0)
    response_time_hours: int | None = Field(default=None, ge=0)
    resolution_time_hours: int | None = Field(default=None, ge=0)
    credit_percent: Decimal | None = Field(default=None, ge=0)
    notes: str | None = None
    is_active: bool = True


class SlaProfileUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    uptime_percent: Decimal | None = Field(default=None, ge=0)
    response_time_hours: int | None = Field(default=None, ge=0)
    resolution_time_hours: int | None = Field(default=None, ge=0)
    credit_percent: Decimal | None = Field(default=None, ge=0)
    notes: str | None = None
    is_active: bool | None = None


class PolicySetRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    is_active: bool


class PolicySetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    proration_policy: ProrationPolicy = ProrationPolicy.immediate
    downgrade_policy: ProrationPolicy = ProrationPolicy.next_cycle
    trial_days: int | None = Field(default=None, ge=0)
    trial_card_required: bool = False
    grace_days: int | None = Field(default=None, ge=0)
    suspension_action: SuspensionAction = SuspensionAction.suspend
    refund_policy: RefundPolicy = RefundPolicy.none
    refund_window_days: int | None = Field(default=None, ge=0)
    is_active: bool = True


class PolicySetUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    proration_policy: ProrationPolicy | None = None
    downgrade_policy: ProrationPolicy | None = None
    trial_days: int | None = Field(default=None, ge=0)
    trial_card_required: bool | None = None
    grace_days: int | None = Field(default=None, ge=0)
    suspension_action: SuspensionAction | None = None
    refund_policy: RefundPolicy | None = None
    refund_window_days: int | None = Field(default=None, ge=0)
    is_active: bool | None = None


class RegionZoneRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    code: str | None = None
    description: str | None = None
    is_active: bool


class RegionZoneCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    code: str | None = Field(default=None, max_length=40)
    description: str | None = None
    is_active: bool = True


class RegionZoneUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    code: str | None = Field(default=None, max_length=40)
    description: str | None = None
    is_active: bool | None = None


class AddOnBase(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    addon_type: AddOnType = AddOnType.custom
    description: str | None = None
    is_active: bool = True


class AddOnRead(AddOnBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class AddOnCreate(AddOnBase):
    pass


class AddOnUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    addon_type: AddOnType | None = None
    description: str | None = None
    is_active: bool | None = None


class OfferAddOnRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    add_on_id: UUID
    is_required: bool
    min_quantity: int | None = None
    max_quantity: int | None = None
    add_on: AddOnRead | None = None


class OfferPriceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    price_type: PriceType
    amount: Decimal
    currency: str
    billing_cycle: BillingCycle | None = None
    unit: PriceUnit | None = None
    description: str | None = None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class RadiusProfileSummary(BaseModel):
    """Minimal RADIUS profile fields surfaced when nested under an Offer."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    code: str | None = None
    download_speed: int | None = None
    upload_speed: int | None = None
    mikrotik_rate_limit: str | None = None


class OfferRadiusProfileLinkRead(BaseModel):
    """Offer ↔ RADIUS profile binding with the nested profile summary."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    profile_id: UUID
    profile: RadiusProfileSummary | None = None


class OfferPriceCreate(BaseModel):
    offer_id: UUID
    price_type: PriceType = PriceType.recurring
    amount: Decimal = Field(gt=0, lt=100_000_000)
    currency: str = Field(default="NGN", min_length=3, max_length=3)
    billing_cycle: BillingCycle | None = None
    unit: PriceUnit | None = None
    description: str | None = Field(default=None, max_length=200)
    is_active: bool = True


class OfferPriceUpdate(BaseModel):
    offer_id: UUID | None = None
    price_type: PriceType | None = None
    amount: Decimal | None = Field(default=None, gt=0, lt=100_000_000)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    billing_cycle: BillingCycle | None = None
    unit: PriceUnit | None = None
    description: str | None = Field(default=None, max_length=200)
    is_active: bool | None = None


class AddOnPriceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    price_type: PriceType
    amount: Decimal
    currency: str
    billing_cycle: BillingCycle | None = None
    unit: PriceUnit | None = None
    description: str | None = None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class AddOnPriceCreate(BaseModel):
    add_on_id: UUID
    price_type: PriceType = PriceType.recurring
    amount: Decimal = Field(gt=0, lt=100_000_000)
    currency: str = Field(default="NGN", min_length=3, max_length=3)
    billing_cycle: BillingCycle | None = None
    unit: PriceUnit | None = None
    description: str | None = Field(default=None, max_length=200)
    is_active: bool = True


class AddOnPriceUpdate(BaseModel):
    add_on_id: UUID | None = None
    price_type: PriceType | None = None
    amount: Decimal | None = Field(default=None, gt=0, lt=100_000_000)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    billing_cycle: BillingCycle | None = None
    unit: PriceUnit | None = None
    description: str | None = Field(default=None, max_length=200)
    is_active: bool | None = None


# Families are settings-driven (catalog.plan_families) — validate the shape,
# not membership; the offer form select constrains the choices.
PlanFamilyValue = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, to_lower=True, pattern=r"^[a-z0-9_ -]{1,60}$"
    ),
]


class CatalogOfferBase(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    code: str | None = Field(default=None, max_length=60)
    service_type: ServiceType
    access_type: AccessType
    price_basis: PriceBasis
    billing_cycle: BillingCycle = BillingCycle.monthly
    billing_mode: BillingMode = BillingMode.prepaid
    contract_term: ContractTerm = ContractTerm.month_to_month
    region_zone_id: UUID | None = None
    usage_allowance_id: UUID | None = None
    sla_profile_id: UUID | None = None
    policy_set_id: UUID | None = None
    splynx_tariff_id: int | None = None
    splynx_service_name: str | None = Field(default=None, max_length=160)
    splynx_tax_id: int | None = None
    with_vat: bool = False
    vat_percent: Decimal | None = None
    speed_download_mbps: int | None = None
    speed_upload_mbps: int | None = None
    guaranteed_speed_limit_at: int | None = Field(default=None, ge=0)
    guaranteed_speed: GuaranteedSpeedType = GuaranteedSpeedType.none
    aggregation: int | None = None
    priority: str | None = Field(default=None, max_length=40)
    available_for_services: bool = True
    show_on_customer_portal: bool = True
    olt_profile_auto_sync_enabled: bool = False
    plan_category: PlanCategory = PlanCategory.internet
    hide_on_admin_portal: bool = False
    service_description: str | None = None
    burst_profile: str | None = Field(default=None, max_length=120)
    prepaid_period: str | None = Field(default=None, max_length=40)
    plan_family: PlanFamilyValue | None = None
    allowed_change_plan_ids: str | None = None
    status: OfferStatus = OfferStatus.active
    description: str | None = None
    is_active: bool = True


class CatalogOfferCreate(CatalogOfferBase):
    pass


class CatalogOfferUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    code: str | None = Field(default=None, max_length=60)
    service_type: ServiceType | None = None
    access_type: AccessType | None = None
    price_basis: PriceBasis | None = None
    billing_cycle: BillingCycle | None = None
    billing_mode: BillingMode | None = None
    contract_term: ContractTerm | None = None
    region_zone_id: UUID | None = None
    usage_allowance_id: UUID | None = None
    sla_profile_id: UUID | None = None
    policy_set_id: UUID | None = None
    splynx_tariff_id: int | None = None
    splynx_service_name: str | None = Field(default=None, max_length=160)
    splynx_tax_id: int | None = None
    with_vat: bool | None = None
    vat_percent: Decimal | None = None
    speed_download_mbps: int | None = None
    speed_upload_mbps: int | None = None
    guaranteed_speed_limit_at: int | None = Field(default=None, ge=0)
    guaranteed_speed: GuaranteedSpeedType | None = None
    aggregation: int | None = None
    priority: str | None = Field(default=None, max_length=40)
    available_for_services: bool | None = None
    show_on_customer_portal: bool | None = None
    olt_profile_auto_sync_enabled: bool | None = None
    plan_category: PlanCategory | None = None
    hide_on_admin_portal: bool | None = None
    service_description: str | None = None
    burst_profile: str | None = Field(default=None, max_length=120)
    prepaid_period: str | None = Field(default=None, max_length=40)
    plan_family: PlanFamilyValue | None = None
    allowed_change_plan_ids: str | None = None
    status: OfferStatus | None = None
    description: str | None = None
    is_active: bool | None = None


class CatalogOfferRead(CatalogOfferBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime
    region_zone: RegionZoneRead | None = None
    usage_allowance: UsageAllowanceRead | None = None
    sla_profile: SlaProfileRead | None = None
    policy_set: PolicySetRead | None = None
    prices: list[OfferPriceRead] = Field(default_factory=list)
    add_on_links: list[OfferAddOnRead] = Field(default_factory=list)
    radius_profiles: list[OfferRadiusProfileLinkRead] = Field(default_factory=list)


class OfferSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    code: str | None = None
    service_type: ServiceType
    access_type: AccessType
    status: OfferStatus
    # Provisioned line rate, so the app can show what the plan actually
    # delivers (e.g. "Unlimited Platinum Plus — 100/50 Mbps").
    speed_download_mbps: int | None = None
    speed_upload_mbps: int | None = None


class SubscriptionAddOnRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    add_on_id: UUID
    quantity: int
    start_at: datetime | None = None
    end_at: datetime | None = None
    add_on: AddOnRead | None = None


class SubscriptionAddOnCreate(BaseModel):
    subscription_id: UUID
    add_on_id: UUID
    quantity: int = Field(ge=1, default=1)
    start_at: datetime | None = None
    end_at: datetime | None = None


class SubscriptionAddOnUpdate(BaseModel):
    subscription_id: UUID | None = None
    add_on_id: UUID | None = None
    quantity: int | None = Field(default=None, ge=1)
    start_at: datetime | None = None
    end_at: datetime | None = None


class SubscriptionBase(BaseModel):
    subscriber_id: UUID = Field(
        validation_alias=AliasChoices("account_id", "subscriber_id"),
        serialization_alias="account_id",
    )
    offer_id: UUID
    offer_version_id: UUID | None = None
    service_address_id: UUID | None = None
    status: SubscriptionStatus = SubscriptionStatus.pending
    billing_mode: BillingMode = BillingMode.prepaid
    contract_term: ContractTerm = ContractTerm.month_to_month
    # Contracted billing cadence (SOT: subscription owns effective cadence).
    # None => inherit the offer price cadence at creation.
    billing_cycle: BillingCycle | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    next_billing_at: datetime | None = None
    canceled_at: datetime | None = None
    cancel_reason: str | None = Field(default=None, max_length=200)
    splynx_service_id: int | None = None
    router_id: int | None = None
    service_description: str | None = None
    quantity: int | None = None
    unit: str | None = Field(default=None, max_length=40)
    unit_price: Decimal | None = None
    discount: bool = False
    discount_value: Decimal | None = None
    discount_type: DiscountType | None = None
    service_status_raw: str | None = Field(default=None, max_length=40)
    login: str | None = Field(default=None, max_length=120)
    ipv4_address: str | None = Field(default=None, max_length=64)
    ipv6_address: str | None = Field(default=None, max_length=128)
    mac_address: str | None = Field(default=None, max_length=64)
    provisioning_nas_device_id: UUID | None = None
    radius_profile_id: UUID | None = None


class SubscriptionCreate(SubscriptionBase):
    pass


class SubscriptionUpdate(BaseModel):
    subscriber_id: UUID | None = Field(
        default=None,
        validation_alias=AliasChoices("account_id", "subscriber_id"),
        serialization_alias="account_id",
    )
    offer_id: UUID | None = None
    offer_version_id: UUID | None = None
    service_address_id: UUID | None = None
    status: SubscriptionStatus | None = None
    billing_mode: BillingMode | None = None
    contract_term: ContractTerm | None = None
    billing_cycle: BillingCycle | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    next_billing_at: datetime | None = None
    canceled_at: datetime | None = None
    cancel_reason: str | None = Field(default=None, max_length=200)
    splynx_service_id: int | None = None
    router_id: int | None = None
    service_description: str | None = None
    quantity: int | None = None
    unit: str | None = Field(default=None, max_length=40)
    unit_price: Decimal | None = None
    discount: bool | None = None
    discount_value: Decimal | None = None
    discount_type: DiscountType | None = None
    service_status_raw: str | None = Field(default=None, max_length=40)
    login: str | None = Field(default=None, max_length=120)
    ipv4_address: str | None = Field(default=None, max_length=64)
    ipv6_address: str | None = Field(default=None, max_length=128)
    mac_address: str | None = Field(default=None, max_length=64)
    provisioning_nas_device_id: UUID | None = None
    radius_profile_id: UUID | None = None

    @model_validator(mode="after")
    def _validate_status_timestamps(self) -> SubscriptionUpdate:
        fields_set = self.model_fields_set
        if "status" in fields_set and self.status == SubscriptionStatus.canceled:
            if "canceled_at" not in fields_set or self.canceled_at is None:
                raise ValueError("canceled_at is required when status is canceled")
        return self


class SubscriptionTechnicalUpdate(BaseModel):
    """Generic PATCH fields that cannot change subscription lifecycle state."""

    model_config = ConfigDict(extra="forbid")

    subscriber_id: UUID | None = Field(
        default=None,
        validation_alias=AliasChoices("account_id", "subscriber_id"),
        serialization_alias="account_id",
    )
    offer_id: UUID | None = None
    offer_version_id: UUID | None = None
    service_address_id: UUID | None = None
    billing_mode: BillingMode | None = None
    contract_term: ContractTerm | None = None
    billing_cycle: BillingCycle | None = None
    splynx_service_id: int | None = None
    router_id: int | None = None
    service_description: str | None = None
    quantity: int | None = None
    unit: str | None = Field(default=None, max_length=40)
    unit_price: Decimal | None = None
    discount: bool | None = None
    discount_value: Decimal | None = None
    discount_type: DiscountType | None = None
    service_status_raw: str | None = Field(default=None, max_length=40)
    login: str | None = Field(default=None, max_length=120)
    ipv4_address: str | None = Field(default=None, max_length=64)
    ipv6_address: str | None = Field(default=None, max_length=128)
    mac_address: str | None = Field(default=None, max_length=64)
    provisioning_nas_device_id: UUID | None = None
    radius_profile_id: UUID | None = None


# Statuses where the service has genuinely ended (vs. merely needing a payment,
# which is blocked/suspended). The one authoritative definition for is_expired.
_ENDED_SUBSCRIPTION_STATUSES = frozenset(
    {
        SubscriptionStatus.expired,
        SubscriptionStatus.canceled,
        SubscriptionStatus.disabled,
        SubscriptionStatus.archived,
        SubscriptionStatus.hidden,
    }
)


class SubscriptionRead(SubscriptionBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime
    offer: OfferSummary | None = None
    add_ons: list[SubscriptionAddOnRead] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def status_presentation(self) -> StatusPresentation:
        """Canonical label/tone/icon projection for cross-client rendering."""
        # Local import keeps the transport schema independent at import time;
        # the computed read adapter still delegates all decisions to the owner.
        from app.services.status_presentation import subscription_status_presentation

        return subscription_status_presentation(self.status)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def expires_at(self) -> datetime | None:
        """The date the service genuinely lapses, or null when it has none.

        This is an explicit contract end only. ``next_billing_at`` is the next
        *charge* date (prepaid) / next invoice date (postpaid), NOT an expiry —
        clients must not treat it as one. Prepaid service lapses on balance
        exhaustion (a consumption-driven event, not a date); the real pending
        lapse date in that case is exposed by ``GET /me/service-status``.
        """
        return self.end_at

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_expired(self) -> bool:
        """Whether the service has genuinely ended.

        An active/pending service is never expired — ``status`` is the source of
        truth, so a momentarily-stale billing date can't override a running
        service. Otherwise: an ended status, or a contract end in the past.
        """
        if self.status in (
            SubscriptionStatus.active,
            SubscriptionStatus.pending,
        ):
            return False
        if self.status in _ENDED_SUBSCRIPTION_STATUSES:
            return True
        end = self.end_at
        if end is None:
            return False
        end = end if end.tzinfo else end.replace(tzinfo=UTC)
        return end <= datetime.now(UTC)


class OfferVersionBase(BaseModel):
    offer_id: UUID
    version_number: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=160)
    code: str | None = Field(default=None, max_length=60)
    service_type: ServiceType
    access_type: AccessType
    price_basis: PriceBasis
    billing_cycle: BillingCycle = BillingCycle.monthly
    contract_term: ContractTerm = ContractTerm.month_to_month
    region_zone_id: UUID | None = None
    usage_allowance_id: UUID | None = None
    sla_profile_id: UUID | None = None
    policy_set_id: UUID | None = None
    status: OfferStatus = OfferStatus.active
    description: str | None = None
    effective_start: datetime | None = None
    effective_end: datetime | None = None
    is_active: bool = True


class OfferVersionCreate(OfferVersionBase):
    pass


class OfferVersionUpdate(BaseModel):
    offer_id: UUID | None = None
    version_number: int | None = Field(default=None, ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=160)
    code: str | None = Field(default=None, max_length=60)
    service_type: ServiceType | None = None
    access_type: AccessType | None = None
    price_basis: PriceBasis | None = None
    billing_cycle: BillingCycle | None = None
    contract_term: ContractTerm | None = None
    region_zone_id: UUID | None = None
    usage_allowance_id: UUID | None = None
    sla_profile_id: UUID | None = None
    policy_set_id: UUID | None = None
    status: OfferStatus | None = None
    description: str | None = None
    effective_start: datetime | None = None
    effective_end: datetime | None = None
    is_active: bool | None = None


class OfferVersionRead(OfferVersionBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class OfferVersionPriceBase(BaseModel):
    offer_version_id: UUID
    price_type: PriceType = PriceType.recurring
    amount: Decimal
    currency: str = Field(default="NGN", max_length=3)
    billing_cycle: BillingCycle | None = None
    unit: PriceUnit | None = None
    description: str | None = Field(default=None, max_length=200)
    is_active: bool = True


class OfferVersionPriceCreate(OfferVersionPriceBase):
    pass


class OfferVersionPriceUpdate(BaseModel):
    offer_version_id: UUID | None = None
    price_type: PriceType | None = None
    amount: Decimal | None = None
    currency: str | None = Field(default=None, max_length=3)
    billing_cycle: BillingCycle | None = None
    unit: PriceUnit | None = None
    description: str | None = Field(default=None, max_length=200)
    is_active: bool | None = None


class OfferVersionPriceRead(OfferVersionPriceBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class PolicyDunningStepBase(BaseModel):
    policy_set_id: UUID
    day_offset: int = Field(ge=0)
    action: DunningAction
    note: str | None = Field(default=None, max_length=200)


class PolicyDunningStepCreate(PolicyDunningStepBase):
    pass


class PolicyDunningStepUpdate(BaseModel):
    policy_set_id: UUID | None = None
    day_offset: int | None = Field(default=None, ge=0)
    action: DunningAction | None = None
    note: str | None = Field(default=None, max_length=200)


class PolicyDunningStepRead(PolicyDunningStepBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID


class RadiusProfileBase(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    code: str | None = Field(default=None, max_length=80)
    vendor: NasVendor = NasVendor.other
    connection_type: ConnectionType | None = None
    description: str | None = None

    # Bandwidth Settings (in Kbps)
    download_speed: int | None = None
    upload_speed: int | None = None
    burst_download: int | None = None
    burst_upload: int | None = None
    burst_threshold: int | None = None
    burst_time: int | None = None

    # VLAN Settings
    vlan_id: int | None = None
    inner_vlan_id: int | None = None

    # IP Pool Settings
    ip_pool_name: str | None = Field(default=None, max_length=120)
    ipv6_pool_name: str | None = Field(default=None, max_length=120)

    # Session Settings
    session_timeout: int | None = None
    idle_timeout: int | None = None
    simultaneous_use: int | None = 1

    # MikroTik-specific
    mikrotik_rate_limit: str | None = Field(default=None, max_length=255)
    mikrotik_address_list: str | None = Field(default=None, max_length=120)

    is_active: bool = True


class RadiusProfileCreate(RadiusProfileBase):
    pass


class RadiusProfileUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    code: str | None = Field(default=None, max_length=80)
    vendor: NasVendor | None = None
    connection_type: ConnectionType | None = None
    description: str | None = None

    download_speed: int | None = None
    upload_speed: int | None = None
    burst_download: int | None = None
    burst_upload: int | None = None
    burst_threshold: int | None = None
    burst_time: int | None = None

    vlan_id: int | None = None
    inner_vlan_id: int | None = None

    ip_pool_name: str | None = Field(default=None, max_length=120)
    ipv6_pool_name: str | None = Field(default=None, max_length=120)

    session_timeout: int | None = None
    idle_timeout: int | None = None
    simultaneous_use: int | None = None

    mikrotik_rate_limit: str | None = Field(default=None, max_length=255)
    mikrotik_address_list: str | None = Field(default=None, max_length=120)

    is_active: bool | None = None


class RadiusProfileRead(RadiusProfileBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class RadiusAttributeBase(BaseModel):
    profile_id: UUID
    attribute: str = Field(min_length=1, max_length=120)
    operator: str | None = Field(default=None, max_length=10)
    value: str = Field(min_length=1, max_length=255)


class RadiusAttributeCreate(RadiusAttributeBase):
    pass


class RadiusAttributeUpdate(BaseModel):
    profile_id: UUID | None = None
    attribute: str | None = Field(default=None, min_length=1, max_length=120)
    operator: str | None = Field(default=None, max_length=10)
    value: str | None = Field(default=None, min_length=1, max_length=255)


class RadiusAttributeRead(RadiusAttributeBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID


class OfferRadiusProfileBase(BaseModel):
    offer_id: UUID
    profile_id: UUID


class OfferRadiusProfileCreate(OfferRadiusProfileBase):
    pass


class OfferRadiusProfileUpdate(BaseModel):
    offer_id: UUID | None = None
    profile_id: UUID | None = None


class OfferRadiusProfileRead(OfferRadiusProfileBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID


class NasDeviceBase(BaseModel):
    # Basic Information
    name: str = Field(min_length=1, max_length=160)
    code: str | None = Field(default=None, max_length=60)
    vendor: NasVendor = NasVendor.other
    model: str | None = Field(default=None, max_length=120)
    serial_number: str | None = Field(default=None, max_length=120)
    firmware_version: str | None = Field(default=None, max_length=80)
    description: str | None = None

    # Location
    pop_site_id: UUID | None = None
    rack_position: str | None = Field(default=None, max_length=40)

    # Network Configuration
    ip_address: str | None = Field(default=None, max_length=64)
    management_ip: str | None = Field(default=None, max_length=64)
    management_port: int | None = 120
    nas_ip: str | None = Field(default=None, max_length=64)

    # RADIUS Configuration
    shared_secret: str | None = Field(default=None, max_length=255)
    coa_port: int | None = 3799

    # Management Credentials
    ssh_username: str | None = Field(default=None, max_length=120)
    ssh_password: str | None = Field(default=None, max_length=255)
    ssh_key: str | None = None
    ssh_verify_host_key: bool = False
    api_username: str | None = Field(default=None, max_length=120)
    api_password: str | None = Field(default=None, max_length=255)
    api_token: str | None = None
    api_url: str | None = Field(default=None, max_length=500)
    api_verify_tls: bool = False

    # SNMP Configuration
    snmp_community: str | None = Field(default=None, max_length=120)
    snmp_version: str | None = Field(default="2c", max_length=10)
    snmp_port: int | None = 161

    # Connection Types
    supported_connection_types: list[str] | None = None
    default_connection_type: ConnectionType | None = None

    # Configuration Backup Settings
    backup_enabled: bool = False
    backup_method: ConfigBackupMethod | None = None
    backup_schedule: str | None = Field(default=None, max_length=60)

    # Status
    status: NasDeviceStatus = NasDeviceStatus.active
    is_active: bool = True

    # Metadata
    notes: str | None = None
    tags: list[str] | None = None

    # Network device link
    network_device_id: UUID | None = None


class NasDeviceCreate(NasDeviceBase):
    pass


class NasDeviceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    code: str | None = Field(default=None, max_length=60)
    vendor: NasVendor | None = None
    model: str | None = Field(default=None, max_length=120)
    serial_number: str | None = Field(default=None, max_length=120)
    firmware_version: str | None = Field(default=None, max_length=80)
    description: str | None = None

    pop_site_id: UUID | None = None
    rack_position: str | None = Field(default=None, max_length=40)

    ip_address: str | None = Field(default=None, max_length=64)
    management_ip: str | None = Field(default=None, max_length=64)
    management_port: int | None = None
    nas_ip: str | None = Field(default=None, max_length=64)

    shared_secret: str | None = Field(default=None, max_length=255)
    coa_port: int | None = None

    ssh_username: str | None = Field(default=None, max_length=120)
    ssh_password: str | None = Field(default=None, max_length=255)
    ssh_key: str | None = None
    ssh_verify_host_key: bool | None = None
    api_username: str | None = Field(default=None, max_length=120)
    api_password: str | None = Field(default=None, max_length=255)
    api_token: str | None = None
    api_url: str | None = Field(default=None, max_length=500)
    api_verify_tls: bool | None = None

    snmp_community: str | None = Field(default=None, max_length=120)
    snmp_version: str | None = Field(default=None, max_length=10)
    snmp_port: int | None = None

    supported_connection_types: list[str] | None = None
    default_connection_type: ConnectionType | None = None

    backup_enabled: bool | None = None
    backup_method: ConfigBackupMethod | None = None
    backup_schedule: str | None = Field(default=None, max_length=60)

    status: NasDeviceStatus | None = None
    is_active: bool | None = None

    notes: str | None = None
    tags: list[str] | None = None

    network_device_id: UUID | None = None


class NasDeviceRead(NasDeviceBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    last_backup_at: datetime | None = None
    last_seen_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class AccessCredentialBase(BaseModel):
    subscriber_id: UUID = Field(
        validation_alias=AliasChoices("account_id", "subscriber_id"),
        serialization_alias="account_id",
    )
    subscription_id: UUID | None = None
    username: str = Field(min_length=1, max_length=120)
    secret_hash: str | None = Field(default=None, max_length=255)
    is_active: bool = True
    last_auth_at: datetime | None = None
    radius_profile_id: UUID | None = None


class AccessCredentialCreate(AccessCredentialBase):
    pass


class AccessCredentialUpdate(BaseModel):
    subscriber_id: UUID | None = Field(
        default=None,
        validation_alias=AliasChoices("account_id", "subscriber_id"),
        serialization_alias="account_id",
    )
    subscription_id: UUID | None = None
    username: str | None = Field(default=None, min_length=1, max_length=120)
    secret_hash: str | None = Field(default=None, max_length=255)
    is_active: bool | None = None
    last_auth_at: datetime | None = None
    radius_profile_id: UUID | None = None


class AccessCredentialRead(AccessCredentialBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class ValidationAddOnRequest(BaseModel):
    add_on_id: UUID
    quantity: int = Field(ge=1, default=1)


class OfferValidationRequest(BaseModel):
    offer_id: UUID
    offer_version_id: UUID | None = None
    billing_cycle: BillingCycle | None = None
    add_ons: list[ValidationAddOnRequest] = Field(default_factory=list)


class OfferValidationPrice(BaseModel):
    source: str
    price_type: PriceType
    amount: Decimal
    currency: str
    billing_cycle: BillingCycle | None = None
    unit: PriceUnit | None = None
    description: str | None = None
    add_on_id: UUID | None = None
    quantity: int | None = None
    extended_amount: Decimal


class OfferValidationResponse(BaseModel):
    valid: bool
    errors: list[str] = Field(default_factory=list)
    offer_id: UUID
    offer_version_id: UUID | None = None
    billing_cycle: BillingCycle | None = None
    prices: list[OfferValidationPrice] = Field(default_factory=list)
    recurring_total: Decimal = Decimal("0.00")
    one_time_total: Decimal = Decimal("0.00")
    usage_total: Decimal = Decimal("0.00")


# =============================================================================
# NAS CONFIG BACKUP SCHEMAS
# =============================================================================


class NasConfigBackupBase(BaseModel):
    nas_device_id: UUID
    config_content: str
    config_format: str | None = Field(default=None, max_length=40)
    backup_method: ConfigBackupMethod | None = None
    is_scheduled: bool = False
    is_manual: bool = True
    keep_forever: bool = False
    notes: str | None = None


class NasConfigBackupCreate(NasConfigBackupBase):
    pass


class NasConfigBackupRead(NasConfigBackupBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    config_hash: str | None = None
    config_size_bytes: int | None = None
    has_changes: bool = False
    changes_summary: str | None = None
    is_current: bool = True
    keep_forever: bool = False
    created_at: datetime
    created_by: str | None = None


# =============================================================================
# PROVISIONING TEMPLATE SCHEMAS
# =============================================================================


class ProvisioningTemplateBase(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    code: str | None = Field(default=None, max_length=80)
    vendor: NasVendor
    connection_type: ConnectionType
    action: ProvisioningAction
    template_content: str
    description: str | None = None
    placeholders: list[str] | None = None
    execution_method: ExecutionMethod | None = None
    expected_output: str | None = None
    timeout_seconds: int | None = 30
    is_active: bool = True
    is_default: bool = False


class ProvisioningTemplateCreate(ProvisioningTemplateBase):
    pass


class ProvisioningTemplateUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    code: str | None = Field(default=None, max_length=80)
    vendor: NasVendor | None = None
    connection_type: ConnectionType | None = None
    action: ProvisioningAction | None = None
    template_content: str | None = None
    description: str | None = None
    placeholders: list[str] | None = None
    execution_method: ExecutionMethod | None = None
    expected_output: str | None = None
    timeout_seconds: int | None = None
    is_active: bool | None = None
    is_default: bool | None = None


class ProvisioningTemplateRead(ProvisioningTemplateBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


# =============================================================================
# PROVISIONING LOG SCHEMAS
# =============================================================================


class ProvisioningLogBase(BaseModel):
    nas_device_id: UUID | None = None
    subscriber_id: UUID | None = None
    template_id: UUID | None = None
    action: ProvisioningAction
    command_sent: str | None = None
    response_received: str | None = None
    status: ProvisioningLogStatus = ProvisioningLogStatus.pending
    error_message: str | None = None
    execution_time_ms: int | None = None
    triggered_by: str | None = Field(default=None, max_length=120)
    request_data: dict | None = None


class ProvisioningLogCreate(ProvisioningLogBase):
    pass


class ProvisioningLogRead(ProvisioningLogBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime


# --- Customer self-service plan change (app/api/me.py) ---------------------


class PlanOfferSummary(BaseModel):
    id: UUID
    name: str
    amount: float = 0.0
    currency: str = "NGN"
    period_label: str = "/cycle"


class ServiceAddressOption(BaseModel):
    id: UUID
    label: str
    has_coordinates: bool = False
    is_current: bool = False


class PlanChangePageResponse(BaseModel):
    current_offer: PlanOfferSummary | None = None
    available_offers: list[PlanOfferSummary] = Field(default_factory=list)
    prepaid_funding: Decimal | None = None
    postpaid_receivables: Decimal = Decimal("0.00")
    collection_blocking_balance: Decimal = Decimal("0.00")
    next_billing_date: datetime | None = None
    billing_message: str | None = None
    service_addresses: list[ServiceAddressOption] = Field(default_factory=list)
    current_service_address_id: UUID | None = None


class PlanChangeSubmitRequest(BaseModel):
    offer_id: UUID
    target_service_address_id: UUID | None = None
    # The owner preview binds money and delivery evidence to this confirmation.
    preview_fingerprint: str | None = Field(default=None, min_length=64, max_length=64)
    field_quote_fingerprint: str | None = Field(
        default=None, min_length=64, max_length=64
    )
    preview_effective_at: datetime | None = None
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=120)
    notes: str | None = None


class PlanChangeSubmitResponse(BaseModel):
    success: bool = True
    # applied, pending_remote_reprovision, or pending_field_migration.
    status: str = "applied"
    message: str | None = None
    change_request_id: UUID | None = None
    account_adjustment_id: UUID | None = None
    credit_note_id: UUID | None = None
    ledger_entry_id: UUID | None = None


# --- Customer self-service add-on purchase (app/api/me.py) -----------------


class AddonOption(BaseModel):
    add_on_id: UUID
    name: str
    addon_type: str
    description: str | None = None
    amount: float = 0.0
    currency: str = "NGN"
    min_quantity: int = 1
    max_quantity: int | None = None
    is_required: bool = False
    grant_gb: int | None = None


class ActiveAddon(BaseModel):
    id: UUID
    add_on_id: UUID
    name: str
    quantity: int = 1
    addon_type: str | None = None
    grant_gb: int | None = None
    total_grant_gb: int | None = None
    starts_at: datetime | None = None
    expires_at: datetime | None = None
    validity_days: int | None = None
    is_expired: bool = False


class AddonsAvailableResponse(BaseModel):
    available: list[AddonOption] = Field(default_factory=list)
    active: list[ActiveAddon] = Field(default_factory=list)


class AddonQuoteResponse(BaseModel):
    add_on_id: UUID
    quantity: int
    unit_amount: Decimal
    charge: Decimal
    currency: str = "NGN"
    subscription_status: str
    prepaid_funding_before: Decimal
    prepaid_funding_after: Decimal
    postpaid_receivables: Decimal
    collection_blocking_balance: Decimal
    shortfall: Decimal
    can_afford: bool
    allowed: bool
    rejection_reason: str | None = None
    ledger_entry_type: LedgerEntryType | None = None
    ledger_source: LedgerSource | None = None
    ledger_amount: Decimal
    access_consequence: str
    preview_fingerprint: str = Field(min_length=64, max_length=64)


class AddonPurchaseRequest(BaseModel):
    add_on_id: UUID
    quantity: int = Field(default=1, ge=1)
    preview_fingerprint: str = Field(min_length=64, max_length=64)
    idempotency_key: str = Field(min_length=16, max_length=120)


class AddonPurchaseResponse(BaseModel):
    success: bool
    reason: str | None = None
    subscription_status: str | None = None
    replayed: bool = False
    subscription_add_on_id: UUID | None = None
    add_on_name: str | None = None
    quantity: int | None = None
    charge: Decimal | None = None
    currency: str = "NGN"
    prepaid_funding_before: Decimal | None = None
    prepaid_funding_after: Decimal | None = None
    postpaid_receivables: Decimal | None = None
    collection_blocking_balance: Decimal | None = None
    shortfall: Decimal | None = None
    account_adjustment_id: UUID | None = None
    ledger_entry_id: UUID | None = None
    preview_fingerprint: str | None = None
    access_consequence: str | None = None
