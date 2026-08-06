"""Leads / pipeline / quotes schemas — CRM port.

Ported from ``dotmac_crm/app/schemas/crm/sales.py`` and evolved natively. Leads
identify a reviewed Party before an account is required; Subscriber remains
the account link for Quote and SalesOrder. Native campaign UUIDs and external
provider attribution are separated by the structured origin contract.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.party import PartyContactPointType, PartyType
from app.models.project import ProjectType
from app.models.sales import (
    LeadCaptureMethod,
    LeadSourcePlatform,
    LeadStatus,
    QuoteDiscountType,
    QuoteStatus,
)
from app.schemas.subscriber import SubscriberCreate


class PipelineBase(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    is_active: bool = True
    metadata_: dict | None = Field(default=None, serialization_alias="metadata")


class PipelineCreate(PipelineBase):
    pass


class PipelineUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    is_active: bool | None = None
    metadata_: dict | None = Field(default=None, serialization_alias="metadata")


class PipelineRead(PipelineBase):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class PipelineStageBase(BaseModel):
    pipeline_id: UUID
    name: str = Field(min_length=1, max_length=160)
    order_index: int = 0
    is_active: bool = True
    default_probability: int = Field(default=50, ge=0, le=100)
    metadata_: dict | None = Field(default=None, serialization_alias="metadata")


class PipelineStageCreate(PipelineStageBase):
    pass


class PipelineStageUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    order_index: int | None = None
    is_active: bool | None = None
    default_probability: int | None = Field(default=None, ge=0, le=100)
    metadata_: dict | None = Field(default=None, serialization_alias="metadata")


class PipelineStageRead(PipelineStageBase):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class LeadBase(BaseModel):
    """Lead linked to Party identity; Subscriber is optional account context."""

    subscriber_id: UUID | None = None
    pipeline_id: UUID | None = None
    stage_id: UUID | None = None
    # CrmAgent UUID carried verbatim — inbox model.
    owner_agent_id: UUID | None = None
    title: str | None = Field(default=None, max_length=200)
    status: LeadStatus = LeadStatus.new
    estimated_value: Decimal | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    probability: int | None = Field(default=None, ge=0, le=100)
    expected_close_date: date | None = None
    lost_reason: str | None = Field(default=None, max_length=200)
    lead_source: str | None = Field(default=None, max_length=40)
    campaign_id: UUID | None = None
    campaign_recipient_id: UUID | None = None
    region: str | None = Field(default=None, max_length=80)
    address: str | None = None
    notes: str | None = None
    metadata_: dict | None = Field(default=None, serialization_alias="metadata")
    is_active: bool = True


class LeadOriginCaptureCreate(BaseModel):
    capture_method: LeadCaptureMethod
    source_platform: LeadSourcePlatform
    integration_inbox_id: UUID | None = None
    source_interaction_id: str | None = Field(default=None, max_length=240)
    campaign_id: UUID | None = None
    campaign_recipient_id: UUID | None = None
    external_campaign_id: str | None = Field(default=None, max_length=200)
    external_ad_set_id: str | None = Field(default=None, max_length=200)
    external_ad_id: str | None = Field(default=None, max_length=200)
    external_form_id: str | None = Field(default=None, max_length=200)
    external_click_id: str | None = Field(default=None, max_length=255)
    utm_source: str | None = Field(default=None, max_length=200)
    utm_medium: str | None = Field(default=None, max_length=200)
    utm_campaign: str | None = Field(default=None, max_length=200)
    utm_content: str | None = Field(default=None, max_length=200)
    utm_term: str | None = Field(default=None, max_length=200)
    landing_path: str | None = Field(default=None, max_length=500)
    captured_at: datetime | None = None
    capture_source: str = Field(min_length=1, max_length=80)
    capture_reason: str = Field(min_length=1)


class LeadContactObservation(BaseModel):
    channel_type: PartyContactPointType
    value: str = Field(min_length=1, max_length=320)
    display_value: str | None = Field(default=None, max_length=320)
    provider: str | None = Field(default=None, max_length=80)
    provider_account_id: str | None = Field(default=None, max_length=160)
    external_subject_id: str | None = Field(default=None, max_length=240)
    is_primary: bool = False


class LeadCapturePartyCreate(BaseModel):
    party_type: PartyType = PartyType.person
    display_name: str = Field(min_length=1, max_length=200)
    contacts: list[LeadContactObservation] = Field(default_factory=list, max_length=10)


class LeadCaptureRequest(BaseModel):
    party_id: UUID | None = None
    party: LeadCapturePartyCreate | None = None
    title: str = Field(min_length=1, max_length=200)
    lead_source: str = Field(min_length=1, max_length=40)
    origin: LeadOriginCaptureCreate
    region: str | None = Field(default=None, max_length=80)
    address: str | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def _one_party_source_and_interaction(self) -> LeadCaptureRequest:
        if (self.party_id is None) == (self.party is None):
            raise ValueError("Supply exactly one of party_id or party")
        if not str(self.origin.source_interaction_id or "").strip():
            raise ValueError("origin.source_interaction_id is required")
        return self


class LeadCaptureRead(BaseModel):
    lead_id: UUID
    party_id: UUID
    origin_capture_id: UUID
    replayed: bool


class LeadAccountConversionRequest(BaseModel):
    party_id: UUID
    subscriber_id: UUID | None = None
    new_account: SubscriberCreate | None = None

    @model_validator(mode="after")
    def _one_account_target(self) -> LeadAccountConversionRequest:
        if (self.subscriber_id is None) == (self.new_account is None):
            raise ValueError("Supply exactly one of subscriber_id or new_account")
        return self


class LeadAccountConversionRead(BaseModel):
    lead_id: UUID
    party_id: UUID
    subscriber_id: UUID
    outcome: str


class LeadCreate(LeadBase):
    party_id: UUID | None = None
    party_binding_source: str | None = Field(default=None, max_length=80)
    party_binding_reason: str | None = None
    origin_capture: LeadOriginCaptureCreate | None = None


class LeadUpdate(BaseModel):
    subscriber_id: UUID | None = None
    pipeline_id: UUID | None = None
    stage_id: UUID | None = None
    owner_agent_id: UUID | None = None
    title: str | None = Field(default=None, max_length=200)
    status: LeadStatus | None = None
    estimated_value: Decimal | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    probability: int | None = Field(default=None, ge=0, le=100)
    expected_close_date: date | None = None
    lost_reason: str | None = Field(default=None, max_length=200)
    lead_source: str | None = Field(default=None, max_length=40)
    region: str | None = Field(default=None, max_length=80)
    address: str | None = None
    notes: str | None = None
    metadata_: dict | None = Field(default=None, serialization_alias="metadata")
    is_active: bool | None = None


class LeadRead(LeadBase):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: UUID
    party_id: UUID | None = None
    weighted_value: Decimal | None = None
    closed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class QuoteBase(BaseModel):
    """Lead-backed commercial offer; Subscriber is attached on acceptance."""

    subscriber_id: UUID | None = None
    lead_id: UUID
    # Staff person UUID carried verbatim — staff map for display.
    owner_person_id: UUID | None = None
    status: QuoteStatus = QuoteStatus.draft
    project_type: ProjectType | None = None
    currency: str = Field(default="NGN", min_length=3, max_length=3)
    subtotal: Decimal = Decimal("0.00")
    tax_rate: Decimal | None = Field(default=None, ge=0, le=100)
    tax_total: Decimal = Decimal("0.00")
    total: Decimal = Decimal("0.00")
    expires_at: datetime | None = None
    sent_at: datetime | None = None
    notes: str | None = None
    metadata_: dict | None = Field(default=None, serialization_alias="metadata")
    is_active: bool = True


class QuoteCreate(QuoteBase):
    project_type: ProjectType


class QuoteUpdate(BaseModel):
    subscriber_id: UUID | None = None
    lead_id: UUID | None = None
    owner_person_id: UUID | None = None
    status: QuoteStatus | None = None
    project_type: ProjectType | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    subtotal: Decimal | None = None
    tax_rate: Decimal | None = Field(default=None, ge=0, le=100)
    tax_total: Decimal | None = None
    total: Decimal | None = None
    expires_at: datetime | None = None
    sent_at: datetime | None = None
    notes: str | None = None
    metadata_: dict | None = Field(default=None, serialization_alias="metadata")
    is_active: bool | None = None


class QuoteRead(QuoteBase):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: UUID
    sales_order_id: UUID | None = None
    discount_type: QuoteDiscountType | None = None
    discount_value: Decimal | None = None
    discount_amount: Decimal = Decimal("0.00")
    discount_reason: str | None = None
    discount_applied_by_system_user_id: UUID | None = None
    discount_applied_at: datetime | None = None
    discount_revision: int = 0
    created_at: datetime
    updated_at: datetime


class QuoteLineItemBase(BaseModel):
    quote_id: UUID
    # CRM inventory UUID carried verbatim while inventory remains externally owned.
    inventory_item_id: UUID | None = None
    description: str = Field(min_length=1, max_length=255)
    quantity: Decimal = Field(default=Decimal("1.000"), gt=0)
    unit_price: Decimal = Field(default=Decimal("0.00"), ge=0)
    amount: Decimal = Field(default=Decimal("0.00"), ge=0)
    metadata_: dict | None = Field(default=None, serialization_alias="metadata")


class QuoteLineItemCreate(QuoteLineItemBase):
    pass


class QuoteLineItemUpdate(BaseModel):
    inventory_item_id: UUID | None = None
    description: str | None = Field(default=None, min_length=1, max_length=255)
    quantity: Decimal | None = Field(default=None, gt=0)
    unit_price: Decimal | None = Field(default=None, ge=0)
    amount: Decimal | None = Field(default=None, ge=0)
    metadata_: dict | None = Field(default=None, serialization_alias="metadata")


class QuoteLineItemRead(QuoteLineItemBase):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: UUID
    # Historical read-only evidence; new Line Items always store zero.
    discount_percent: Decimal = Decimal("0.00")
    created_at: datetime
