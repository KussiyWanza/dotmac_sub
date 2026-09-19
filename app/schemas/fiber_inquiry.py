"""Typed contract for signed fiber.dotmac.ng inquiry ingress."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)


class FiberInquiryInterest(StrEnum):
    new_connection = "new_connection"
    technical_support = "technical_support"
    billing = "billing"
    enterprise_services = "enterprise_services"
    academy = "academy"
    other = "other"

    @property
    def label(self) -> str:
        return self.value.replace("_", " ").title()


class FiberAttribution(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)

    journey_id: UUID
    utm_source: str | None = Field(default=None, max_length=200)
    utm_medium: str | None = Field(default=None, max_length=200)
    utm_campaign: str | None = Field(default=None, max_length=200)
    utm_content: str | None = Field(default=None, max_length=200)
    utm_term: str | None = Field(default=None, max_length=200)
    campaign_id: str | None = Field(default=None, max_length=200)
    ad_set_id: str | None = Field(default=None, max_length=200)
    ad_id: str | None = Field(default=None, max_length=200)
    click_id: str | None = Field(default=None, max_length=255)
    landing_path: str = Field(min_length=1, max_length=500, pattern=r"^/[^?#]*$")
    captured_at: datetime

    @field_validator(
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_content",
        "utm_term",
        "campaign_id",
        "ad_set_id",
        "ad_id",
        "click_id",
        mode="before",
    )
    @classmethod
    def empty_attribution_text_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("captured_at")
    @classmethod
    def captured_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("attribution.captured_at must include a timezone")
        return value


class FiberLocation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)

    address: str = Field(min_length=5, max_length=1000)
    area: str = Field(min_length=2, max_length=80)
    latitude: Decimal | None = Field(default=None, ge=-90, le=90)
    longitude: Decimal | None = Field(default=None, ge=-180, le=180)

    @model_validator(mode="after")
    def coordinates_are_a_pair(self) -> FiberLocation:
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("latitude and longitude must be supplied together")
        return self


class FiberSelectedPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)

    name: str = Field(min_length=1, max_length=200)


class FiberInquiryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)

    form_version: str = Field(pattern=r"^fiber-(?:contact|coverage)-v1$")
    full_name: str = Field(min_length=2, max_length=200)
    phone: str | None = Field(default=None, max_length=40)
    email: EmailStr | None = None
    interest: FiberInquiryInterest
    message: str | None = Field(default=None, max_length=4000)
    submitted_at: datetime
    attribution: FiberAttribution | None = None
    location: FiberLocation | None = None
    selected_plan: FiberSelectedPlan | None = None

    @field_validator("phone", "email", "message", mode="before")
    @classmethod
    def empty_text_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("submitted_at")
    @classmethod
    def submitted_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("submitted_at must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_version_contract(self) -> FiberInquiryRequest:
        if self.form_version == "fiber-contact-v1":
            if self.email is None:
                raise ValueError("email is required for fiber-contact-v1")
            if any(
                value is not None
                for value in (self.attribution, self.location, self.selected_plan)
            ):
                raise ValueError(
                    "coverage attribution, location, and plan are not part of "
                    "fiber-contact-v1"
                )
            return self
        if self.phone is None:
            raise ValueError("phone is required for fiber-coverage-v1")
        if (
            self.attribution is None
            or self.location is None
            or self.selected_plan is None
        ):
            raise ValueError(
                "attribution, location, and selected_plan are required for "
                "fiber-coverage-v1"
            )
        if self.attribution.captured_at > self.submitted_at:
            raise ValueError("attribution.captured_at cannot be after submitted_at")
        if self.submitted_at - self.attribution.captured_at > timedelta(days=30):
            raise ValueError("attribution capture must be within the 30-day window")
        return self

    @property
    def is_coverage_request(self) -> bool:
        return self.form_version == "fiber-coverage-v1"


class FiberCoverageStatus(StrEnum):
    covered = "covered"
    survey_required = "survey_required"
    out_of_area = "out_of_area"


class FiberCoverageResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: FiberCoverageStatus
    summary: str


class FiberInquiryReceipt(BaseModel):
    observation_id: UUID
    conversation_id: UUID
    message_id: UUID
    replayed: bool
    resolution_status: str
    reference: str | None = None
    coverage: FiberCoverageResult | None = None
