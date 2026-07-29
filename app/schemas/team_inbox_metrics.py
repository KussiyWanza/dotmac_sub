from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel


class InboxTeamPerformanceRead(BaseModel):
    service_team_id: UUID
    service_team_name: str
    service_team_capabilities: tuple[str, ...]
    response_sla_seconds: int | None = None
    conversation_count: int
    open_count: int
    unassigned_open_count: int
    assigned_open_count: int
    inbound_message_count: int
    outbound_message_count: int
    responded_count: int
    response_sla_breached_count: int
    response_rate: float | None = None
    response_sla_breach_rate: float | None = None
    average_first_response_seconds: float | None = None
    average_queue_wait_seconds: float | None = None


class InboxAgentPerformanceRead(BaseModel):
    person_id: UUID
    service_team_id: UUID
    service_team_name: str
    service_team_capabilities: tuple[str, ...]
    active_assignment_count: int
    handled_conversation_count: int
    average_queue_wait_seconds: float | None = None


class InboxEscalationCandidateRead(BaseModel):
    conversation_id: UUID
    service_team_id: UUID
    service_team_name: str
    service_team_capabilities: tuple[str, ...]
    subject: str | None = None
    contact_address: str | None = None
    status: str
    reasons: list[str]
    response_sla_seconds: int | None = None
    queue_sla_seconds: int | None = None
    pending_response_seconds: float | None = None
    queue_wait_seconds: float | None = None
    assigned_person_id: UUID | None = None
    available_agent_count: int
