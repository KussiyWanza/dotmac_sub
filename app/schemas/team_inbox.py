from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class InboxConversationEscalateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    service_team_id: UUID
    assigned_person_id: UUID | None = None
    auto_assign: bool = True
    reason: str | None = Field(default=None, max_length=500)


class InboxConversationEscalationRead(BaseModel):
    conversation_id: UUID
    kind: str
    service_team_id: UUID | None = None
    assigned_person_id: UUID | None = None
    reason: str | None = None


class InboxConversationReplyRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    body_html: str = Field(min_length=1)
    body_text: str | None = None
    subject: str | None = Field(default=None, max_length=200)
    to_email: str | None = Field(default=None, max_length=255)


class InboxConversationReplyRead(BaseModel):
    conversation_id: UUID
    kind: str
    message_id: UUID | None = None
    service_team_id: UUID | None = None
    sender_key: str | None = None
    activity: str | None = None
    from_address: str | None = None
    to_email: str | None = None
    reason: str | None = None


class InboxConversationContactLinkRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    subscriber_id: UUID | None = None
    reseller_id: UUID | None = None
    note: str | None = Field(default=None, max_length=500)


class InboxConversationContactLinkRead(BaseModel):
    conversation_id: UUID
    contact_link_id: UUID
    channel_type: str
    normalized_contact: str
    subscriber_id: UUID | None = None
    reseller_id: UUID | None = None
    previous_link_ids_deactivated: list[UUID] = Field(default_factory=list)


class InboxTimelineTeamRead(BaseModel):
    service_team_id: UUID
    service_team_name: str | None = None
    service_team_capabilities: tuple[str, ...] = ()
    role: str
    source: str
    is_active: bool


class InboxTimelineAssignmentRead(BaseModel):
    person_id: UUID
    service_team_id: UUID
    service_team_name: str | None = None
    assigned_by_person_id: UUID | None = None
    assigned_at: datetime
    is_active: bool


class InboxTimelineMessageRead(BaseModel):
    id: UUID
    channel_type: str
    direction: str
    subject: str | None = None
    body: str | None = None
    from_address: str | None = None
    to_addresses: list = Field(default_factory=list)
    cc_addresses: list = Field(default_factory=list)
    sent_at: datetime | None = None
    received_at: datetime | None = None
    created_at: datetime
    metadata: dict | None = None
    attachments: list[dict] = Field(default_factory=list)


class InboxConversationTimelineRead(BaseModel):
    id: UUID
    subscriber_id: UUID | None = None
    primary_service_team_id: UUID | None = None
    channel_type: str
    status: str
    priority: int = 100
    is_muted: bool = False
    snoozed_until: datetime | None = None
    subject: str | None = None
    contact_address: str | None = None
    external_thread_id: str | None = None
    first_message_at: datetime | None = None
    last_message_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    metadata: dict | None = None
    teams: list[InboxTimelineTeamRead] = Field(default_factory=list)
    assignments: list[InboxTimelineAssignmentRead] = Field(default_factory=list)
    messages: list[InboxTimelineMessageRead] = Field(default_factory=list)


class InboxConversationListItemRead(BaseModel):
    id: UUID
    subscriber_id: UUID | None = None
    primary_service_team_id: UUID | None = None
    primary_service_team_name: str | None = None
    primary_service_team_capabilities: tuple[str, ...] = ()
    channel_type: str
    status: str
    priority: int = 100
    is_muted: bool = False
    snoozed_until: datetime | None = None
    is_snoozed: bool = False
    subject: str | None = None
    contact_address: str | None = None
    first_message_at: datetime | None = None
    last_message_at: datetime | None = None
    latest_message_direction: str | None = None
    latest_message_body: str | None = None
    latest_message_at: datetime | None = None
    contact_resolution_status: str | None = None
    latest_delivery_status: str | None = None
    active_assigned_person_id: UUID | None = None
    needs_response: bool
    team_count: int
