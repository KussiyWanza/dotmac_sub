from __future__ import annotations

from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
)

from app.models.support import (
    TicketChannel,
    TicketCommentAuthorType,
    TicketPriority,
    TicketStatus,
    canonical_ticket_status_value,
)
from app.schemas.portal import CustomerSelfCareAction
from app.schemas.status_presentation import StatusPresentation


class AttachmentMeta(BaseModel):
    """Typed private-storage reference persisted with a Ticket attachment."""

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    file_name: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=1, max_length=255)
    file_size: int = Field(ge=0)
    storage_key: str = Field(min_length=1, max_length=1024)
    # Optional only for legacy/imported metadata. Every new local upload supplies
    # this UUID so authorized attachment routes can resolve the StoredFile row.
    stored_file_id: UUID | None = None


class TicketBase(BaseModel):
    # Strip surrounding whitespace so a whitespace-only title fails the
    # ``min_length=1`` check instead of creating a blank-titled ticket.
    model_config = ConfigDict(str_strip_whitespace=True, populate_by_name=True)

    subscriber_id: UUID | None = None
    customer_account_id: UUID | None = None
    lead_id: UUID | None = None
    customer_person_id: UUID | None = None
    created_by_person_id: UUID | None = None
    assigned_to_person_id: UUID | None = None
    technician_person_id: UUID | None = None
    ticket_manager_person_id: UUID | None = None
    site_coordinator_person_id: UUID | None = None
    service_team_id: UUID | None = None

    title: str = Field(min_length=1, max_length=255)
    description: str | None = None
    description_is_internal: bool = True
    region: str | None = Field(default=None, max_length=80)
    status: TicketStatus | None = None
    priority: str = TicketPriority.normal.value
    ticket_type: str | None = Field(default=None, max_length=80)
    channel: TicketChannel = TicketChannel.web
    tags: list[str] = Field(default_factory=list)
    metadata_: dict | None = Field(
        default=None, validation_alias="metadata", serialization_alias="metadata"
    )
    inbound_sender: str | None = None
    inbound_sender_type: str | None = None

    due_at: datetime | None = None
    resolved_at: datetime | None = None
    closed_at: datetime | None = None

    assignee_person_ids: list[UUID] = Field(default_factory=list)
    related_outage_ticket_id: UUID | None = None


class TicketCreate(TicketBase):
    # Retained on TicketBase for historical response compatibility only.
    site_coordinator_person_id: None = None

    @field_validator("status", mode="before")
    @classmethod
    def _normalize_status(cls, value):
        if value is None:
            return value
        return canonical_ticket_status_value(value)

    @field_validator("priority", "ticket_type", mode="before")
    @classmethod
    def _normalize_text_fields(cls, value):
        if value is None:
            return value
        if hasattr(value, "value"):
            value = value.value
        text = str(value).strip()
        return text or None


class TicketUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    subscriber_id: UUID | None = None
    customer_account_id: UUID | None = None
    lead_id: UUID | None = None
    customer_person_id: UUID | None = None
    created_by_person_id: UUID | None = None
    assigned_to_person_id: UUID | None = None
    technician_person_id: UUID | None = None
    ticket_manager_person_id: UUID | None = None
    site_coordinator_person_id: UUID | None = None
    service_team_id: UUID | None = None

    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    description_is_internal: bool | None = None
    region: str | None = Field(default=None, max_length=80)
    status: TicketStatus | None = None
    priority: str | None = None
    ticket_type: str | None = Field(default=None, max_length=80)
    # NCC complaints-return correction: setting either marks it agent-owned,
    # and it is never re-derived from the ticket text afterwards.
    ncc_category: str | None = Field(default=None, max_length=80)
    ncc_subcategory: str | None = Field(default=None, max_length=120)
    channel: TicketChannel | None = None
    tags: list[str] | None = None
    metadata_: dict | None = Field(
        default=None, validation_alias="metadata", serialization_alias="metadata"
    )
    inbound_sender: str | None = None
    inbound_sender_type: str | None = None

    due_at: datetime | None = None
    resolved_at: datetime | None = None
    closed_at: datetime | None = None

    assignee_person_ids: list[UUID] | None = None

    @field_validator("status", mode="before")
    @classmethod
    def _normalize_status(cls, value):
        if value is None:
            return value
        return canonical_ticket_status_value(value)

    @field_validator("priority", "ticket_type", mode="before")
    @classmethod
    def _normalize_update_text_fields(cls, value):
        if value is None:
            return value
        if hasattr(value, "value"):
            value = value.value
        text = str(value).strip()
        return text or None


class TicketRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    number: str | None

    subscriber_id: UUID | None
    customer_account_id: UUID | None
    lead_id: UUID | None
    customer_person_id: UUID | None
    created_by_person_id: UUID | None
    assigned_to_person_id: UUID | None
    technician_person_id: UUID | None
    ticket_manager_person_id: UUID | None
    site_coordinator_person_id: UUID | None
    service_team_id: UUID | None

    title: str
    description: str | None
    description_is_internal: bool
    region: str | None
    status: str
    priority: str
    ticket_type: str | None
    ncc_category: str | None = None
    ncc_category_source: str | None = None
    ncc_subcategory: str | None = None
    ncc_subcategory_source: str | None = None
    channel: TicketChannel
    tags: list[str] | None = None
    metadata_: dict | None = Field(
        default=None,
        validation_alias=AliasChoices("metadata_", "metadata"),
        serialization_alias="metadata",
    )
    attachments: list[dict] | None = None

    due_at: datetime | None
    resolved_at: datetime | None
    closed_at: datetime | None

    merged_into_ticket_id: UUID | None
    is_active: bool
    created_at: datetime
    updated_at: datetime

    # Support-satisfaction rating (1-5) if the customer has rated this closed
    # ticket. Read from the ORM's `Ticket.csat_rating` property (backed by
    # metadata.csat) so the apps can show the score / hide the rate prompt.
    csat_rating: int | None = None

    @field_validator("status", mode="before")
    @classmethod
    def _canonicalize_legacy_status(cls, value):
        return canonical_ticket_status_value(value)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def status_presentation(self) -> StatusPresentation:
        """Canonical label/tone/icon projection for ticket rendering."""
        from app.services.status_presentation import ticket_status_presentation

        return ticket_status_presentation(self.status)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def resolution_actions(self) -> list[CustomerSelfCareAction]:
        """Customer actions are projected by the lifecycle read owner."""
        from app.services.customer_experience_lifecycle import ticket_actions

        return ticket_actions(self)


class TicketSatisfactionRequest(BaseModel):
    """Customer CSAT on a closed support ticket."""

    rating: int = Field(ge=1, le=5)
    comment: str | None = Field(default=None, max_length=2000)


class TicketResolutionDisputeRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=2000)


class TicketBulkUpdateItem(BaseModel):
    ticket_id: UUID
    status: TicketStatus | None = None
    priority: str | None = None
    assigned_to_person_id: UUID | None = None

    @field_validator("status", mode="before")
    @classmethod
    def _normalize_status(cls, value):
        if value is None:
            return value
        return canonical_ticket_status_value(value)

    @field_validator("priority", mode="before")
    @classmethod
    def _normalize_bulk_text_fields(cls, value):
        if value is None:
            return value
        if hasattr(value, "value"):
            value = value.value
        text = str(value).strip()
        return text or None


class TicketBulkUpdateRequest(BaseModel):
    items: list[TicketBulkUpdateItem]


class TicketWorkOrderIssueRequest(BaseModel):
    """Explicit field-action scope issued from an assigned support ticket."""

    model_config = ConfigDict(str_strip_whitespace=True)

    reason: str = Field(min_length=1, max_length=2000)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    project_id: UUID | None = None
    project_task_id: UUID | None = None
    priority: str | None = Field(default=None, max_length=20)
    work_type: str = Field(default="repair", min_length=1, max_length=20)
    address: str | None = Field(default=None, max_length=255)
    scheduled_start: datetime | None = None
    scheduled_end: datetime | None = None
    estimated_duration_minutes: int | None = Field(default=None, ge=0)
    required_skills: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    access_notes: str | None = Field(default=None, max_length=2000)
    requires_as_built_evidence: bool = True


