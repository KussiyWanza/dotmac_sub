"""Strict contracts for customer-facing Team Inbox AI intake."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

GENERIC_FOLLOW_UP_QUESTION = (
    "Please tell us whether your request is about your internet connection, "
    "payment, subscription, account, or a new installation."
)
CUSTOMER_TYPE_FOLLOW_UP_QUESTION = (
    "Is this new internet request for you personally or for an organization?"
)
APPROVED_FOLLOW_UP_QUESTIONS = frozenset(
    {GENERIC_FOLLOW_UP_QUESTION, CUSTOMER_TYPE_FOLLOW_UP_QUESTION}
)


class AiIntakeIntent(StrEnum):
    technical_support = "technical_support"
    billing_issue = "billing_issue"
    payment_confirmation = "payment_confirmation"
    subscription_renewal = "subscription_renewal"
    plan_change = "plan_change"
    coverage_request = "coverage_request"
    new_connection = "new_connection"
    account_access = "account_access"
    complaint = "complaint"
    general_enquiry = "general_enquiry"
    unknown = "unknown"


class AiIntakePartyType(StrEnum):
    individual = "individual"
    organization = "organization"
    unknown = "unknown"


class AiIntakeCategory(StrEnum):
    no_internet = "no_internet"
    slow_internet = "slow_internet"
    intermittent_connection = "intermittent_connection"
    router_issue = "router_issue"
    relocation = "relocation"
    other_technical_issue = "other_technical_issue"
    payment_not_reflected = "payment_not_reflected"
    invoice_request = "invoice_request"
    subscription_expired = "subscription_expired"
    renewal_request = "renewal_request"
    plan_change_request = "plan_change_request"
    login_problem = "login_problem"
    account_information = "account_information"
    other_billing_issue = "other_billing_issue"
    payment_confirmation = "payment_confirmation"
    coverage_request = "coverage_request"
    new_connection = "new_connection"
    complaint = "complaint"
    general_enquiry = "general_enquiry"
    unknown = "unknown"


class AiIntakeStatus(StrEnum):
    skipped = "skipped"
    classifying = "classifying"
    awaiting_follow_up = "awaiting_follow_up"
    classified = "classified"
    fallback = "fallback"
    failed = "failed"
    escalated = "escalated"


class DataCleaningState(StrEnum):
    """Reserved conversation steps for the future contact-data cleaning flow."""

    idle = "idle"
    identify_pending = "identify_pending"
    verify_pending = "verify_pending"
    collect_pending = "collect_pending"
    saving = "saving"
    confirmed = "confirmed"
    escalated = "escalated"


class DataCleaningEligibilityReason(StrEnum):
    eligible = "eligible"
    no_matching_config = "no_matching_config"
    intake_disabled = "intake_disabled"
    unsupported_channel = "unsupported_channel"
    routing_disabled = "routing_disabled"
    support_team_not_configured = "support_team_not_configured"
    support_team_unavailable = "support_team_unavailable"
    conversation_team_not_set = "conversation_team_not_set"
    conversation_team_mismatch = "conversation_team_mismatch"
    subscriber_not_linked = "subscriber_not_linked"
    subscriber_ineligible = "subscriber_ineligible"
    no_missing_profile_fields = "no_missing_profile_fields"
    collection_disabled = "collection_disabled"
    invalid_configuration = "invalid_configuration"


class AiIntakeReason(StrEnum):
    no_matching_config = "no_matching_config"
    disabled = "disabled"
    unsupported_channel = "unsupported_channel"
    routing_disabled = "routing_disabled"
    campaign_excluded = "campaign_excluded"
    active_owner = "active_owner"
    existing_conversation = "existing_conversation"
    classified = "classified"
    low_confidence = "low_confidence"
    follow_up_limit_reached = "follow_up_limit_reached"
    gateway_unavailable = "gateway_unavailable"
    invalid_model_output = "invalid_model_output"
    invalid_configuration = "invalid_configuration"
    context_error = "context_error"
    fallback_timeout = "fallback_timeout"
    no_text_content = "no_text_content"


class AiIntakeContextMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    direction: str = Field(pattern="^(inbound|outbound)$")
    body: str = Field(min_length=1, max_length=1200)


class AiIntakeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    channel_type: str = Field(min_length=1, max_length=40)
    provider: str = Field(min_length=1, max_length=80)
    account_scope: str = Field(min_length=1, max_length=160)
    inbound_message_id: str = Field(min_length=1, max_length=255)
    body: str = Field(min_length=1, max_length=4000)
    conversation_id: UUID | None = None
    recent_messages: tuple[AiIntakeContextMessage, ...] = ()
    conversation_tags: tuple[str, ...] = ()
    campaign_attributed: bool = False
    routing_allows_ai: bool = True
    created_conversation: bool = True
    has_active_assignment: bool = False
    awaiting_follow_up: bool = False
    follow_up_count: Annotated[int, Field(ge=0, le=10)] = 0


class AiProviderClassification(BaseModel):
    """Untrusted provider output after strict JSON parsing."""

    model_config = ConfigDict(extra="forbid")

    intent: AiIntakeIntent
    category: AiIntakeCategory
    confidence: Annotated[float, Field(strict=True, ge=0.0, le=1.0)]
    department: str | None = Field(default=None, max_length=80)
    requires_follow_up: StrictBool
    follow_up_question: str | None = Field(default=None, max_length=300)
    summary: str | None = Field(default=None, max_length=500)
    party_type: AiIntakePartyType = AiIntakePartyType.unknown
    party_type_confidence: Annotated[float, Field(strict=True, ge=0.0, le=1.0)] = 0.0

    @model_validator(mode="after")
    def validate_follow_up_shape(self) -> AiProviderClassification:
        if not self.requires_follow_up and self.follow_up_question:
            raise ValueError("follow_up_question requires requires_follow_up=true")
        return self


class AiIntakeClassification(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    intent: AiIntakeIntent
    category: AiIntakeCategory
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]
    department: str | None = Field(default=None, max_length=80)
    department_team_id: UUID | None = None
    requires_follow_up: bool
    follow_up_question: str | None = Field(default=None, max_length=300)
    summary: str | None = Field(default=None, max_length=500)
    party_type: AiIntakePartyType = AiIntakePartyType.unknown
    party_type_confidence: Annotated[float, Field(ge=0.0, le=1.0)] = 0.0

    @model_validator(mode="after")
    def validate_follow_up_shape(self) -> AiIntakeClassification:
        if not self.requires_follow_up and self.follow_up_question:
            raise ValueError("follow_up_question requires requires_follow_up=true")
        return self


class AiIntakeOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: AiIntakeStatus
    reason: AiIntakeReason
    config_id: UUID | None = None
    classification: AiIntakeClassification | None = None
    fallback_team_id: UUID | None = None
    follow_up_count: Annotated[int, Field(ge=0, le=10)] = 0
    fallback_due_at: datetime | None = None
    provider: str | None = Field(default=None, max_length=80)
    model: str | None = Field(default=None, max_length=160)
    duration_ms: Annotated[int, Field(ge=0)] = 0


class DataCleaningEligibility(BaseModel):
    """Typed, side-effect-free eligibility result for the future flow."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    eligible: bool
    state: DataCleaningState = DataCleaningState.idle
    reason: DataCleaningEligibilityReason
    config_id: UUID | None = None
    support_team_id: UUID | None = None
