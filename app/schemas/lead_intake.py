"""Strict transport contracts for Inbox lead-intake forms and AI observations."""

from datetime import date
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class LeadIntakePartyType(StrEnum):
    individual = "individual"
    organization = "organization"
    unknown = "unknown"


class LeadIntakeIntent(StrEnum):
    new_connection = "new_connection"
    coverage_request = "coverage_request"
    other = "other"


class AiLeadIntakeClassification(BaseModel):
    # Provider JSON contains string enum values. Pydantic still enforces the
    # closed vocabulary and numeric bounds while admitting that wire format.
    model_config = ConfigDict(extra="forbid")
    intent: LeadIntakeIntent
    intent_confidence: float = Field(ge=0, le=1)
    party_type: LeadIntakePartyType
    party_type_confidence: float = Field(ge=0, le=1)
    clarification_question: str | None = Field(default=None, max_length=240)


class LeadIntakeTemplateDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    party_type: LeadIntakePartyType
    name: str = Field(min_length=1, max_length=160)
    heading: str = Field(min_length=1, max_length=200)
    introduction: str | None = Field(default=None, max_length=2_000)
    privacy_notice: str = Field(min_length=1, max_length=2_000)
    invitation_message: str = Field(min_length=1, max_length=2_000)
    confirmation_message: str = Field(min_length=1, max_length=2_000)
    thank_you_message: str = Field(min_length=1, max_length=2_000)
    target_service_team_id: UUID
    owner_system_user_id: UUID | None = None
    pipeline_id: UUID | None = None
    stage_id: UUID | None = None

    @model_validator(mode="after")
    def validate_template(self):
        if self.party_type is LeadIntakePartyType.unknown:
            raise ValueError("A template must target an individual or organization")
        if (self.pipeline_id is None) != (self.stage_id is None):
            raise ValueError("Pipeline and Stage must be selected together")
        if "{link}" not in self.invitation_message:
            raise ValueError("Invitation message must contain {link}")
        return self


class ResolvedLeadIntakeAddress(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    display_name: str = Field(min_length=1, max_length=500)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    state: str = Field(min_length=1, max_length=80)
    country_code: str = Field(min_length=2, max_length=2)


class LeadIntakeSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    full_name: str | None = Field(default=None, max_length=200)
    gender: str | None = Field(default=None, max_length=24)
    date_of_birth: date | None = None
    organization_name: str | None = Field(default=None, max_length=200)
    representative_name: str | None = Field(default=None, max_length=200)
    representative_role: str | None = Field(default=None, max_length=120)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    address_confirmation: bool
    privacy_acknowledged: bool
