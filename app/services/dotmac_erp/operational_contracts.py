"""Version-2 contract for Sub-owned operational context sent to ERP."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, JsonValue


class _OperationalContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ErpProjectProjection(_OperationalContract):
    source_id: UUID
    name: str = Field(max_length=160)
    code: str | None = Field(default=None, max_length=80)
    project_type: str | None = Field(default=None, max_length=80)
    status: str
    priority: str | None = Field(default=None, max_length=40)
    region: str | None = Field(default=None, max_length=80)
    description: str | None = None
    start_at: datetime | None = None
    due_at: datetime | None = None
    customer_name: str | None = Field(default=None, max_length=200)
    customer_crm_id: UUID | None = None
    metadata: dict[str, JsonValue] | None = None
    service_team_name: str | None = Field(default=None, max_length=200)


class ErpTicketProjection(_OperationalContract):
    source_id: UUID
    subject: str = Field(max_length=255)
    ticket_number: str | None = Field(default=None, max_length=40)
    ticket_type: str | None = Field(default=None, max_length=80)
    status: str
    priority: str | None = Field(default=None, max_length=40)
    description: str | None = None
    customer_crm_id: UUID | None = None
    metadata: dict[str, JsonValue] | None = None
    comments: tuple[dict[str, JsonValue], ...] = ()
    activity_log: tuple[dict[str, JsonValue], ...] = ()


class ErpProjectTaskProjection(_OperationalContract):
    source_id: UUID
    project_source_id: UUID
    parent_task_source_id: UUID | None = None
    ticket_source_id: UUID | None = None
    title: str = Field(max_length=200)
    number: str | None = Field(default=None, max_length=40)
    description: str | None = None
    status: str
    priority: str | None = Field(default=None, max_length=40)
    start_at: datetime | None = None
    due_at: datetime | None = None
    completed_at: datetime | None = None
    effort_hours: int | None = Field(default=None, ge=0)
    metadata: dict[str, JsonValue] | None = None


class ErpWorkOrderProjection(_OperationalContract):
    source_id: UUID
    title: str = Field(max_length=200)
    work_type: str | None = Field(default=None, max_length=80)
    status: str
    priority: str | None = Field(default=None, max_length=40)
    project_crm_id: str | None = None
    ticket_crm_id: str | None = None
    scheduled_start: datetime | None = None
    scheduled_end: datetime | None = None
    metadata: dict[str, JsonValue] | None = None


class ErpOperationalSyncCommand(_OperationalContract):
    projects: tuple[ErpProjectProjection, ...] = Field(default=(), max_length=500)
    tickets: tuple[ErpTicketProjection, ...] = Field(default=(), max_length=500)
    project_tasks: tuple[ErpProjectTaskProjection, ...] = Field(
        default=(), max_length=500
    )
    work_orders: tuple[ErpWorkOrderProjection, ...] = Field(default=(), max_length=500)


OperationalEntityType = Literal[
    "project", "ticket", "ticket_item", "project_task", "work_order"
]


class ErpOperationalSyncError(_OperationalContract):
    entity_type: OperationalEntityType
    source_id: UUID = Field(validation_alias=AliasChoices("crm_id", "source_id"))
    error: str


class ErpOperationalSyncOutcome(_OperationalContract):
    contract_version: Literal[2]
    projects_synced: int = Field(ge=0)
    tickets_synced: int = Field(ge=0)
    project_tasks_synced: int = Field(ge=0)
    work_orders_synced: int = Field(ge=0)
    errors: tuple[ErpOperationalSyncError, ...] = ()


OperationalDomain = Literal["projects", "project_tasks", "tickets", "work_orders"]


class OperationalSyncExecution(_OperationalContract):
    domains: tuple[OperationalDomain, ...] = Field(
        default=("projects", "project_tasks", "tickets", "work_orders"),
        min_length=1,
    )
    batch_size: int = Field(default=100, ge=1, le=500)


class OperationalSyncRunOutcome(_OperationalContract):
    projects: int = Field(ge=0)
    tickets: int = Field(ge=0)
    project_tasks: int = Field(ge=0)
    work_orders: int = Field(ge=0)
    errors: tuple[ErpOperationalSyncError, ...] = ()
    skipped: Literal["capability_disabled"] | None = None