class TicketCommentBase(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    body: str = Field(min_length=1)
    is_internal: bool = True
    attachments: list[AttachmentMeta] = Field(default_factory=list)


class TicketMentionTargetKind(str, Enum):
    person = "person"
    group = "group"


class TicketMentionTarget(BaseModel):
    """Exact authoritative target selected by a comment author."""

    model_config = ConfigDict(frozen=True)

    kind: TicketMentionTargetKind
    target_id: UUID

    @property
    def token(self) -> str:
        return f"{self.kind.value}:{self.target_id}"


class TicketCommentMentionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    kind: TicketMentionTargetKind
    target_id: UUID
    created_at: datetime


class TicketCommentCreate(TicketCommentBase):
    author_person_id: UUID | None = None
    author_type: TicketCommentAuthorType | str | None = None
    author_system_user_id: UUID | None = None
    mentions: tuple[TicketMentionTarget, ...] = ()


class TicketCommentUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    body: str | None = Field(default=None, min_length=1)
    is_internal: bool | None = None
    attachments: list[AttachmentMeta] | None = None
    # None preserves the current set; an explicit empty tuple clears it.
    mentions: tuple[TicketMentionTarget, ...] | None = None


class TicketCommentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    ticket_id: UUID
    author_person_id: UUID | None
    author_type: str
    author_system_user_id: UUID | None
    body: str
    is_internal: bool
    attachments: list[dict] | None = None
    mentions: tuple[TicketCommentMentionRead, ...] = Field(
        default=(), validation_alias="mention_links"
    )
    created_at: datetime


class MySupportTicketCreate(BaseModel):
    """Customer self-care ticket creation. Deliberately omits every identity /
    assignment field of [TicketCreate] — the `/me` endpoint forces
    `subscriber_id` to the caller, so a customer can never raise a ticket on
    another account or self-assign it to staff."""

    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=255)
    description: str | None = None
    priority: str = TicketPriority.normal.value
    ticket_type: str | None = Field(default=None, max_length=80)


class MySupportCommentCreate(BaseModel):
    """Customer self-care reply. Only the body is accepted; the `/me` endpoint
    forces `is_internal=False` so customers can never post (or, by reading the
    filtered list, see) staff-internal notes."""

    model_config = ConfigDict(str_strip_whitespace=True)

    body: str = Field(min_length=1)


class TicketSlaEventBase(BaseModel):
    event_type: str = Field(min_length=1, max_length=80)
    expected_at: datetime | None = None
    actual_at: datetime | None = None
    metadata_: dict | None = Field(
        default=None, validation_alias="metadata", serialization_alias="metadata"
    )


class TicketSlaEventCreate(TicketSlaEventBase):
    ticket_id: UUID


class TicketSlaEventUpdate(BaseModel):
    event_type: str | None = Field(default=None, min_length=1, max_length=80)
    expected_at: datetime | None = None
    actual_at: datetime | None = None
    metadata_: dict | None = Field(
        default=None, validation_alias="metadata", serialization_alias="metadata"
    )


class TicketSlaEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    ticket_id: UUID
    event_type: str
    expected_at: datetime | None
    actual_at: datetime | None
    metadata_: dict | None = Field(
        default=None, validation_alias="metadata", serialization_alias="metadata"
    )
    created_at: datetime


class TicketLinkCreate(BaseModel):
    to_ticket_id: UUID
    link_type: str = Field(min_length=1, max_length=80)


class TicketMergeRequest(BaseModel):
    target_ticket_id: UUID
    reason: str | None = None


class TicketLookupQuery(BaseModel):
    ticket: str

    @field_validator("ticket")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        text = v.strip()
        if not text:
            raise ValueError("ticket cannot be empty")
        return text
