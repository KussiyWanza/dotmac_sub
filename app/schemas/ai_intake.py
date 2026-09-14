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
NATURAL_CLARIFICATION_QUESTION = "Could you briefly tell me what you need help with?"
CUSTOMER_TYPE_FOLLOW_UP_QUESTION = (
    "Is this new internet request for you personally or for an organization?"
)
DEFAULT_CLARIFICATION_QUESTIONS = (
    NATURAL_CLARIFICATION_QUESTION,
    CUSTOMER_TYPE_FOLLOW_UP_QUESTION,
)
APPROVED_FOLLOW_UP_QUESTIONS = frozenset(
    {
        NATURAL_CLARIFICATION_QUESTION,
        GENERIC_FOLLOW_UP_QUESTION,
        CUSTOMER_TYPE_FOLLOW_UP_QUESTION,
    }
)


def normalize_clarification_questions(value: object) -> tuple[str, str]:
    """Validate the ordered, customer-visible questions stored in policy JSON."""

    if value is None:
        return DEFAULT_CLARIFICATION_QUESTIONS
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise ValueError("clarification_questions must contain two text values")
    if len(value) != 2 or any(not isinstance(item, str) for item in value):
        raise ValueError("clarification_questions must contain exactly two text values")
    questions = tuple(item.strip() for item in value)
    if any(not item or len(item) > 300 for item in questions):
        raise ValueError("clarification questions must be between 1 and 300 characters")
    return (questions[0], questions[1])


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


class AiIntakeMessageRole(StrEnum):
    customer = "customer"
    ai = "ai"
    human_agent = "human_agent"


class AiIntakeConnectivityState(StrEnum):
    down = "down"
    slow = "slow"
    intermittent = "intermittent"
    working = "working"
    unknown = "unknown"


class AiIntakeDeviceScope(StrEnum):
    all_devices = "all_devices"
    one_device = "one_device"
    some_devices = "some_devices"
    unknown = "unknown"


class AiIntakeConnectionMedium(StrEnum):
    wifi = "wifi"
    ethernet = "ethernet"
    both = "both"
    unknown = "unknown"


class AiIntakeConnectionPattern(StrEnum):
    constant = "constant"
    intermittent = "intermittent"
    unknown = "unknown"


class AiIntakeLosState(StrEnum):
    red = "red"
    not_red = "not_red"
    off = "off"
    unknown = "unknown"


class AiIntakeAffectLevel(StrEnum):
    none = "none"
    mild = "mild"
    moderate = "moderate"
    high = "high"


class AiIntakeAffectSource(StrEnum):
    deterministic = "deterministic"
    model = "model"
    conversation_context = "conversation_context"


class AiIntakeAffectAssessment(BaseModel):
    """Bounded affect evidence; it is never a diagnosis of the customer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    frustration_level: AiIntakeAffectLevel = AiIntakeAffectLevel.none
    agitation_level: AiIntakeAffectLevel = AiIntakeAffectLevel.none
    repeated_complaint: StrictBool = False
    repeated_failed_steps: StrictBool = False
    prior_failed_interaction: StrictBool = False
    sources: tuple[AiIntakeAffectSource, ...] = ()


class AiProviderAffectAssessment(BaseModel):
    """Untrusted provider affect candidate without authoritative provenance."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    frustration_level: AiIntakeAffectLevel = AiIntakeAffectLevel.none
    agitation_level: AiIntakeAffectLevel = AiIntakeAffectLevel.none
    repeated_complaint: StrictBool = False
    repeated_failed_steps: StrictBool = False
    prior_failed_interaction: StrictBool = False


class AiIntakeAnswerStatus(StrEnum):
    pending = "pending"
    answered = "answered"
    partially_answered = "partially_answered"
    unclear = "unclear"
    declined = "declined"
    corrected = "corrected"


class AiIntakeNextAction(StrEnum):
    ask_question = "ask_question"
    provide_guidance = "provide_guidance"
    wait_for_customer = "wait_for_customer"
    resolve = "resolve"
    handoff = "handoff"


class AiIntakeResponsePurpose(StrEnum):
    acknowledgement_question = "acknowledgement_question"
    clarification = "clarification"
    guidance = "guidance"
    status_update = "status_update"
    resolution = "resolution"
    handoff = "handoff"


class AiIntakeStatus(StrEnum):
    skipped = "skipped"
    classifying = "classifying"
    classification_unavailable = "classification_unavailable"
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
    classifier_invalid_output = "classifier_invalid_output"
    classifier_unavailable = "classifier_unavailable"
    classifier_unavailable_after_retries = "classifier_unavailable_after_retries"
    invalid_configuration = "invalid_configuration"
    context_error = "context_error"
    fallback_timeout = "fallback_timeout"
    no_text_content = "no_text_content"


