"""Schemas for the customer Portal API broker (RFC #73)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.status_presentation import StatusPresentation


class PortalSessionResponse(BaseModel):
    """A brokered, short-lived Portal API token the client uses to call the CRM
    Portal API directly (e.g. Refer & Earn)."""

    portal_token: str
    expires_at: int = Field(..., description="Unix epoch seconds")
    api_base: str = Field(..., description="Absolute base URL for the CRM Portal API")


# ── Refer & Earn (served from the local mirror) ──────────────────────────────


class ReferralProgram(BaseModel):
    enabled: bool
    reward_amount: str = Field("0", description="Advertised reward, decimal string")
    reward_currency: str = "NGN"


class ReferralTotals(BaseModel):
    total: int = 0
    pending: int = 0
    qualified: int = 0
    rewarded: int = 0
    total_earned: str = "0"


class ReferralItem(BaseModel):
    id: str
    status: str
    referred_name: str | None = None
    reward_amount: str | None = None
    reward_currency: str = "NGN"
    reward_status: str = "none"
    created_at: str | None = None
    qualified_at: str | None = None


class MyReferralsResponse(BaseModel):
    """The signed-in subscriber's referral code, program terms, and history,
    served from the local mirror (RFC #73)."""

    code: str
    share_url: str
    program: ReferralProgram
    totals: ReferralTotals
    referrals: list[ReferralItem] = Field(default_factory=list)


class ReferAFriendRequest(BaseModel):
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    note: str | None = None


class ReferAFriendResponse(BaseModel):
    id: str
    status: str
    message: str = "Referral submitted"


# ── Native customer-experience lifecycle ────────────────────────────────────


class CustomerExperienceState(StrEnum):
    planned = "planned"
    in_progress = "in_progress"
    field_work = "field_work"
    waiting_on_customer = "waiting_on_customer"
    on_hold = "on_hold"
    resolved = "resolved"
    canceled = "canceled"


class ProjectStageState(StrEnum):
    pending = "pending"
    in_progress = "in_progress"
    blocked = "blocked"
    done = "done"
    canceled = "canceled"


class CustomerProvisioningDecisionState(StrEnum):
    not_evaluated = "not_evaluated"
    blocked = "blocked"
    activation_requested = "activation_requested"
    activated = "activated"
    failed = "failed"


class CustomerProvisioningCheckResult(StrEnum):
    passed = "passed"
    failed = "failed"
    not_applicable = "not_applicable"


class CustomerActionKey(StrEnum):
    view_project = "view_project"
    view_work_order = "view_work_order"
    track_technician = "track_technician"
    chat_technician = "chat_technician"
    rate_technician = "rate_technician"
    view_ticket = "view_ticket"
    confirm_resolution = "confirm_resolution"
    dispute_resolution = "dispute_resolution"
    rate_support = "rate_support"
    contact_support = "contact_support"


class CustomerSelfCareAction(BaseModel):
    key: CustomerActionKey
    label: str
    allowed: bool = True
    reason: str | None = None
    method: Literal["GET", "POST"] = "GET"
    api_path: str | None = None


class CustomerTicketReference(BaseModel):
    id: UUID
    number: str | None = None
    title: str
    status: str
    status_presentation: StatusPresentation
    resolved_at: datetime | None = None
    closed_at: datetime | None = None
    actions: list[CustomerSelfCareAction] = Field(default_factory=list)


class CustomerWorkOrderReference(BaseModel):
    id: UUID
    public_id: str
    project_id: UUID | None = None
    project_task_id: UUID | None = None
    origin_ticket_id: UUID | None = None
    title: str
    status: str
    status_presentation: StatusPresentation
    scheduled_start: datetime | None = None
    scheduled_end: datetime | None = None
    estimated_arrival_at: datetime | None = None
    completed_at: datetime | None = None
    technician_name: str | None = None
    technician_phone: str | None = None
    technician_rating: int | None = None
    actions: list[CustomerSelfCareAction] = Field(default_factory=list)


class ProjectStage(BaseModel):
    task_id: UUID | None = None
    key: str | None = None
    title: str
    status: ProjectStageState = ProjectStageState.pending
    status_presentation: StatusPresentation
    completed_at: datetime | None = None
    ticket: CustomerTicketReference | None = None
    work_orders: list[CustomerWorkOrderReference] = Field(default_factory=list)


class CustomerProvisioningCheck(BaseModel):
    kind: str
    result: CustomerProvisioningCheckResult
    reason_code: str


class CustomerProvisioningReference(BaseModel):
    service_order_id: UUID
    subscription_id: UUID | None = None
    activation_task_id: UUID | None = None
    order_status: str
    decision: CustomerProvisioningDecisionState
    reason_code: str | None = None
    checks: list[CustomerProvisioningCheck] = Field(default_factory=list)
    decided_at: datetime | None = None


class ProjectItem(BaseModel):
    id: UUID
    name: str
    status: str
    status_presentation: StatusPresentation
    experience_state: CustomerExperienceState
    project_type: str | None = None
    progress_pct: int = 0
    current_stage: str | None = None
    stages: list[ProjectStage] = Field(default_factory=list)
    work_orders: list[CustomerWorkOrderReference] = Field(default_factory=list)
    related_tickets: list[CustomerTicketReference] = Field(default_factory=list)
    provisioning: list[CustomerProvisioningReference] = Field(default_factory=list)
    actions: list[CustomerSelfCareAction] = Field(default_factory=list)
    customer_address: str | None = None
    region: str | None = None
    start_at: datetime | None = None
    due_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime | None = None


class MyProjectsResponse(BaseModel):
    """Sub-owned project/task/field/support lifecycle for one subscriber."""

    projects: list[ProjectItem] = Field(default_factory=list)
    total: int = 0
    active: int = 0


# ── Field Service / Work Orders ─────────────────────────────────────────────


class WorkOrderItem(CustomerWorkOrderReference):
    work_type: str | None = None
    priority: str | None = None
    address: str | None = None
    estimated_duration_minutes: int | None = None
    started_at: datetime | None = None
    paused_at: datetime | None = None
    resumed_at: datetime | None = None
    total_active_seconds: int | None = None
    created_at: datetime | None = None
    project_name: str | None = None
    project_task_title: str | None = None
    origin_ticket: CustomerTicketReference | None = None


class MyWorkOrdersResponse(BaseModel):
    """The signed-in subscriber's Sub-owned field-service work orders."""

    work_orders: list[WorkOrderItem] = Field(default_factory=list)
    total: int = 0
    upcoming: int = 0


# ── Self-serve quotes (served from the local mirror) ─────────────────────────


class QuoteRequestCreate(BaseModel):
    """Request a map-pinned installation quote (the pin drives feasibility +
    estimate + deposit, computed by the CRM)."""

    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    address: str | None = None
    region: str | None = None
    note: str | None = None


class QuoteFeasibility(BaseModel):
    coverage: str | None = None  # covered | survey_required | out_of_area
    feasible: bool | None = None
    distance_meters: float | None = None
    nearest_fap_name: str | None = None


class QuoteLineItem(BaseModel):
    description: str
    quantity: str | None = None
    unit_price: str | None = None
    amount: str | None = None


class QuoteItem(BaseModel):
    id: str
    status: str
    currency: str = "NGN"
    subtotal: str | None = None
    tax_total: str | None = None
    total: str | None = None
    project_type: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    address: str | None = None
    region: str | None = None
    feasibility: QuoteFeasibility = Field(default_factory=QuoteFeasibility)
    estimate_provisional: bool = False
    deposit_percent: int | None = None
    deposit_amount: str | None = None
    deposit_paid: bool = False
    deposit_reference: str | None = None
    line_items: list[QuoteLineItem] = Field(default_factory=list)
    sales_order_id: str | None = None
    project_id: str | None = None
    created_at: str | None = None
    expires_at: str | None = None


class MyQuotesResponse(BaseModel):
    """The signed-in subscriber's self-serve installation quotes."""

    quotes: list[QuoteItem] = Field(default_factory=list)
    total: int = 0
    open: int = 0


class QuoteDepositInitiateRequest(BaseModel):
    """Start paying a quote's deposit via the existing billing/pay flow."""

    provider: str | None = None
    redirect_url: str | None = None


class QuoteDepositInitiateResponse(BaseModel):
    invoice_id: str
    quote_id: str
    amount: str
    currency: str = "NGN"
    provider_type: str | None = None
    provider_public_key: str | None = None
    payment_reference: str | None = None
    checkout_url: str | None = None
    customer_email: str | None = None
    charged: bool = False


class QuoteDepositVerifyRequest(BaseModel):
    reference: str
    provider: str | None = None


class QuoteDepositVerifyResponse(BaseModel):
    paid: bool
    reference: str
    quote: QuoteItem | None = None


# ── Technician live map + rating ─────────────────────────────────────────────


class CustomerFieldJobChatMessage(BaseModel):
    """One message in the customer's chat with their technician."""

    id: str
    body: str
    from_customer: bool
    author_name: str | None = None
    created_at: datetime


class CustomerFieldJobChatMessageCreate(BaseModel):
    body: str = Field(min_length=1, max_length=2000)


class CustomerFieldJobChatThread(BaseModel):
    """The job chat, which exists only once the technician has departed.

    ``available`` is False with a ``reason`` before departure or when the visit
    is not the customer's; ``can_send`` closes with the visit while the history
    stays readable.
    """

    available: bool = False
    reason: str | None = None
    can_send: bool = False
    work_order_id: str | None = None
    conversation_id: str | None = None
    technician_name: str | None = None
    messages: list[CustomerFieldJobChatMessage] = Field(default_factory=list)


class TechnicianLocation(BaseModel):
    """Live technician position for an in-progress work order.

    ``available`` is False (with a ``reason``) when the map should be hidden:
    outside the Start work to End work window, sharing off, or no fix yet.
    """

    available: bool = False
    reason: str | None = None
    work_order_id: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    accuracy_m: float | None = None
    updated_at: datetime | None = None
    estimated_arrival_at: datetime | None = None


class TechnicianRatingRequest(BaseModel):
    """Rate the technician after a completed work order."""

    rating: int = Field(..., ge=1, le=5, description="1-5 star rating")
    comment: str | None = Field(default=None, max_length=2000)


class TechnicianRatingResponse(BaseModel):
    ok: bool = True
    already_rated: bool = False
    rating: int | None = None
    work_order_id: str | None = None
