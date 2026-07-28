"""Native projects API schemas ported from CRM ``schemas/projects.py``.

Shape-compatible with the CRM API: field names, optionality and enum
vocabularies carry verbatim. The only deltas are sub's model/enum names
(``ProjectTaskStatus``/``ProjectTaskPriority`` instead of CRM's
``TaskStatus``/``TaskPriority``) — the *values* are identical (§1.7).
The sub models store enum values as strings; pydantic coerces both ways.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.project import (
    ProjectPriority,
    ProjectStatus,
    ProjectTaskPriority,
    ProjectTaskStatus,
    ProjectType,
)


class ProjectBase(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    name: str = Field(min_length=1, max_length=160)
    code: str | None = Field(default=None, max_length=80)
    description: str | None = None
    customer_address: str | None = None
    project_type: ProjectType | None = None
    project_template_id: UUID | None = None
    status: ProjectStatus = ProjectStatus.open
    priority: ProjectPriority = ProjectPriority.normal
    subscriber_id: UUID | None = None
    lead_id: UUID | None = None
    created_by_person_id: UUID | None = None
    owner_person_id: UUID | None = None
    manager_person_id: UUID | None = None
    project_manager_person_id: UUID | None = None
    assistant_manager_person_id: UUID | None = None
    service_team_id: UUID | None = None
    start_at: datetime | None = None
    due_at: datetime | None = None
    completed_at: datetime | None = None
    region: str | None = Field(default=None, max_length=80)
    tags: list[str] | None = None
    metadata_: dict | None = Field(
        default=None,
        serialization_alias="metadata",
    )
    is_active: bool = True


class ProjectCreate(ProjectBase):
    pass


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    code: str | None = Field(default=None, max_length=80)
    description: str | None = None
    customer_address: str | None = None
    project_type: ProjectType | None = None
    project_template_id: UUID | None = None
    status: ProjectStatus | None = None
    priority: ProjectPriority | None = None
    subscriber_id: UUID | None = None
    lead_id: UUID | None = None
    created_by_person_id: UUID | None = None
    owner_person_id: UUID | None = None
    manager_person_id: UUID | None = None
    project_manager_person_id: UUID | None = None
    assistant_manager_person_id: UUID | None = None
    service_team_id: UUID | None = None
    start_at: datetime | None = None
    due_at: datetime | None = None
    completed_at: datetime | None = None
    region: str | None = Field(default=None, max_length=80)
    tags: list[str] | None = None
    metadata_: dict | None = Field(
        default=None,
        serialization_alias="metadata",
    )
    is_active: bool | None = None

    @model_validator(mode="after")
    def _validate_dates(self) -> ProjectUpdate:
        if self.start_at and self.due_at and self.start_at >= self.due_at:
            raise ValueError("start_at must be before due_at")
        return self


class ProjectRead(ProjectBase):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: UUID
    number: str | None = None
    created_at: datetime
    updated_at: datetime


class ProjectCostSummary(BaseModel):
    project_id: UUID
    currency: str
    labor_minutes: int
    labor_hourly_rate: Decimal
    labor_cost: Decimal
    expense_total: Decimal
    total_cost: Decimal


class ProjectTemplateBase(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    name: str = Field(min_length=1, max_length=160)
    project_type: ProjectType | None = None
    description: str | None = None
    is_active: bool = True


class ProjectTemplateCreate(ProjectTemplateBase):
    pass


class ProjectTemplateUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    project_type: ProjectType | None = None
    description: str | None = None
    is_active: bool | None = None


class ProjectTemplateRead(ProjectTemplateBase):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class ProjectTemplateTaskBase(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    template_id: UUID
    title: str = Field(min_length=1, max_length=200)
    description: str | None = None
    status: ProjectTaskStatus | None = None
    priority: ProjectTaskPriority | None = None
    sort_order: int = 0
    effort_hours: int | None = None
    is_active: bool = True


class ProjectTemplateTaskCreate(ProjectTemplateTaskBase):
    pass


class ProjectTemplateTaskUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    status: ProjectTaskStatus | None = None
    priority: ProjectTaskPriority | None = None
    sort_order: int | None = None
    effort_hours: int | None = None
    is_active: bool | None = None


class ProjectTemplateTaskRead(ProjectTemplateTaskBase):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class ProjectTaskCommentBase(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    task_id: UUID
    author_person_id: UUID | None = None
    body: str = Field(min_length=1)
    attachments: list[dict] | None = None


class ProjectTaskCommentCreate(ProjectTaskCommentBase):
    pass


class ProjectTaskCommentRead(ProjectTaskCommentBase):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: UUID
    created_at: datetime


class ProjectCommentBase(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    project_id: UUID
    author_person_id: UUID | None = None
    body: str = Field(min_length=1)
    attachments: list[dict] | None = None


class ProjectCommentCreate(ProjectCommentBase):
    pass


class ProjectCommentUpdate(BaseModel):
    body: str | None = Field(default=None, min_length=1)
    attachments: list[dict] | None = None


class ProjectCommentRead(ProjectCommentBase):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: UUID
    created_at: datetime


class ProjectTaskBase(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    project_id: UUID
    parent_task_id: UUID | None = None
    title: str = Field(min_length=1, max_length=200)
    description: str | None = None
    status: ProjectTaskStatus = ProjectTaskStatus.todo
    priority: ProjectTaskPriority = ProjectTaskPriority.normal
    assigned_to_person_id: UUID | None = None
    assigned_to_person_ids: list[UUID] | None = None
    created_by_person_id: UUID | None = None
    ticket_id: UUID | None = None
    start_at: datetime | None = None
    due_at: datetime | None = None
    completed_at: datetime | None = None
    effort_hours: int | None = None
    tags: list[str] | None = None
    metadata_: dict | None = Field(
        default=None,
        serialization_alias="metadata",
    )
    is_active: bool = True


class ProjectTaskCreate(ProjectTaskBase):
    pass


class ProjectTaskUpdate(BaseModel):
    project_id: UUID | None = None
    parent_task_id: UUID | None = None
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    status: ProjectTaskStatus | None = None
    priority: ProjectTaskPriority | None = None
    assigned_to_person_id: UUID | None = None
    assigned_to_person_ids: list[UUID] | None = None
    created_by_person_id: UUID | None = None
    ticket_id: UUID | None = None
    start_at: datetime | None = None
    due_at: datetime | None = None
    completed_at: datetime | None = None
    effort_hours: int | None = None
    tags: list[str] | None = None
    metadata_: dict | None = Field(
        default=None,
        serialization_alias="metadata",
    )
    is_active: bool | None = None


class ProjectTaskRead(ProjectTaskBase):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: UUID
    number: str | None = None
    created_at: datetime
    updated_at: datetime