class AiClassifierAttemptStatus(StrEnum):
    not_attempted = "not_attempted"
    accepted = "accepted"
    invalid_output = "invalid_output"
    unavailable = "unavailable"
    no_accepted_intent = "no_accepted_intent"


class AiClassifierFailureKind(StrEnum):
    invalid_model_output = "invalid_model_output"
    schema_validation_failure = "schema_validation_failure"
    classifier_unavailable = "classifier_unavailable"
    no_accepted_intent = "no_accepted_intent"


class AiClassifierValidationIssue(BaseModel):
    """Sanitized provider-schema evidence without customer or completion content."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    location: str = Field(min_length=1, max_length=240)
    error_type: str = Field(min_length=1, max_length=120)
    expected_type: str = Field(min_length=1, max_length=120)
    actual_type: str = Field(min_length=1, max_length=80)


class AiClassifierAttempt(BaseModel):
    """Safe classifier evidence passed from classification into orchestration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: AiClassifierAttemptStatus = AiClassifierAttemptStatus.not_attempted
    reason: AiIntakeReason | None = None
    failure_kind: AiClassifierFailureKind | None = None
    retry_count: Annotated[int, Field(ge=0, le=10)] = 0
    retry_limit: Annotated[int, Field(ge=0, le=5)] = 0
    retries_exhausted: bool = False
    provider: str | None = Field(default=None, max_length=80)
    model: str | None = Field(default=None, max_length=160)
    validation_issues: tuple[AiClassifierValidationIssue, ...] = ()


class AiIntakeContextMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: AiIntakeMessageRole
    body: str = Field(min_length=1, max_length=1200)


