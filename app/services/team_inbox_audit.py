"""Typed Team Inbox lifecycle audit timeline and drift projection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.team_inbox import (
    InboxAuditEvidenceGrade,
    InboxConversation,
    InboxConversationAssignment,
    InboxRoutingEvent,
    InboxStatusTransitionEvent,
)


class InboxAuditEntryKind(StrEnum):
    routing = "routing"
    status = "status"


class InboxAuditDriftKind(StrEnum):
    status_projection_mismatch = "status_projection_mismatch"
    assignment_end_event_missing = "assignment_end_event_missing"
    assignment_end_event_mismatch = "assignment_end_event_mismatch"


@dataclass(frozen=True, slots=True)
class InboxAuditEntry:
    event_id: UUID
    kind: InboxAuditEntryKind
    action: str
    previous_value: str | None
    value: str | None
    actor_person_id: UUID | None
    reason_code: str
    occurred_at: datetime
    recorded_at: datetime
    evidence_grade: InboxAuditEvidenceGrade
    source_id: str


@dataclass(frozen=True, slots=True)
class InboxAuditDriftFinding:
    kind: InboxAuditDriftKind
    subject_id: UUID
    expected_value: str | None
    actual_value: str | None


@dataclass(frozen=True, slots=True)
class InboxConversationAuditTimeline:
    conversation_id: UUID
    entries: tuple[InboxAuditEntry, ...]
    findings: tuple[InboxAuditDriftFinding, ...]
    native_coverage_started_at: datetime | None
    has_pre_cutover_unknowns: bool


def conversation_audit_timeline(
    db: Session, *, conversation_id: UUID
) -> InboxConversationAuditTimeline:
    conversation = db.get(InboxConversation, conversation_id)
    if conversation is None:
        return InboxConversationAuditTimeline(
            conversation_id=conversation_id,
            entries=(),
            findings=(),
            native_coverage_started_at=None,
            has_pre_cutover_unknowns=False,
        )
    routing = (
        db.query(InboxRoutingEvent)
        .filter(InboxRoutingEvent.conversation_id == conversation_id)
        .all()
    )
    statuses = (
        db.query(InboxStatusTransitionEvent)
        .filter(InboxStatusTransitionEvent.conversation_id == conversation_id)
        .all()
    )
    entries = [
        InboxAuditEntry(
            event_id=event.id,
            kind=InboxAuditEntryKind.routing,
            action=event.event_type.value,
            previous_value=(
                str(event.previous_person_id) if event.previous_person_id else None
            ),
            value=str(event.person_id) if event.person_id else None,
            actor_person_id=event.actor_person_id,
            reason_code=event.reason_code,
            occurred_at=event.occurred_at,
            recorded_at=event.recorded_at,
            evidence_grade=event.evidence_grade,
            source_id=event.source_id,
        )
        for event in routing
    ]
    entries.extend(
        InboxAuditEntry(
            event_id=event.id,
            kind=InboxAuditEntryKind.status,
            action="status_changed",
            previous_value=event.previous_status,
            value=event.status,
            actor_person_id=event.actor_person_id,
            reason_code=event.reason_code,
            occurred_at=event.occurred_at,
            recorded_at=event.recorded_at,
            evidence_grade=event.evidence_grade,
            source_id=event.source_id,
        )
        for event in statuses
    )
    entries.sort(key=lambda item: (item.occurred_at, str(item.event_id)))
    findings: list[InboxAuditDriftFinding] = []
    if statuses:
        latest_status = max(statuses, key=lambda event: (event.occurred_at, event.id))
        if latest_status.status != conversation.status:
            findings.append(
                InboxAuditDriftFinding(
                    kind=InboxAuditDriftKind.status_projection_mismatch,
                    subject_id=conversation.id,
                    expected_value=latest_status.status,
                    actual_value=conversation.status,
                )
            )
    assignments = (
        db.query(InboxConversationAssignment)
        .filter(InboxConversationAssignment.conversation_id == conversation_id)
        .all()
    )
    routing_by_id = {event.id: event for event in routing}
    for assignment in assignments:
        if assignment.is_active or assignment.ended_at is None:
            continue
        if assignment.ended_by_event_id is None:
            findings.append(
                InboxAuditDriftFinding(
                    kind=InboxAuditDriftKind.assignment_end_event_missing,
                    subject_id=assignment.id,
                    expected_value="routing_event",
                    actual_value=None,
                )
            )
        elif assignment.ended_by_event_id not in routing_by_id:
            findings.append(
                InboxAuditDriftFinding(
                    kind=InboxAuditDriftKind.assignment_end_event_mismatch,
                    subject_id=assignment.id,
                    expected_value=str(assignment.ended_by_event_id),
                    actual_value=None,
                )
            )
    native_times = [
        item.recorded_at
        for item in entries
        if item.evidence_grade is InboxAuditEvidenceGrade.native
    ]
    return InboxConversationAuditTimeline(
        conversation_id=conversation_id,
        entries=tuple(entries),
        findings=tuple(findings),
        native_coverage_started_at=min(native_times) if native_times else None,
        has_pre_cutover_unknowns=any(
            not assignment.is_active and assignment.ended_at is None
            for assignment in assignments
        ),
    )
