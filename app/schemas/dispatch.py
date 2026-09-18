from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator


class SkillBase(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = None
    is_active: bool = True


class SkillCreate(SkillBase):
    pass


class SkillUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = None
    is_active: bool | None = None


class SkillRead(SkillBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class TechnicianProfileBase(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    person_id: UUID | None = None
    system_user_id: UUID | None = None
    crm_person_id: str | None = Field(default=None, max_length=64)
    title: str | None = Field(default=None, max_length=120)
    region: str | None = Field(default=None, max_length=120)
    workforce_system: str | None = Field(default=None, max_length=40)
    workforce_employee_reference: str | None = Field(default=None, max_length=100)
    metadata_: dict | None = Field(default=None, serialization_alias="metadata")
    is_active: bool = True

    @model_validator(mode="after")
    def _require_identity(self) -> TechnicianProfileBase:
        if self.person_id is None and self.system_user_id is None:
            raise ValueError("person_id or system_user_id is required")
        if self.person_id is None:
            self.person_id = self.system_user_id
        return self


class TechnicianProfileCreate(TechnicianProfileBase):
    pass


class TechnicianProfileUpdate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    person_id: UUID | None = None
    system_user_id: UUID | None = None
    crm_person_id: str | None = Field(default=None, max_length=64)
    title: str | None = Field(default=None, max_length=120)
    region: str | None = Field(default=None, max_length=120)
    workforce_system: str | None = Field(default=None, max_length=40)
    workforce_employee_reference: str | None = Field(default=None, max_length=100)
    metadata_: dict | None = Field(default=None, serialization_alias="metadata")
    is_active: bool | None = None


class TechnicianProfileRead(TechnicianProfileBase):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class TechnicianSkillBase(BaseModel):
    technician_id: UUID
    skill_id: UUID
    proficiency: int | None = Field(default=None, ge=0, le=5)
    is_primary: bool = False
    is_active: bool = True


class TechnicianSkillCreate(TechnicianSkillBase):
    pass


class TechnicianSkillUpdate(BaseModel):
    proficiency: int | None = Field(default=None, ge=0, le=5)
    is_primary: bool | None = None
    is_active: bool | None = None


class TechnicianSkillRead(TechnicianSkillBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime


class TimeWindowMixin(BaseModel):
    start_at: datetime
    end_at: datetime

    @model_validator(mode="after")
    def _valid_window(self):
        if self.end_at <= self.start_at:
            raise ValueError("end_at must be after start_at")
        return self


class ShiftCreate(TimeWindowMixin):
    technician_id: UUID
    timezone: str | None = Field(default=None, max_length=64)
    shift_type: str | None = Field(default=None, max_length=60)
    workforce_system: str | None = Field(default=None, max_length=40)
    workforce_record_reference: str | None = Field(default=None, max_length=100)
    is_active: bool = True


class ShiftUpdate(BaseModel):
    start_at: datetime | None = None
    end_at: datetime | None = None
    timezone: str | None = Field(default=None, max_length=64)
    shift_type: str | None = Field(default=None, max_length=60)
    workforce_system: str | None = Field(default=None, max_length=40)
    workforce_record_reference: str | None = Field(default=None, max_length=100)
    is_active: bool | None = None

    @model_validator(mode="after")
    def _valid_window(self) -> ShiftUpdate:
        if (
            self.start_at is not None
            and self.end_at is not None
            and self.end_at <= self.start_at
        ):
            raise ValueError("end_at must be after start_at")
        return self


class ShiftRead(ShiftCreate):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class AvailabilityBlockCreate(TimeWindowMixin):
    technician_id: UUID
    reason: str | None = Field(default=None, max_length=160)
    block_type: str | None = Field(default=None, max_length=60)
    is_available: bool = False
    workforce_system: str | None = Field(default=None, max_length=40)
    workforce_record_reference: str | None = Field(default=None, max_length=100)
    is_active: bool = True


class AvailabilityBlockUpdate(BaseModel):
    start_at: datetime | None = None
    end_at: datetime | None = None
    reason: str | None = Field(default=None, max_length=160)
    block_type: str | None = Field(default=None, max_length=60)
    is_available: bool | None = None
    workforce_system: str | None = Field(default=None, max_length=40)
    workforce_record_reference: str | None = Field(default=None, max_length=100)
    is_active: bool | None = None

    @model_validator(mode="after")
    def _valid_window(self) -> AvailabilityBlockUpdate:
        if (
            self.start_at is not None
            and self.end_at is not None
            and self.end_at <= self.start_at
        ):
            raise ValueError("end_at must be after start_at")
        return self


class AvailabilityBlockRead(AvailabilityBlockCreate):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class DispatchRuleBase(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    priority: int = 0
    work_type: str | None = Field(default=None, max_length=40)
    work_priority: str | None = Field(default=None, max_length=40)
    region: str | None = Field(default=None, max_length=120)
    service_team_id: UUID | None = None
    skill_ids: list[UUID] = Field(default_factory=list)
    auto_assign: bool = False
    is_active: bool = True


class DispatchRuleCreate(DispatchRuleBase):
    pass


class DispatchRuleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    priority: int | None = None
    work_type: str | None = Field(default=None, max_length=40)
    work_priority: str | None = Field(default=None, max_length=40)
    region: str | None = Field(default=None, max_length=120)
    service_team_id: UUID | None = None
    skill_ids: list[UUID] | None = None
    auto_assign: bool | None = None
    is_active: bool | None = None


class DispatchRuleRead(DispatchRuleBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class WorkOrderHeaderBase(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    title: str = Field(min_length=1, max_length=200)
    subscriber_id: UUID | None = None
    project_id: UUID | None = None
    project_task_id: UUID | None = None
    requires_as_built_evidence: bool = True
    description: str | None = None
    status: str = Field(default="draft", max_length=20)
    priority: str | None = Field(default="normal", max_length=20)
    work_type: str | None = Field(default="install", max_length=20)
    crm_ticket_id: str | None = Field(default=None, max_length=64)
    crm_project_id: str | None = Field(default=None, max_length=64)
    assigned_to_crm_person_id: str | None = Field(default=None, max_length=64)
    assigned_to_name: str | None = Field(default=None, max_length=160)
    technician_name: str | None = Field(default=None, max_length=160)
    technician_phone: str | None = Field(default=None, max_length=40)
    address: str | None = Field(default=None, max_length=255)
    scheduled_start: datetime | None = None
    scheduled_end: datetime | None = None
    estimated_arrival_at: datetime | None = None
    estimated_duration_minutes: int | None = Field(default=None, ge=0)
    required_skills: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    access_notes: str | None = Field(default=None, max_length=2000)
    metadata_: dict | None = Field(
        default=None,
        validation_alias=AliasChoices("metadata_", "metadata"),
        serialization_alias="metadata",
    )
    is_active: bool = True

    @model_validator(mode="after")
    def _valid_schedule(self) -> WorkOrderHeaderBase:
        if (
            self.scheduled_start is not None
            and self.scheduled_end is not None
            and self.scheduled_end <= self.scheduled_start
        ):
            raise ValueError("scheduled_end must be after scheduled_start")
        return self


class WorkOrderHeaderCreate(WorkOrderHeaderBase):
    public_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description="Optional stable work-order id; generated as sub-<uuid> when omitted.",
    )

    @model_validator(mode="after")
    def _require_subscriber(self) -> WorkOrderHeaderCreate:
        if self.subscriber_id is None:
            raise ValueError("subscriber_id is required for customer work orders")
        return self


class WorkOrderHeaderUpdate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    title: str | None = Field(default=None, min_length=1, max_length=200)
    subscriber_id: UUID | None = None
    project_id: UUID | None = None
    project_task_id: UUID | None = None
    requires_as_built_evidence: bool | None = None
    description: str | None = None
    status: str | None = Field(default=None, max_length=20)
    priority: str | None = Field(default=None, max_length=20)
    work_type: str | None = Field(default=None, max_length=20)
    crm_ticket_id: str | None = Field(default=None, max_length=64)
    crm_project_id: str | None = Field(default=None, max_length=64)
    assigned_to_crm_person_id: str | None = Field(default=None, max_length=64)
    assigned_to_name: str | None = Field(default=None, max_length=160)
    technician_name: str | None = Field(default=None, max_length=160)
    technician_phone: str | None = Field(default=None, max_length=40)
    address: str | None = Field(default=None, max_length=255)
    scheduled_start: datetime | None = None
    scheduled_end: datetime | None = None
    estimated_arrival_at: datetime | None = None
    estimated_duration_minutes: int | None = Field(default=None, ge=0)
    required_skills: list[str] | None = None
    tags: list[str] | None = None
    access_notes: str | None = Field(default=None, max_length=2000)
    metadata_: dict | None = Field(
        default=None,
        validation_alias=AliasChoices("metadata_", "metadata"),
        serialization_alias="metadata",
    )
    is_active: bool | None = None

    @model_validator(mode="after")
    def _valid_schedule(self) -> WorkOrderHeaderUpdate:
        if (
            self.scheduled_start is not None
            and self.scheduled_end is not None
            and self.scheduled_end <= self.scheduled_start
        ):
            raise ValueError("scheduled_end must be after scheduled_start")
        return self


class WorkOrderHeaderRead(WorkOrderHeaderBase):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: UUID
    public_id: str
    # Native support provenance is read-only at generic dispatch boundaries.
    # Only the ticket handoff owner can establish this relationship.
    origin_ticket_id: UUID | None = None
    # Root-only CRM provenance reference; NULL for native rows.
    crm_work_order_id: str | None = None
    work_order_created_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class WorkOrderAssignmentQueueCreate(BaseModel):
    work_order_mirror_id: UUID | None = None
    crm_work_order_id: str | None = Field(default=None, max_length=64)
    status: str = Field(default="queued", max_length=20)
    reason: str | None = None
    dispatch_rule_id: UUID | None = None
    assigned_technician_id: UUID | None = None

    @model_validator(mode="after")
    def _require_work_order_ref(self) -> WorkOrderAssignmentQueueCreate:
        if self.work_order_mirror_id is None and not self.crm_work_order_id:
            raise ValueError("work_order_mirror_id or crm_work_order_id is required")
        return self


class WorkOrderAssignmentQueueUpdate(BaseModel):
    status: str | None = Field(default=None, max_length=20)
    reason: str | None = None
    dispatch_rule_id: UUID | None = None
    assigned_technician_id: UUID | None = None


class WorkOrderAssignmentQueueRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    work_order_mirror_id: UUID
    crm_work_order_id: str
    status: str
    reason: str | None = None
    dispatch_rule_id: UUID | None = None
    assigned_technician_id: UUID | None = None
    created_at: datetime
    updated_at: datetime


class WorkOrderAssignmentPreviewRequest(BaseModel):
    technician_id: UUID
    scheduled_start: datetime | None = None
    scheduled_end: datetime | None = None
    status: str = Field(default="dispatched", max_length=20)

    @model_validator(mode="after")
    def _valid_schedule(self) -> WorkOrderAssignmentPreviewRequest:
        if (
            self.scheduled_start is not None
            and self.scheduled_end is not None
            and self.scheduled_end <= self.scheduled_start
        ):
            raise ValueError("scheduled_end must be after scheduled_start")
        return self


class WorkOrderAssignmentPreviewState(BaseModel):
    status: str
    technician_id: UUID | None = None
    person_id: UUID | None = None
    technician_name: str | None = None
    scheduled_start: datetime | None = None
    scheduled_end: datetime | None = None


class WorkOrderAssignmentPreviewRead(BaseModel):
    work_order_id: str
    previous: WorkOrderAssignmentPreviewState
    result: WorkOrderAssignmentPreviewState


class FiberFieldVerificationJobPlanRequest(BaseModel):
    expected_worklist_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    staged_feature_ids: list[UUID] = Field(min_length=1, max_length=100)
    subscriber_id: UUID
    title: str = Field(min_length=1, max_length=200)
    description: str | None = None
    priority: Literal["lower", "low", "medium", "normal", "high", "urgent"] = "normal"
    address: str | None = Field(default=None, max_length=255)
    scheduled_start: datetime | None = None
    scheduled_end: datetime | None = None
    assigned_technician_id: UUID | None = None
    assignment_reason: str | None = Field(default=None, max_length=2000)
    idempotency_key: str = Field(min_length=16, max_length=160)

    @model_validator(mode="after")
    def _valid_plan(self) -> FiberFieldVerificationJobPlanRequest:
        if len(set(self.staged_feature_ids)) != len(self.staged_feature_ids):
            raise ValueError("staged_feature_ids contains duplicates")
        if (
            self.scheduled_start is not None
            and self.scheduled_end is not None
            and self.scheduled_end <= self.scheduled_start
        ):
            raise ValueError("scheduled_end must be after scheduled_start")
        return self


class FiberFieldVerificationJobPlanExecuteRequest(FiberFieldVerificationJobPlanRequest):
    expected_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class FiberFieldVerificationJobPlanCommandRead(BaseModel):
    address: str | None = None
    assigned_technician_id: UUID | None = None
    assignment_reason: str | None = None
    description: str | None = None
    idempotency_key: str
    priority: str
    public_id: str
    scheduled_end: datetime | None = None
    scheduled_start: datetime | None = None
    subscriber_id: UUID
    title: str


class FiberFieldVerificationJobPlanFeatureRead(BaseModel):
    asset_type: str
    content_sha256: str
    current_work_orders: list[dict[str, str]]
    display_name: str | None = None
    external_id: str | None = None
    geometry_sha256: str | None = None
    geometry_type: str | None = None
    needs_follow_up: bool
    priority: str
    row_sha256: str
    source_batch_id: UUID
    source_profile: str
    source_system: str
    staged_feature_id: UUID
    superseded_work_orders: list[dict[str, str]]
    verification_state: str


class FiberFieldVerificationJobPlanPreviewRead(BaseModel):
    command: FiberFieldVerificationJobPlanCommandRead
    plan_sha256: str
    schema_version: int
    scope_sha256: str
    selected_feature_count: int
    selected_features: list[FiberFieldVerificationJobPlanFeatureRead]
    worklist_report_sha256: str


class FiberFieldVerificationJobPlanExecuteRead(BaseModel):
    assignment: WorkOrderAssignmentQueueRead | None = None
    plan: FiberFieldVerificationJobPlanPreviewRead
    replayed: bool
    work_order: WorkOrderHeaderRead
