from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.network import FiberSplicePlanDiffRead, FiberSplicePlanRead
from app.schemas.status_presentation import StatusPresentation


class DeviceTokenRegister(BaseModel):
    platform: str = Field(min_length=1, max_length=20)
    fcm_token: str = Field(min_length=1, max_length=512)
    app_version: str | None = Field(default=None, max_length=40)


class DeviceTokenRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    subscriber_id: UUID | None = None
    system_user_id: UUID | None = None
    platform: str | None = None
    app_version: str | None = None
    is_active: bool
    created_at: datetime
    last_seen_at: datetime


class FieldMeResponse(BaseModel):
    person_id: UUID
    name: str
    email: str | None = None
    technician_title: str | None = None
    region: str | None = None
    open_jobs: int
    completed_today: int


class FieldJobSummary(BaseModel):
    """Technician job-list item from the native work-order view."""

    id: str
    work_order_mirror_id: UUID
    title: str
    description: str | None = None
    status: str
    status_presentation: StatusPresentation
    priority: str | None = None
    work_type: str | None = None
    scheduled_start: datetime | None = None
    scheduled_end: datetime | None = None
    estimated_duration_minutes: int | None = None
    estimated_arrival_at: datetime | None = None
    started_at: datetime | None = None
    paused_at: datetime | None = None
    resumed_at: datetime | None = None
    completed_at: datetime | None = None
    total_active_seconds: int | None = None
    technician_name: str | None = None
    technician_phone: str | None = None
    address: str | None = None
    tags: list[str] = Field(default_factory=list)


class FieldCustomer(BaseModel):
    subscriber_id: UUID
    name: str | None = None
    phone: str | None = None
    email: str | None = None
    address_text: str | None = None
    service_plan: str | None = None
    account_number: str | None = None
    status: str | None = None


class FieldJobLocation(BaseModel):
    latitude: float | None = None
    longitude: float | None = None
    address_text: str | None = None
    source: str


