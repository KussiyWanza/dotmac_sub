"""Typed references and bounded queries over native infrastructure inventory."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.infrastructure import InfrastructureType


class InfrastructureReference(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)
    type: InfrastructureType
    id: UUID


class InfrastructureSearch(BaseModel):
    model_config = ConfigDict(frozen=True)
    type: InfrastructureType
    query: str = Field(default="", max_length=120)
    limit: int = Field(default=20, ge=1, le=20)


class InfrastructureOption(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: UUID
    label: str
    context: str | None = None


class InfrastructureOptions(BaseModel):
    model_config = ConfigDict(frozen=True)
    results: tuple[InfrastructureOption, ...]


class ProjectInfrastructureChanged(BaseModel):
    model_config = ConfigDict(frozen=True)
    name: Literal["project.infrastructure_changed"] = "project.infrastructure_changed"
    project_id: UUID
    command_id: UUID
    previous: InfrastructureReference | None
    current: InfrastructureReference | None