class AiIntakeExtractedFacts(BaseModel):
    """Bounded customer-statement facts; never hidden reasoning."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    connectivity_state: AiIntakeConnectivityState = AiIntakeConnectivityState.unknown
    issue_started_when: str | None = Field(default=None, max_length=80)
    device_scope: AiIntakeDeviceScope = AiIntakeDeviceScope.unknown
    connection_medium: AiIntakeConnectionMedium = AiIntakeConnectionMedium.unknown
    connection_pattern: AiIntakeConnectionPattern = AiIntakeConnectionPattern.unknown
    router_powered: StrictBool | None = None
    restart_attempted: StrictBool | None = None
    los_state: AiIntakeLosState = AiIntakeLosState.unknown
    affected_location_or_service: str | None = Field(default=None, max_length=120)
    speed_test_download_mbps: Annotated[
        float | None, Field(default=None, strict=True, ge=0, le=100000)
    ] = None
    speed_test_upload_mbps: Annotated[
        float | None, Field(default=None, strict=True, ge=0, le=100000)
    ] = None
    human_requested: StrictBool = False
    portal_id: str | None = Field(default=None, max_length=32)
    registered_email: str | None = Field(default=None, max_length=254)
    registered_phone: str | None = Field(default=None, max_length=32)
    service_interest: str | None = Field(default=None, max_length=120)
    enquiry_topic: str | None = Field(default=None, max_length=160)
    billing_concern: str | None = Field(default=None, max_length=160)
    invoice_or_charge_reference: str | None = Field(default=None, max_length=80)
    payment_reference: str | None = Field(default=None, max_length=80)
    payment_date: str | None = Field(default=None, max_length=40)
    payment_amount: Annotated[float | None, Field(default=None, strict=True, ge=0)] = (
        None
    )
    renewal_service: str | None = Field(default=None, max_length=120)
    desired_renewal_period: str | None = Field(default=None, max_length=80)
    desired_plan: str | None = Field(default=None, max_length=120)
    coverage_location: str | None = Field(default=None, max_length=160)
    installation_location: str | None = Field(default=None, max_length=160)
    account_access_problem: str | None = Field(default=None, max_length=160)
    complaint_subject: str | None = Field(default=None, max_length=160)
    desired_resolution: str | None = Field(default=None, max_length=160)


class AiIntakeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    channel_type: str = Field(min_length=1, max_length=40)
    provider: str = Field(min_length=1, max_length=80)
    account_scope: str = Field(min_length=1, max_length=160)
    inbound_message_id: str = Field(min_length=1, max_length=255)
    body: str = Field(min_length=1, max_length=4000)
    conversation_id: UUID | None = None
    session_id: UUID | None = None
    policy_version_id: UUID | None = None
    persisted_inbound_message_id: UUID | None = None
    recent_messages: tuple[AiIntakeContextMessage, ...] = ()
    conversation_tags: tuple[str, ...] = ()
    campaign_attributed: bool = False
    routing_allows_ai: bool = True
    created_conversation: bool = True
    active_ai_session: bool = False
    has_active_assignment: bool = False
    awaiting_follow_up: bool = False
    follow_up_count: Annotated[int, Field(ge=0, le=10)] = 0
    classifier_failure_count: Annotated[int, Field(ge=0, le=10)] = 0


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
    message_facts: AiIntakeExtractedFacts = Field(
        default_factory=lambda: AiIntakeExtractedFacts()
    )
    message_affect: AiProviderAffectAssessment = Field(
        default_factory=AiProviderAffectAssessment
    )

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
    message_facts: AiIntakeExtractedFacts = Field(
        default_factory=lambda: AiIntakeExtractedFacts()
    )
    message_affect: AiIntakeAffectAssessment = Field(
        default_factory=AiIntakeAffectAssessment
    )

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
    classifier_attempt: AiClassifierAttempt = Field(default_factory=AiClassifierAttempt)


class AiIntakeSafeCustomerIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    identified: bool = False
    subscriber_status: str | None = Field(default=None, max_length=40)


class AiIntakeRadiusContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    state: str | None = Field(default=None, max_length=40)
    active_session_count: Annotated[int | None, Field(default=None, ge=0)]
    observed_at: datetime | None = None


class AiIntakeOntContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    effective_state: str | None = Field(default=None, max_length=40)


class AiIntakeMonitoringContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: str | None = Field(default=None, max_length=40)
    radius: AiIntakeRadiusContext | None = None
    onts: tuple[AiIntakeOntContext, ...] = ()


class AiIntakePlaybookStepContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str | None = Field(default=None, max_length=120)
    action: AiIntakeNextAction
    approved_instruction: str = Field(min_length=1, max_length=800)
    question_purpose: str | None = Field(default=None, max_length=500)
    expected_fact: str | None = Field(default=None, max_length=80)


class AiCustomerResponseCompositionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    intent: AiIntakeIntent
    category: AiIntakeCategory
    latest_customer_statement: str = Field(min_length=1, max_length=1200)
    recent_messages: tuple[AiIntakeContextMessage, ...] = ()
    facts: AiIntakeExtractedFacts
    missing_fact_keys: tuple[str, ...] = ()
    asked_question_keys: tuple[str, ...] = ()
    troubleshooting_completed: tuple[str, ...] = ()
    customer_identity: AiIntakeSafeCustomerIdentity
    monitoring: AiIntakeMonitoringContext | None = None
    playbook_step: AiIntakePlaybookStepContext
    business_tone: str = Field(min_length=1, max_length=1000)
    approved_isp_information: str | None = Field(default=None, max_length=4000)
    affect: AiIntakeAffectAssessment = Field(default_factory=AiIntakeAffectAssessment)
    acknowledgement_required: bool = False
    issue_acknowledgement_required: bool = False
    issue_acknowledged: bool = False
    frustration_acknowledged: bool = False


class AiProviderCustomerResponse(BaseModel):
    """Untrusted composition output before backend policy validation."""

    model_config = ConfigDict(extra="forbid")

    response_text: str = Field(min_length=1, max_length=800)
    purpose: AiIntakeResponsePurpose
    follow_up_fact_key: str | None = Field(default=None, max_length=80)
    acknowledges_issue: StrictBool = False
    acknowledges_frustration: StrictBool = False


class AiCustomerResponseCompositionOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    response_text: str = Field(min_length=1, max_length=800)
    purpose: AiIntakeResponsePurpose
    follow_up_fact_key: str | None = Field(default=None, max_length=80)
    acknowledges_issue: bool = False
    acknowledges_frustration: bool = False
    response_source: str = Field(pattern="^(model|playbook|template)$")
    provider: str | None = Field(default=None, max_length=80)
    model: str | None = Field(default=None, max_length=160)
    endpoint: str | None = Field(default=None, max_length=20)
    fallback_used: bool = False
    tokens_in: Annotated[int | None, Field(default=None, ge=0)] = None
    tokens_out: Annotated[int | None, Field(default=None, ge=0)] = None
    duration_ms: Annotated[int, Field(ge=0)] = 0
    safety_reason: str | None = Field(default=None, max_length=120)


class DataCleaningEligibility(BaseModel):
    """Typed, side-effect-free eligibility result for the future flow."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    eligible: bool
    state: DataCleaningState = DataCleaningState.idle
    reason: DataCleaningEligibilityReason
    config_id: UUID | None = None
    support_team_id: UUID | None = None
