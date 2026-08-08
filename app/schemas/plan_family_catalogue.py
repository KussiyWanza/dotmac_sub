"""Typed command and query contracts for plan-family catalogue PDFs."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from app.services.owner_commands import CommandContext


@dataclass(frozen=True, slots=True)
class PublishPlanFamilyCatalogueCommand:
    context: CommandContext
    plan_family: str
    display_name: str
    description: str | None
    original_filename: str
    content_type: str | None
    file_bytes: bytes
    actor_system_user_id: UUID


@dataclass(frozen=True, slots=True)
class PublishPlanFamilyCatalogueOutcome:
    catalogue_id: UUID
    plan_family: str
    display_name: str
    version: int
    stored_file_id: UUID
    replayed: bool


@dataclass(frozen=True, slots=True)
class PlanFamilyCatalogueOption:
    plan_family: str
    label: str
    catalogue_id: UUID | None
    display_name: str | None
    version: int | None
    filename: str | None
    file_size: int | None
    is_shareable: bool


@dataclass(frozen=True, slots=True)
class ResolveShareablePlanFamilyCatalogueQuery:
    plan_family: str


@dataclass(frozen=True, slots=True)
class PublicPlanFamilyCatalogue:
    catalogue_id: UUID
    plan_family: str
    display_name: str
    version: int
    filename: str
    content_type: str
    file_size: int
    stored_file_id: UUID
