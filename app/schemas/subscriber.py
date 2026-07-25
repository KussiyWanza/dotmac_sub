from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)

from app.models.catalog import BillingMode
from app.models.subscriber import (
    AddressType,
    ChannelType,
    ContactMethod,
    Gender,
    SubscriberCategory,
    SubscriberStatus,
)
from app.services.nin_matching import normalize_nin


class OrganizationBase(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    legal_name: str | None = Field(default=None, max_length=200)
    tax_id: str | None = Field(default=None, max_length=80)
    domain: str | None = Field(default=None, max_length=120)
    website: str | None = Field(default=None, max_length=255)
    address_line1: str | None = Field(default=None, max_length=120)
    address_line2: str | None = Field(default=None, max_length=120)
    city: str | None = Field(default=None, max_length=80)
    region: str | None = Field(default=None, max_length=80)
    postal_code: str | None = Field(default=None, max_length=20)
    country_code: str | None = Field(default=None, max_length=2)
    notes: str | None = None


class OrganizationCreate(OrganizationBase):
    pass


class OrganizationUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    legal_name: str | None = Field(default=None, max_length=200)
    tax_id: str | None = Field(default=None, max_length=80)
    domain: str | None = Field(default=None, max_length=120)
    website: str | None = Field(default=None, max_length=255)
    address_line1: str | None = Field(default=None, max_length=120)
    address_line2: str | None = Field(default=None, max_length=120)
    city: str | None = Field(default=None, max_length=80)
    region: str | None = Field(default=None, max_length=80)
    postal_code: str | None = Field(default=None, max_length=20)
    country_code: str | None = Field(default=None, max_length=2)
    notes: str | None = None
    primary_login_subscriber_id: UUID | None = None


class OrganizationRead(OrganizationBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    primary_login_subscriber_id: UUID | None = None
    created_at: datetime
    updated_at: datetime


class ResellerBase(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    code: str | None = Field(default=None, max_length=60)
    contact_email: str | None = Field(default=None, max_length=255)
    contact_phone: str | None = Field(default=None, max_length=40)
    policy_set_id: UUID | None = None
    is_active: bool = True
    restrict_to_assigned_offers: bool | None = None
    notes: str | None = None


class ResellerCreate(ResellerBase):
    pass


class ResellerUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    code: str | None = Field(default=None, max_length=60)
    contact_email: str | None = Field(default=None, max_length=255)
    contact_phone: str | None = Field(default=None, max_length=40)
    policy_set_id: UUID | None = None
    is_active: bool | None = None
    restrict_to_assigned_offers: bool | None = None
    notes: str | None = None


class ResellerRead(ResellerBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class ResellerSyncRead(BaseModel):
    """Minimal reseller contract for cross-application synchronization."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    code: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    is_active: bool
    updated_at: datetime


class SubscriberBase(BaseModel):
    """Unified subscriber model - combines identity, account, and billing."""

    # Identity fields
    first_name: str = Field(min_length=1, max_length=80)
    last_name: str = Field(min_length=1, max_length=80)
    display_name: str | None = Field(default=None, max_length=120)
    avatar_url: str | None = Field(default=None, max_length=512)
    company_name: str | None = Field(default=None, max_length=160)
    legal_name: str | None = Field(default=None, max_length=200)
    tax_id: str | None = Field(default=None, max_length=80)
    domain: str | None = Field(default=None, max_length=120)
    website: str | None = Field(default=None, max_length=255)
    email: EmailStr
    email_verified: bool = False
    phone: str | None = Field(default=None, max_length=40)
    nin: str | None = Field(default=None, max_length=11)
    date_of_birth: date | None = None
    gender: Gender = Gender.unknown
    preferred_contact_method: ContactMethod | None = None
    locale: str | None = Field(default=None, max_length=16)
    timezone: str | None = Field(default=None, max_length=64)

    # Contact address
    address_line1: str | None = Field(default=None, max_length=120)
    address_line2: str | None = Field(default=None, max_length=120)
    city: str | None = Field(default=None, max_length=80)
    region: str | None = Field(default=None, max_length=80)
    # NCC Local Government Area. Validated against `region` by the subscriber
    # service (the merged-state gate); blank when unknown, never guessed.
    lga: str | None = Field(default=None, max_length=80)
    postal_code: str | None = Field(default=None, max_length=20)
    country_code: str | None = Field(default=None, max_length=2)

    # Service location (POP site determines NAS/IP pool for provisioning)
    pop_site_id: UUID | None = None

    # Account fields
    subscriber_number: str | None = Field(default=None, max_length=80)
    account_number: str | None = Field(default=None, max_length=80)
    account_start_date: datetime | None = None
    status: SubscriberStatus = SubscriberStatus.active
    category: SubscriberCategory = SubscriberCategory.residential
    is_active: bool = True
    marketing_opt_in: bool = False

    # Reseller
    reseller_id: UUID | None = None

    # Billing fields
    tax_rate_id: UUID | None = None
    billing_enabled: bool = True
    captive_redirect_enabled: bool = False
    billing_name: str | None = Field(default=None, max_length=160)
    billing_address_line1: str | None = Field(default=None, max_length=160)
    billing_address_line2: str | None = Field(default=None, max_length=120)
    billing_city: str | None = Field(default=None, max_length=80)
    billing_region: str | None = Field(default=None, max_length=80)
    billing_postal_code: str | None = Field(default=None, max_length=20)
    billing_country_code: str | None = Field(default=None, max_length=2)

    # Payment settings
    billing_mode: BillingMode = BillingMode.prepaid
    payment_method: str | None = Field(default=None, max_length=80)
    billing_day: int | None = None
    payment_due_days: int | None = None
    grace_period_days: int | None = None
    min_balance: Decimal | None = None

    notes: str | None = None
    metadata_: dict | None = Field(default=None, serialization_alias="metadata")

    @field_validator("nin", mode="before")
    @classmethod
    def _validate_nin(cls, value):
        if value is None:
            return None
        normalized = normalize_nin(str(value))
        if not normalized:
            return None
        if len(normalized) != 11:
            raise ValueError("NIN must be exactly 11 digits")
        return normalized


class SubscriberCreate(SubscriberBase):
    # Backwards-compat: allow "create" to target an existing person/subscriber row.
    person_id: UUID | None = Field(default=None, exclude=True)

    # If person_id is provided these can be omitted; service will update the existing row.
    first_name: str | None = Field(default=None, min_length=1, max_length=80)  # type: ignore[assignment]
    last_name: str | None = Field(default=None, min_length=1, max_length=80)  # type: ignore[assignment]
    email: EmailStr | None = None  # type: ignore[assignment]

    @model_validator(mode="after")
    def _require_identity_when_creating_new(self) -> SubscriberCreate:
        if self.person_id:
            return self
        if not self.first_name or not self.last_name or not self.email:
            raise ValueError(
                "first_name, last_name, and email are required when person_id is not provided."
            )
        return self


class SubscriberUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Identity fields
    first_name: str | None = Field(default=None, min_length=1, max_length=80)
    last_name: str | None = Field(default=None, min_length=1, max_length=80)
    display_name: str | None = Field(default=None, max_length=120)
    avatar_url: str | None = Field(default=None, max_length=512)
    company_name: str | None = Field(default=None, max_length=160)
    legal_name: str | None = Field(default=None, max_length=200)
    tax_id: str | None = Field(default=None, max_length=80)
    domain: str | None = Field(default=None, max_length=120)
    website: str | None = Field(default=None, max_length=255)
    email: EmailStr | None = None
    email_verified: bool | None = None
    phone: str | None = Field(default=None, max_length=40)
    nin: str | None = Field(default=None, max_length=11)
    date_of_birth: date | None = None
    gender: Gender | None = None
    preferred_contact_method: ContactMethod | None = None
    locale: str | None = Field(default=None, max_length=16)
    timezone: str | None = Field(default=None, max_length=64)

    # Contact address
    address_line1: str | None = Field(default=None, max_length=120)
    address_line2: str | None = Field(default=None, max_length=120)
    city: str | None = Field(default=None, max_length=80)
    region: str | None = Field(default=None, max_length=80)
    # NCC Local Government Area. Validated against `region` by the subscriber
    # service (the merged-state gate); blank when unknown, never guessed.
    lga: str | None = Field(default=None, max_length=80)
    postal_code: str | None = Field(default=None, max_length=20)
    country_code: str | None = Field(default=None, max_length=2)

    # Service location
    pop_site_id: UUID | None = None

    # Account fields
    subscriber_number: str | None = Field(default=None, max_length=80)
    account_number: str | None = Field(default=None, max_length=80)
    account_start_date: datetime | None = None
    category: SubscriberCategory | None = None
    marketing_opt_in: bool | None = None

    # Reseller
    reseller_id: UUID | None = None

    # Billing fields
    tax_rate_id: UUID | None = None
    billing_enabled: bool | None = None
    captive_redirect_enabled: bool | None = None
    billing_name: str | None = Field(default=None, max_length=160)
    billing_address_line1: str | None = Field(default=None, max_length=160)
    billing_address_line2: str | None = Field(default=None, max_length=120)
    billing_city: str | None = Field(default=None, max_length=80)
    billing_region: str | None = Field(default=None, max_length=80)
    billing_postal_code: str | None = Field(default=None, max_length=20)
    billing_country_code: str | None = Field(default=None, max_length=2)

    # Payment settings
    billing_mode: BillingMode | None = None
    payment_method: str | None = Field(default=None, max_length=80)
    billing_day: int | None = None
    payment_due_days: int | None = None
    grace_period_days: int | None = None
    min_balance: Decimal | None = None

    notes: str | None = None
    metadata_: dict | None = Field(default=None, serialization_alias="metadata")

    @field_validator("nin", mode="before")
    @classmethod
    def _validate_nin(cls, value):
        if value is None:
            return None
        normalized = normalize_nin(str(value))
        if not normalized:
            return None
        if len(normalized) != 11:
            raise ValueError("NIN must be exactly 11 digits")
        return normalized


class SubscriberBillingApprovalUpdate(BaseModel):
    """Explicit account billing/service lifecycle transition request."""

    model_config = ConfigDict(extra="forbid")

    approved: bool
    reason: str = Field(min_length=1, max_length=500)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=500)


class SubscriberBillingApprovalRead(BaseModel):
    account_id: UUID
    approved: bool
    prior_approved: bool
    prior_status: SubscriberStatus
    status: SubscriberStatus
    action: str
    affected_subscription_ids: tuple[UUID, ...]


class SubscriberAccountStatusPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str = Field(min_length=1, max_length=20)


class SubscriberAccountStatusPreviewRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    account_id: UUID
    action: str
    current_status: SubscriberStatus
    current_override: SubscriberStatus | None
    projected_status: SubscriberStatus
    clears_override: bool
    allowed: bool
    fingerprint: str


class SubscriberAccountStatusConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str = Field(min_length=1, max_length=20)
    reason: str = Field(min_length=1, max_length=500)
    preview_fingerprint: str = Field(min_length=64, max_length=64)
    idempotency_key: str = Field(min_length=1, max_length=120)


class SubscriberAccountStatusOutcomeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    account_id: UUID
    action: str
    prior_status: SubscriberStatus
    status: SubscriberStatus
    prior_override: SubscriberStatus | None
    lifecycle_override: SubscriberStatus | None
    replayed: bool


class SubscriberRead(SubscriberBase):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    # Override `email` as a plain string for reads. The DB column is a plain
    # text column and may hold imported placeholder values (e.g. legacy
    # `no-email+<id>@splynx.local`) that EmailStr's strict validator rejects
    # as a "special-use TLD". Keep strict EmailStr on create/update payloads.
    email: str  # type: ignore[assignment]

    id: UUID
    lifecycle_override_status: SubscriberStatus | None = None
    lifecycle_override_reason: str | None = None
    lifecycle_override_source: str | None = None
    lifecycle_override_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    addresses: list[AddressRead] = Field(default_factory=list)
    channels: list[SubscriberChannelRead] = Field(default_factory=list)
    custom_fields: list[SubscriberCustomFieldRead] = Field(default_factory=list)


class SubscriberSyncRead(BaseModel):
    """Bounded subscriber projection consumed by accounting integrations."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    first_name: str
    last_name: str
    display_name: str | None = None
    company_name: str | None = None
    legal_name: str | None = None
    email: str
    phone: str | None = None
    status: SubscriberStatus
    category: SubscriberCategory
    is_active: bool
    reseller_id: UUID | None = None
    tax_id: str | None = None
    subscriber_number: str | None = None
    account_number: str | None = None
    address_line1: str | None = None
    address_line2: str | None = None
    city: str | None = None
    region: str | None = None
    postal_code: str | None = None
    country_code: str | None = None
    created_at: datetime
    updated_at: datetime


class SubscriberChannelBase(BaseModel):
    """Communication channel for a subscriber."""

    subscriber_id: UUID
    channel_type: ChannelType
    address: str = Field(min_length=1, max_length=255)
    label: str | None = Field(default=None, max_length=60)
    is_primary: bool = False
    is_verified: bool = False


class SubscriberChannelCreate(SubscriberChannelBase):
    pass


class SubscriberChannelUpdate(BaseModel):
    subscriber_id: UUID | None = None
    channel_type: ChannelType | None = None
    address: str | None = Field(default=None, min_length=1, max_length=255)
    label: str | None = Field(default=None, max_length=60)
    is_primary: bool | None = None
    is_verified: bool | None = None


class SubscriberChannelRead(SubscriberChannelBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    verified_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class AddressBase(BaseModel):
    subscriber_id: UUID
    tax_rate_id: UUID | None = None
    address_type: AddressType = AddressType.service
    label: str | None = Field(default=None, max_length=120)
    address_line1: str = Field(min_length=1, max_length=120)
    address_line2: str | None = Field(default=None, max_length=120)
    city: str | None = Field(default=None, max_length=80)
    region: str | None = Field(default=None, max_length=80)
    # NCC Local Government Area — see SubscriberBase.lga.
    lga: str | None = Field(default=None, max_length=80)
    postal_code: str | None = Field(default=None, max_length=20)
    country_code: str | None = Field(default=None, max_length=2)
    latitude: float | None = None
    longitude: float | None = None
    is_primary: bool = False


class AddressCreate(AddressBase):
    pass


class AddressUpdate(BaseModel):
    subscriber_id: UUID | None = None
    tax_rate_id: UUID | None = None
    address_type: AddressType | None = None
    label: str | None = Field(default=None, max_length=120)
    address_line1: str | None = Field(default=None, min_length=1, max_length=120)
    address_line2: str | None = Field(default=None, max_length=120)
    city: str | None = Field(default=None, max_length=80)
    region: str | None = Field(default=None, max_length=80)
    # NCC Local Government Area — see SubscriberBase.lga.
    lga: str | None = Field(default=None, max_length=80)
    postal_code: str | None = Field(default=None, max_length=20)
    country_code: str | None = Field(default=None, max_length=2)
    latitude: float | None = None
    longitude: float | None = None
    is_primary: bool | None = None


class AddressRead(AddressBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class SubscriberCustomFieldBase(BaseModel):
    subscriber_id: UUID
    key: str = Field(min_length=1, max_length=120)
    value_text: str | None = None
    value_json: dict | None = None
    is_active: bool = True


class SubscriberCustomFieldCreate(SubscriberCustomFieldBase):
    pass


class SubscriberCustomFieldUpdate(BaseModel):
    subscriber_id: UUID | None = None
    key: str | None = Field(default=None, min_length=1, max_length=120)
    value_text: str | None = None
    value_json: dict | None = None
    is_active: bool | None = None


class SubscriberCustomFieldRead(SubscriberCustomFieldBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


# --- Compatibility schemas ---
# Accounts are represented by subscribers; these request/response shapes
# remain to preserve API/import compatibility.


class SubscriberAccountCreate(BaseModel):
    """Compatibility create payload for account-as-subscriber flows."""

    subscriber_id: UUID | None = None
    reseller_id: UUID | None = None
    account_number: str | None = None
    notes: str | None = None


class SubscriberAccountUpdate(BaseModel):
    """Compatibility update payload for account-as-subscriber flows."""

    pass


class SubscriberAccountRead(BaseModel):
    """Compatibility read payload for account-as-subscriber flows."""

    model_config = ConfigDict(from_attributes=True)
    id: UUID | None = None


# --- Subscriber contacts (customer self-care: /api/v1/me/contacts) ---
# Mirrors the web portal's /portal/contacts feature (see
# app/services/customer_portal_contacts.py). Channels are normalized and the
# "at least one contact channel" rule is enforced server-side by the service;
# the response is serialized straight from the SubscriberContact model.

_CONTACT_CHANNEL_FIELDS = (
    "phone",
    "email",
    "whatsapp",
    "facebook",
    "instagram",
    "x_handle",
    "telegram",
    "linkedin",
    "other_social",
)


class SubscriberContactBase(BaseModel):
    """Editable fields of a subscriber contact (mirrors the web ContactForm)."""

    full_name: str | None = Field(default=None, max_length=160)
    phone: str | None = Field(default=None, max_length=40)
    email: str | None = Field(default=None, max_length=255)
    whatsapp: str | None = Field(default=None, max_length=80)
    facebook: str | None = Field(default=None, max_length=160)
    instagram: str | None = Field(default=None, max_length=160)
    x_handle: str | None = Field(default=None, max_length=160)
    telegram: str | None = Field(default=None, max_length=160)
    linkedin: str | None = Field(default=None, max_length=160)
    other_social: str | None = None
    relationship: str | None = Field(default=None, max_length=80)
    contact_type: str | None = Field(default="general", max_length=40)
    is_authorized: bool = False
    receives_notifications: bool = False
    is_billing_contact: bool = False
    notes: str | None = None


class SubscriberContactCreate(SubscriberContactBase):
    """Create payload — must carry at least one contact channel."""


class SubscriberContactUpdate(SubscriberContactBase):
    """Update payload — full replace, must carry at least one contact channel."""


class SubscriberContactRead(BaseModel):
    """A subscriber contact as returned by the customer self-care API."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    subscriber_id: UUID
    full_name: str | None = None
    phone: str | None = None
    email: str | None = None
    whatsapp: str | None = None
    facebook: str | None = None
    instagram: str | None = None
    x_handle: str | None = None
    telegram: str | None = None
    linkedin: str | None = None
    other_social: str | None = None
    relationship: str | None = None
    contact_type: str
    is_billing_contact: bool
    is_authorized: bool
    receives_notifications: bool
    notes: str | None = None
    created_at: datetime
    updated_at: datetime


class SubscriberContactWriteResponse(BaseModel):
    """Create/update response: the saved contact plus any duplicate warnings."""

    contact: SubscriberContactRead
    warnings: list[str] = Field(default_factory=list)


class AccountDeletionRequest(BaseModel):
    """Optional reason for an in-app account-deletion request."""

    reason: str | None = Field(default=None, max_length=500)


class AccountDeletionResponse(BaseModel):
    status: str
    requested_at: datetime
    already_requested: bool = False
    detail: str = (
        "Your account deletion request has been received. We will end your "
        "service and delete your personal data per our privacy policy."
    )