class FieldJobLocationUpdate(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class FieldJobDestination(BaseModel):
    destination_type: str = Field(min_length=1, max_length=40)
    destination_id: str | None = Field(default=None, max_length=120)
    label: str = Field(min_length=1, max_length=255)
    latitude: float | None = None
    longitude: float | None = None
    address_text: str | None = None


class FieldJobDestinationsResponse(BaseModel):
    items: list[FieldJobDestination]
    count: int


class FieldSiteContact(BaseModel):
    name: str | None = None
    phone: str | None = None
    email: str | None = None
    relationship: str | None = None


class FieldVisitHistoryItem(BaseModel):
    work_order_id: str
    title: str
    work_type: str | None = None
    status: str | None = None
    completed_at: datetime | None = None


class FieldOpenTicketItem(BaseModel):
    id: str
    ref: str
    subject: str | None = None
    status: str | None = None


class FieldAttachmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    crm_work_order_id: str
    note_id: UUID | None = None
    kind: str
    file_name: str
    mime_type: str
    size_bytes: int
    latitude: float | None = None
    longitude: float | None = None
    captured_at: datetime | None = None
    signer_name: str | None = None
    uploaded_by_person_id: UUID
    uploaded_by_system_user_id: UUID | None = None
    client_ref: UUID | None = None
    asset_type: str | None = None
    asset_id: UUID | None = None
    created_at: datetime
    download_path: str


class FieldNoteCreate(BaseModel):
    body: str = Field(min_length=1, max_length=10000)
    is_internal: bool = True
    attachment_ids: list[UUID] = Field(default_factory=list, max_length=20)


class FieldNoteRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    body: str
    is_internal: bool
    author_person_id: UUID | None = None
    author_name: str | None = None
    created_at: datetime
    attachments: list[FieldAttachmentRead] = Field(default_factory=list)


class FieldWorkLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    person_id: UUID
    start_at: datetime
    end_at: datetime | None = None
    minutes: int
    notes: str | None = None


class FieldWorkLogEntry(BaseModel):
    start_at: datetime
    end_at: datetime | None = None
    notes: str | None = Field(default=None, max_length=2000)
    client_ref: UUID | None = None


class FieldWorkLogSubmit(BaseModel):
    entries: list[FieldWorkLogEntry] = Field(min_length=1, max_length=50)


class FieldWorkLogResult(BaseModel):
    worklog: FieldWorkLogRead
    duplicate: bool
    backdated: bool


class FieldWorkLogSubmitResponse(BaseModel):
    results: list[FieldWorkLogResult]


class FieldJobEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    crm_work_order_id: str
    event: str
    previous_status: str | None = None
    new_status: str | None = None
    person_id: UUID
    system_user_id: UUID | None = None
    latitude: float | None = None
    longitude: float | None = None
    note: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime
    received_at: datetime
    client_event_id: UUID


class FieldMovementRead(BaseModel):
    id: UUID
    crm_work_order_id: str
    destination_type: str
    destination_id: str | None = None
    destination_label: str | None = None
    destination_latitude: float | None = None
    destination_longitude: float | None = None
    started_at: datetime
    arrived_at: datetime | None = None
    start_latitude: float | None = None
    start_longitude: float | None = None
    arrival_latitude: float | None = None
    arrival_longitude: float | None = None
    status: str
    client_ref: UUID | None = None
    created_at: datetime
    updated_at: datetime


class FieldCompletionRequirements(BaseModel):
    """Server-owned completion policy projected to the field client."""

    evidence_required: bool
    minimum_photo_count: int = Field(ge=0)
    customer_signoff_required: bool
    signature_unavailable_reason_allowed: bool


class FieldTransitionRequest(BaseModel):
    event: Literal[
        "accept",
        "en_route",
        "arrived",
        "start",
        "pause",
        "hold",
        "resume",
        "complete",
        "unable_to_complete",
    ]
    client_event_id: UUID
    occurred_at: datetime | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    note: str | None = Field(default=None, max_length=2000)
    payload: dict[str, Any] = Field(default_factory=dict)


class FieldTransitionResponse(BaseModel):
    job: FieldJobSummary
    event: FieldJobEventRead
    replayed: bool


class FieldEquipmentRecord(BaseModel):
    serial_number: str = Field(min_length=1, max_length=120)
    subscription_id: UUID
    pon_port_id: UUID
    vendor: str | None = Field(default=None, max_length=120)
    model: str | None = Field(default=None, max_length=120)
    notes: str | None = Field(default=None, max_length=2000)


class FieldEquipmentRead(BaseModel):
    id: UUID
    ont_unit_id: UUID
    serial_number: str
    vendor: str | None = None
    model: str | None = None
    subscriber_id: UUID
    crm_work_order_id: str | None = None
    assigned_at: datetime | None = None
    active: bool
    notes: str | None = None


class FieldEquipmentCustodyRead(BaseModel):
    id: UUID
    asset_source: str
    asset_id: UUID
    technician_id: UUID
    system_user_id: UUID | None = None
    status: str
    issued_at: datetime
    returned_at: datetime | None = None
    condition_on_issue: str | None = None
    condition_on_return: str | None = None
    notes: str | None = None
    asset_label: str | None = None
    asset_identifier: str | None = None
    assigned_to: str | None = None


class FieldEquipmentIssueRequest(BaseModel):
    asset_source: Literal[
        "field_inventory",
        "field_asset",
        "ont",
        "cpe",
        "olt",
        "network_device",
        "router",
    ]
    asset_id: UUID
    technician_id: UUID
    condition_on_issue: str | None = Field(default=None, max_length=80)
    notes: str | None = Field(default=None, max_length=2000)


class FieldEquipmentReturnRequest(BaseModel):
    condition_on_return: str | None = Field(default=None, max_length=80)
    status: Literal["returned", "lost", "damaged"] = "returned"
    notes: str | None = Field(default=None, max_length=2000)


class FieldMaterialRead(BaseModel):
    id: UUID
    crm_work_order_id: str
    crm_material_id: str | None = None
    item_id: UUID
    sku: str | None = None
    name: str
    unit: str | None = None
    allocated_quantity: int
    consumed_quantity: int
    remaining_quantity: int
    status: str
    notes: str | None = None
    created_at: datetime
    updated_at: datetime


class FieldMaterialConsumeItem(BaseModel):
    material_id: UUID
    consumed_quantity: int = Field(ge=0)
    leftover_note: str | None = Field(default=None, max_length=1000)


class FieldMaterialConsumeRequest(BaseModel):
    items: list[FieldMaterialConsumeItem] = Field(min_length=1, max_length=50)


class FieldMaterialRequestItemCreate(BaseModel):
    item_id: UUID
    quantity: int = Field(gt=0)
    notes: str | None = Field(default=None, max_length=1000)
    serial_numbers: list[str] = Field(default_factory=list, max_length=100)


class FieldMaterialRequestCreate(BaseModel):
    crm_work_order_id: str = Field(min_length=1, max_length=64)
    priority: Literal["low", "medium", "high", "urgent"] = "medium"
    notes: str | None = Field(default=None, max_length=2000)
    source_warehouse_code: str = Field(min_length=1, max_length=100)
    items: list[FieldMaterialRequestItemCreate] = Field(min_length=1, max_length=50)


class FieldMaterialRequestItemRead(BaseModel):
    id: UUID
    item_id: UUID
    sku: str | None = None
    name: str
    unit: str | None = None
    quantity: int
    notes: str | None = None
    serial_numbers: list[str] = Field(default_factory=list)


class FieldMaterialRequestRead(BaseModel):
    id: UUID
    crm_work_order_id: str
    crm_material_request_id: str | None = None
    requested_by_person_id: UUID
    requested_by_system_user_id: UUID | None = None
    status: str
    priority: str
    notes: str | None = None
    source_warehouse_code: str | None = None
    support_system: str | None = None
    support_reference: str | None = None
    support_status: str | None = None
    submitted_at: datetime | None = None
    approved_at: datetime | None = None
    rejected_at: datetime | None = None
    fulfilled_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    items: list[FieldMaterialRequestItemRead] = Field(default_factory=list)


class FieldManagerMaterialRejectRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class FieldInventoryItemRead(BaseModel):
    id: UUID
    crm_item_id: str | None = None
    sku: str | None = None
    name: str
    unit: str | None = None
    description: str | None = None
    category: str | None = None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class FieldInventoryLocationRead(BaseModel):
    id: UUID
    name: str
    code: str | None = None
    is_active: bool = True


class FieldExpenseRequestItemCreate(BaseModel):
    category_code: str = Field(min_length=1, max_length=30)
    category_name: str | None = Field(default=None, max_length=120)
    description: str = Field(min_length=1, max_length=500)
    amount: Decimal = Field(gt=0)
    expense_date: date | None = None
    vendor_name: str | None = Field(default=None, max_length=200)
    receipt_url: str | None = Field(default=None, max_length=500)
    receipt_attachment_id: UUID | None = None
    notes: str | None = Field(default=None, max_length=2000)


class FieldExpenseRequestCreate(BaseModel):
    crm_work_order_id: str = Field(min_length=1, max_length=64)
    purpose: str = Field(min_length=1, max_length=500)
    expense_date: date | None = None
    currency: str = Field(default="NGN", min_length=3, max_length=3)
    notes: str | None = Field(default=None, max_length=2000)
    client_ref: UUID | None = None
    items: list[FieldExpenseRequestItemCreate] = Field(min_length=1, max_length=50)


class FieldExpenseRequestItemRead(BaseModel):
    id: UUID
    category_code: str
    category_name: str | None = None
    description: str
    amount: Decimal
    expense_date: date | None = None
    vendor_name: str | None = None
    receipt_url: str | None = None
    receipt_attachment_id: UUID | None = None
    notes: str | None = None


class FieldExpenseRequestRead(BaseModel):
    id: UUID
    crm_work_order_id: str
    crm_expense_request_id: str | None = None
    requested_by_person_id: UUID
    requested_by_system_user_id: UUID | None = None
    status: str
    purpose: str
    expense_date: date | None = None
    currency: str
    notes: str | None = None
    rejection_reason: str | None = None
    expense_system: str | None = None
    expense_claim_reference: str | None = None
    expense_claim_number: str | None = None
    expense_claim_status: str | None = None
    client_ref: UUID | None = None
    total_amount: Decimal
    submitted_at: datetime | None = None
    approved_at: datetime | None = None
    rejected_at: datetime | None = None
    paid_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    items: list[FieldExpenseRequestItemRead] = Field(default_factory=list)


class FieldJobHistoryItem(BaseModel):
    id: str
    type: str
    title: str
    description: str | None = None
    occurred_at: datetime | None = None
    actor_name: str | None = None
    status: str | None = None
    is_internal: bool | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class FieldProjectContext(BaseModel):
    id: UUID
    number: str | None = None
    name: str
    status: str
    status_presentation: StatusPresentation


class FieldProjectTaskContext(BaseModel):
    id: UUID
    number: str | None = None
    title: str
    status: str
    status_presentation: StatusPresentation


class FieldTicketContext(BaseModel):
    id: UUID
    number: str | None = None
    title: str
    status: str
    status_presentation: StatusPresentation


class FieldCustomerExperienceContext(BaseModel):
    project: FieldProjectContext | None = None
    project_task: FieldProjectTaskContext | None = None
    origin_ticket: FieldTicketContext | None = None
    project_task_ticket: FieldTicketContext | None = None


class FieldJobDetail(BaseModel):
    job: FieldJobSummary
    completion_requirements: FieldCompletionRequirements
    customer: FieldCustomer | None = None
    location: FieldJobLocation
    customer_experience: FieldCustomerExperienceContext
    access_notes: str | None = None
    additional_contacts: list[FieldSiteContact] = Field(default_factory=list)
    recent_visits: list[FieldVisitHistoryItem] = Field(default_factory=list)
    open_tickets: list[FieldOpenTicketItem] = Field(default_factory=list)
    notes: list[FieldNoteRead] = Field(default_factory=list)
    attachments: list[FieldAttachmentRead] = Field(default_factory=list)
    materials: list[FieldMaterialRead] = Field(default_factory=list)
    material_requests: list[FieldMaterialRequestRead] = Field(default_factory=list)
    expense_requests: list[FieldExpenseRequestRead] = Field(default_factory=list)
    worklogs: list[FieldWorkLogRead] = Field(default_factory=list)
    events: list[FieldJobEventRead] = Field(default_factory=list)
    movements: list[FieldMovementRead] = Field(default_factory=list)
    equipment: FieldEquipmentRead | None = None
    history: list[FieldJobHistoryItem] = Field(default_factory=list)


class FieldScheduleEntry(BaseModel):
    type: str
    start_at: datetime
    end_at: datetime | None = None
    title: str
    reference_id: str


class FieldRouteStop(BaseModel):
    sequence: int
    work_order_id: str
    work_order_mirror_id: UUID
    title: str
    distance_km: float | None = None
    leg_km: float | None = None
    latitude: float | None = None
    longitude: float | None = None
    address_text: str | None = None


class FieldRouteResponse(BaseModel):
    route: list[FieldRouteStop]


class LocationPingInput(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy_m: float | None = Field(default=None, ge=0)
    captured_at: datetime | None = None
    crm_work_order_id: str | None = Field(default=None, max_length=64)
    source: str = Field(default="mobile", max_length=32)
    status: str | None = Field(default=None, max_length=20)


class LocationPingBatch(BaseModel):
    pings: list[LocationPingInput] = Field(min_length=1, max_length=200)


class LocationSharingUpdate(BaseModel):
    enabled: bool
    status: str | None = Field(default=None, max_length=20)


class FieldPresenceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    person_id: UUID
    status: str
    location_sharing_enabled: bool
    last_latitude: float | None = None
    last_longitude: float | None = None
    last_location_accuracy_m: float | None = None
    last_location_at: datetime | None = None
    last_seen_at: datetime | None = None


class LocationIngestResponse(BaseModel):
    accepted: int
    errors: list[dict[str, Any]] = Field(default_factory=list)
    presence: FieldPresenceRead
    transitions: list[dict[str, Any]] = Field(default_factory=list)


class VoiceExtractRequest(BaseModel):
    transcript: str = Field(min_length=1, max_length=4000)
    context: str | None = Field(default=None, max_length=120)
    asr_confidence: float | None = Field(default=None, ge=0, le=1)


class VoiceExtractResponse(BaseModel):
    work_status: str | None = None
    equipment_serial: str | None = None
    signal_readings: dict[str, str] = Field(default_factory=dict)
    materials_used: list[dict[str, str | None]] = Field(default_factory=list)
    notes: str
    confidence: float | None = None
    requires_review: bool
    review_reasons: list[str] = Field(default_factory=list)


class FieldMapAsset(BaseModel):
    id: UUID
    type: str
    title: str
    subtitle: str | None = None
    latitude: float
    longitude: float
    status: str | None = None
    updated_at: datetime | None = None
    distance_m: float | None = None


class FieldMapAssetNearbyResponse(BaseModel):
    items: list[FieldMapAsset]
    count: int
    latitude: float
    longitude: float
    radius_m: float
    server_time: datetime


class FieldMapAssetLocationUpdate(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    expected_updated_at: datetime | None = None
    source: str | None = Field(default=None, max_length=32)
    accuracy_m: float | None = Field(default=None, ge=0)
    client_ref: UUID | None = None
    force: bool = False


class FieldMapSearchResult(BaseModel):
    kind: Literal["job", "asset"]
    id: str
    asset_type: str | None = None
    title: str
    subtitle: str | None = None
    latitude: float
    longitude: float
    status: str | None = None
    status_presentation: StatusPresentation | None = None
    address_text: str | None = None


class FieldMapSearchResponse(BaseModel):
    items: list[FieldMapSearchResult]
    count: int
    limit: int
    offset: int = 0


class FieldSpliceCreate(BaseModel):
    closure_id: UUID
    from_strand_id: UUID
    from_strand_end: Literal["a", "b"]
    to_strand_id: UUID
    to_strand_end: Literal["a", "b"]
    tray_id: UUID | None = None
    position: int | None = Field(default=None, ge=1)
    splice_type: str = Field(min_length=1, max_length=80)
    loss_db: float | None = Field(default=None, ge=0, le=5)
    note: str | None = Field(default=None, max_length=2000)
    work_order_id: str | None = Field(default=None, min_length=1, max_length=64)
    plan_item_id: UUID | None = None


class FieldStrandColorRead(BaseModel):
    strand_number: int
    color_standard: str
    tube_number: int | None = None
    tube_color: str | None = None
    core_number_in_tube: int
    core_color: str


class FieldSpliceProposalResponse(BaseModel):
    change_request_id: UUID
    status: str
    replayed: bool
    closure_id: UUID
    from_strand_id: UUID
    from_strand_end: Literal["a", "b"]
    to_strand_id: UUID
    to_strand_end: Literal["a", "b"]
    work_order_public_id: str | None = None
    from_strand_colors: FieldStrandColorRead | None = None
    to_strand_colors: FieldStrandColorRead | None = None
    plan_id: UUID | None = None
    plan_item_id: UUID | None = None


class FieldSpliceProposalStatusRead(BaseModel):
    change_request_id: UUID
    status: str
    operation: str
    closure_id: UUID | None = None
    from_strand_id: UUID | None = None
    from_strand_end: Literal["a", "b"] | None = None
    to_strand_id: UUID | None = None
    to_strand_end: Literal["a", "b"] | None = None
    splice_type: str | None = None
    loss_db: float | None = None
    work_order_public_id: str | None = None
    from_strand_colors: FieldStrandColorRead | None = None
    to_strand_colors: FieldStrandColorRead | None = None
    plan_id: UUID | None = None
    plan_item_id: UUID | None = None
    review_notes: str | None = None
    reviewed_at: datetime | None = None
    applied_at: datetime | None = None
    created_at: datetime


class FieldFiberTraceHop(BaseModel):
    kind: str
    label: str
    asset_id: UUID | None = None
    evidence: str
    validation: str
    operational_state: str | None = None
    splitter_stage: int | None = None
    insertion_loss_db: str | None = None
    cumulative_splitter_loss_db: str | None = None


class FieldFiberTraceGap(BaseModel):
    code: str
    message: str
    after_kind: str | None = None
    after_asset_id: UUID | None = None


class FieldFiberOntLiveRead(BaseModel):
    ont_unit_id: UUID
    serial_number: str
    olt_status: str | None = None
    olt_status_seen_at: datetime | None = None
    rx_signal_dbm: float | None = None
    rx_observed_at: datetime | None = None


class FieldOntAttachmentCreate(BaseModel):
    work_order_id: str = Field(min_length=1, max_length=64)
    ont_unit_id: UUID
    splitter_port_id: UUID
    note: str | None = Field(default=None, max_length=2000)


class FieldOntAttachmentResponse(BaseModel):
    decision_id: UUID
    status: str
    ont_unit_id: UUID
    splitter_port_id: UUID
    work_order_public_id: str


class FieldCableRegistrationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    fiber_count: int = Field(ge=1, le=1728)
    segment_type: Literal["feeder", "distribution", "drop"] | None = None
    cable_type: str | None = Field(default=None, max_length=40)
    fibers_per_tube: int | None = Field(default=None, ge=1, le=48)
    color_standard: str | None = Field(default=None, max_length=40)
    length_m: float | None = Field(default=None, ge=0)
    notes: str | None = Field(default=None, max_length=2000)
    work_order_id: str | None = Field(default=None, min_length=1, max_length=64)


class FieldCableRegistrationResponse(BaseModel):
    change_request_id: UUID
    status: str
    name: str
    fiber_count: int
    work_order_public_id: str | None = None


class FieldStrandDamageCreate(BaseModel):
    note: str = Field(min_length=1, max_length=2000)
    strand_id: UUID | None = None
    segment_id: UUID | None = None
    tube_number: int | None = Field(default=None, ge=1)
    work_order_id: str | None = Field(default=None, min_length=1, max_length=64)


class FieldStrandDamageResponse(BaseModel):
    change_request_ids: list[UUID] = Field(default_factory=list)
    strand_ids: list[UUID] = Field(default_factory=list)
    tube_number: int | None = None
    work_order_public_id: str | None = None


class FieldSpliceProposalCountsRead(BaseModel):
    pending: int
    applied: int
    rejected: int


class FieldJobPlanSummaryRead(BaseModel):
    plan_id: UUID
    status: str
    item_count: int
    executed_count: int
    unexecuted_count: int


class FieldJobEvidenceRead(BaseModel):
    work_order_id: UUID
    work_order_public_id: str
    fiber_test_count: int
    derived_failed_count: int
    assertion_conflict_count: int
    source_observation_count: int
    splice_proposals: FieldSpliceProposalCountsRead
    unplanned_splice_count: int
    plan: FieldJobPlanSummaryRead | None = None
    attachment_count: int
    pending_inventory_proposals: int
    as_built_required: bool
    as_built_satisfied: bool


class FieldSplicePlanResponse(BaseModel):
    """The job's live cut sheet and planned-vs-as-built diff (None when absent)."""

    work_order_id: str
    plan: FiberSplicePlanRead | None = None
    diff: FiberSplicePlanDiffRead | None = None


class FieldStrandTerminationDetailRead(BaseModel):
    strand_number: int
    strand_end: str
    port_label: str | None = None
    port_number: int | None = None
    panel_name: str | None = None
    rack_code: str | None = None
    rack_name: str | None = None
    colors: FieldStrandColorRead | None = None


class FieldSegmentPhysicalDetailRead(BaseModel):
    segment_id: UUID
    termination_count: int
    truncated: bool
    terminations: list[FieldStrandTerminationDetailRead] = Field(default_factory=list)


class FieldLinkBudgetComponentRead(BaseModel):
    name: str
    loss_db: float
    basis: str


class FieldLinkBudgetRead(BaseModel):
    expected_loss_db: float
    complete: bool
    components: list[FieldLinkBudgetComponentRead] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    measured_rx_dbm: float | None = None
    assumed_tx_dbm: float | None = None
    margin_db: float | None = None
    policy_version: int


class FieldFiberCustomerTraceRead(BaseModel):
    subscription_id: UUID
    customer_label: str
    subscription_status: str
    hops: list[FieldFiberTraceHop]
    gaps: list[FieldFiberTraceGap]
    electronic_complete: bool
    physical_complete: bool
    customer_trace_complete: bool
    first_gap: FieldFiberTraceGap | None = None
    last_validated_scope: FieldFiberTraceHop | None = None
    upstream_scope: str
    upstream_message: str
    ont_live: FieldFiberOntLiveRead | None = None
    physical_details: list[FieldSegmentPhysicalDetailRead] = Field(default_factory=list)
    link_budget: FieldLinkBudgetRead | None = None


class FieldFiberTestCreate(BaseModel):
    crm_work_order_id: str = Field(min_length=1, max_length=64)
    asset_type: str = Field(min_length=1, max_length=80)
    asset_id: UUID
    test_type: str = Field(min_length=1, max_length=40)
    wavelength_nm: int | None = Field(default=None, ge=0)
    value_db: float | None = None
    unit: str | None = Field(default=None, max_length=16)
    passed: bool | None = None
    instrument: str | None = Field(default=None, max_length=120)
    measured_at: datetime | None = None
    notes: str | None = Field(default=None, max_length=2000)
    attachment_id: UUID | None = None
    client_ref: UUID | None = None


class FieldFiberTestRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    work_order_mirror_id: UUID
    crm_work_order_id: str
    asset_type: str
    asset_id: UUID
    test_type: str
    wavelength_nm: int | None = None
    value_db: float | None = None
    unit: str | None = None
    passed: bool | None = None
    instrument: str | None = None
    attachment_id: UUID | None = None
    measured_by_person_id: UUID
    measured_by_system_user_id: UUID | None = None
    measured_at: datetime | None = None
    notes: str | None = None
    client_ref: UUID | None = None
    created_at: datetime
    derived_passed: bool | None = None
    derived_verdict: str | None = None
    applied_minimum_db: float | None = None
    applied_maximum_db: float | None = None
    acceptance_policy_version: int | None = None
    assertion_conflict: bool = False


class FieldFiberSourceObservationCreate(BaseModel):
    work_order_id: str = Field(min_length=1, max_length=64)
    staged_feature_id: UUID
    expected_feature_content_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    verification_scope: Literal[
        "identity",
        "presence",
        "start_endpoint",
        "end_endpoint",
        "path_endpoints",
    ]
    outcome: Literal[
        "agrees",
        "conflicts",
        "not_found",
        "inaccessible",
        "inconclusive",
    ]
    observed_at: datetime
    client_ref: UUID
    observed_external_label: str | None = Field(default=None, max_length=255)
    observed_asset_type: str | None = Field(default=None, max_length=40)
    observed_asset_id: UUID | None = None
    start_endpoint_type: str | None = Field(default=None, max_length=40)
    start_endpoint_ref_id: UUID | None = None
    end_endpoint_type: str | None = Field(default=None, max_length=40)
    end_endpoint_ref_id: UUID | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    accuracy_m: float | None = Field(default=None, ge=0, le=10000)
    instrument: str | None = Field(default=None, max_length=120)
    measurement_payload: dict[str, Any] = Field(default_factory=dict)
    attachment_ids: list[UUID] = Field(default_factory=list, max_length=20)
    notes: str | None = Field(default=None, max_length=4000)


class FieldFiberSourceObservationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    staged_feature_id: UUID
    feature_content_sha256: str
    source_system: str
    source_profile: str
    source_asset_type: str
    source_external_id: str | None = None
    work_order_id: UUID
    work_order_public_id: str
    verification_scope: str
    outcome: str
    observed_external_label: str | None = None
    observed_asset_type: str | None = None
    observed_asset_id: UUID | None = None
    start_endpoint_type: str | None = None
    start_endpoint_ref_id: UUID | None = None
    end_endpoint_type: str | None = None
    end_endpoint_ref_id: UUID | None = None
    latitude: float | None = None
    longitude: float | None = None
    accuracy_m: float | None = None
    instrument: str | None = None
    measurement_payload: dict[str, Any] = Field(default_factory=dict)
    attachment_ids: list[UUID] = Field(default_factory=list)
    notes: str | None = None
    claim_sha256: str
    observation_sha256: str
    client_ref: UUID
    recorded_by_technician_id: UUID
    recorded_by_person_id: UUID
    recorded_by_system_user_id: UUID | None = None
    observed_at: datetime
    created_at: datetime


class FieldFiberWorkOrderEvidenceMapRead(BaseModel):
    report_sha256: str
    source_overlay_sha256: str
    worklist_report_sha256: str
    observation_evidence_sha256: str
    work_order_id: UUID
    work_order_public_id: str
    observation_count: int
    current_source_observation_count: int
    superseded_source_observation_count: int
    feature_count: int
    evidence_context_counts: dict[str, int]
    geometry_presentation_counts: dict[str, int]
    feature_collection: dict[str, Any]
    schema_version: int = 1


class FieldJobChatMessageCreate(BaseModel):
    body: str = Field(min_length=1, max_length=4000)


class FieldJobChatMessageRead(BaseModel):
    id: UUID
    body: str
    direction: str
    author_name: str | None = None
    created_at: datetime
    read_at: datetime | None = None


class FieldJobChatThread(BaseModel):
    available: bool
    can_send: bool
    conversation_id: str | None = None
    customer_name: str | None = None
    messages: list[FieldJobChatMessageRead] = Field(default_factory=list)


class FieldManagerMeResponse(BaseModel):
    person_id: str
    name: str
    roles: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    is_manager: bool = True


class FieldManagerSummary(BaseModel):
    technicians_total: int
    technicians_live: int
    technicians_sharing: int
    open_jobs: int
    unassigned_jobs: int
    pending_expenses: int


class FieldManagerActiveWorkOrder(BaseModel):
    id: str
    title: str
    status: str
    status_presentation: StatusPresentation


class FieldManagerTechnician(BaseModel):
    technician_id: UUID
    person_id: UUID
    person_label: str
    title: str | None = None
    region: str | None = None
    status: str
    location_sharing_enabled: bool
    is_live: bool
    last_latitude: float | None = None
    last_longitude: float | None = None
    accuracy_m: float | None = None
    last_location_at: datetime | None = None
    last_seen_at: datetime | None = None
    active_work_order: FieldManagerActiveWorkOrder | None = None


class FieldManagerTechniciansResponse(BaseModel):
    items: list[FieldManagerTechnician] = Field(default_factory=list)
    count: int
    live_count: int
    sharing_count: int
    limit: int
    offset: int


class TechnicianSatisfactionScorecard(BaseModel):
    """One technician's customer satisfaction over the reporting window.

    ``average`` is null when nobody has rated them yet, which is deliberately
    distinct from a low score.
    """

    technician_id: str
    technician_name: str | None = None
    rated_visits: int
    average: float | None = None
    distribution: dict[int, int] = Field(default_factory=dict)
    latest_rated_at: datetime | None = None


class TechnicianSatisfactionResponse(BaseModel):
    items: list[TechnicianSatisfactionScorecard] = Field(default_factory=list)
    count: int
    window_days: int | None = None


class FieldManagerJob(BaseModel):
    id: str
    work_order_mirror_id: UUID
    title: str
    description: str | None = None
    status: str
    status_presentation: StatusPresentation
    priority: str | None = None
    work_type: str | None = None
    scheduled_start: datetime | None = None
    scheduled_end: datetime | None = None
    assigned_to_person_id: UUID | None = None
    assigned_to_label: str | None = None
    subscriber_label: str | None = None
    address_text: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    location_source: str | None = None


class FieldManagerJobAssignRequest(BaseModel):
    person_id: str = Field(min_length=1, max_length=64)
    scheduled_start: datetime | None = None
    scheduled_end: datetime | None = None
    status: str | None = Field(default=None, max_length=20)


class FieldManagerExpenseRejectRequest(BaseModel):
    reason: str = Field(min_length=2, max_length=500)
