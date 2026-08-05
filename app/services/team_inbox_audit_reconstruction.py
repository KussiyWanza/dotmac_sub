"""Reviewed, deterministic historical Team Inbox audit reconstruction."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.team_inbox import (
    InboxAgentPresence,
    InboxAgentPresenceEvent,
    InboxAuditEvidenceGrade,
    InboxAuditReconstructionRun,
    InboxAuditSource,
    InboxConversation,
    InboxConversationAssignment,
    InboxRoutingDecisionMode,
    InboxRoutingEvent,
    InboxRoutingEventType,
    InboxStatusTransitionEvent,
)
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

OWNER = "communications.team_inbox_audit_reconstruction"
_BACKFILL_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="reviewed Team Inbox historical audit reconstruction",
    name="execute_team_inbox_audit_backfill",
)


class ReconstructionKind(StrEnum):
    assignment_started = "assignment_started"
    status_changed = "status_changed"
    presence_changed = "presence_changed"
    assignment_end_unknown = "assignment_end_unknown"


@dataclass(frozen=True, slots=True)
class ReconstructionItem:
    source_id: str
    kind: ReconstructionKind
    subject_id: UUID
    previous_value: str | None
    value: str | None
    actor_person_id: UUID | None
    occurred_at: datetime | None
    evidence_grade: InboxAuditEvidenceGrade


@dataclass(frozen=True, slots=True)
class ReconstructionManifest:
    generated_at: datetime
    source_watermark: str
    items: tuple[ReconstructionItem, ...]
    sha256: str


@dataclass(frozen=True, slots=True)
class ApplyReconstructionCommand:
    expected_manifest_sha256: str
    expected_source_watermark: str
    actor_person_id: UUID
    approval_reference: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class ApplyReconstructionOutcome:
    manifest_sha256: str
    applied: int
    exceptions: int


class ReconstructionError(RuntimeError):
    pass


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _parse_uuid(value: object) -> UUID | None:
    try:
        return UUID(str(value)) if value else None
    except (TypeError, ValueError):
        return None


def _payload_hash(items: tuple[ReconstructionItem, ...], watermark: str) -> str:
    payload = {
        "source_watermark": watermark,
        "items": [
            {
                **asdict(item),
                "kind": item.kind.value,
                "subject_id": str(item.subject_id),
                "actor_person_id": (
                    str(item.actor_person_id) if item.actor_person_id else None
                ),
                "occurred_at": (
                    item.occurred_at.isoformat() if item.occurred_at else None
                ),
                "evidence_grade": item.evidence_grade.value,
            }
            for item in items
        ],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def preview_reconstruction(db: Session) -> ReconstructionManifest:
    """Build a complete deterministic preview without writing."""

    items: list[ReconstructionItem] = []
    existing_routing_sources = {
        row[0]
        for row in db.query(InboxRoutingEvent.source_id)
        .filter(InboxRoutingEvent.source == InboxAuditSource.historical_backfill)
        .all()
    }
    existing_status_sources = {
        row[0]
        for row in db.query(InboxStatusTransitionEvent.source_id)
        .filter(
            InboxStatusTransitionEvent.source == InboxAuditSource.historical_backfill
        )
        .all()
    }
    existing_presence_sources = {
        row[0]
        for row in db.query(InboxAgentPresenceEvent.source_id)
        .filter(InboxAgentPresenceEvent.source == InboxAuditSource.historical_backfill)
        .all()
    }
    assignments = db.query(InboxConversationAssignment).order_by(
        InboxConversationAssignment.assigned_at,
        InboxConversationAssignment.id,
    )
    for row in assignments:
        assignment_source = f"assignment:{row.id}:start"
        if assignment_source not in existing_routing_sources:
            items.append(
                ReconstructionItem(
                    source_id=assignment_source,
                    kind=ReconstructionKind.assignment_started,
                    subject_id=row.id,
                    previous_value=None,
                    value=str(row.person_id),
                    actor_person_id=row.assigned_by_person_id,
                    occurred_at=row.assigned_at,
                    evidence_grade=InboxAuditEvidenceGrade.authoritative_historical,
                )
            )
        if not row.is_active and row.ended_at is None:
            items.append(
                ReconstructionItem(
                    source_id=f"assignment:{row.id}:end-unknown",
                    kind=ReconstructionKind.assignment_end_unknown,
                    subject_id=row.id,
                    previous_value=str(row.person_id),
                    value=None,
                    actor_person_id=None,
                    occurred_at=None,
                    evidence_grade=InboxAuditEvidenceGrade.unknown,
                )
            )
    conversations = db.query(InboxConversation).order_by(InboxConversation.id)
    for conversation in conversations:
        history = (conversation.metadata_ or {}).get("status_history")
        if not isinstance(history, list):
            continue
        for index, raw in enumerate(history):
            if not isinstance(raw, dict):
                continue
            source_id = f"conversation:{conversation.id}:status:{index}"
            if source_id in existing_status_sources:
                continue
            occurred_at = _parse_time(raw.get("at"))
            value = str(raw.get("to") or "").strip() or None
            grade = (
                InboxAuditEvidenceGrade.authoritative_historical
                if occurred_at and value
                else InboxAuditEvidenceGrade.unknown
            )
            items.append(
                ReconstructionItem(
                    source_id=source_id,
                    kind=ReconstructionKind.status_changed,
                    subject_id=conversation.id,
                    previous_value=str(raw.get("from") or "").strip() or None,
                    value=value,
                    actor_person_id=_parse_uuid(raw.get("actor_id")),
                    occurred_at=occurred_at,
                    evidence_grade=grade,
                )
            )
    presences = db.query(InboxAgentPresence).order_by(InboxAgentPresence.person_id)
    for presence in presences:
        history = (presence.metadata_ or {}).get("manual_status_history")
        if not isinstance(history, list):
            continue
        for index, raw in enumerate(history):
            if not isinstance(raw, dict):
                continue
            source_id = f"presence:{presence.person_id}:{index}"
            if source_id in existing_presence_sources:
                continue
            occurred_at = _parse_time(raw.get("at"))
            value = str(raw.get("to") or "").strip() or None
            items.append(
                ReconstructionItem(
                    source_id=source_id,
                    kind=ReconstructionKind.presence_changed,
                    subject_id=presence.person_id,
                    previous_value=str(raw.get("from") or "").strip() or None,
                    value=value,
                    actor_person_id=None,
                    occurred_at=occurred_at,
                    evidence_grade=(
                        InboxAuditEvidenceGrade.authoritative_historical
                        if occurred_at and value
                        else InboxAuditEvidenceGrade.unknown
                    ),
                )
            )
    ordered = tuple(sorted(items, key=lambda item: item.source_id))
    maxima = (
        db.query(InboxConversation.updated_at)
        .order_by(InboxConversation.updated_at.desc())
        .first(),
        db.query(InboxConversationAssignment.updated_at)
        .order_by(InboxConversationAssignment.updated_at.desc())
        .first(),
        db.query(InboxAgentPresence.updated_at)
        .order_by(InboxAgentPresence.updated_at.desc())
        .first(),
    )
    watermark = "|".join(
        str(row[0].isoformat() if row and row[0] else "empty") for row in maxima
    )
    return ReconstructionManifest(
        generated_at=datetime.now(UTC),
        source_watermark=watermark,
        items=ordered,
        sha256=_payload_hash(ordered, watermark),
    )


def apply_reconstruction(
    db: Session, command: ApplyReconstructionCommand
) -> ApplyReconstructionOutcome:
    def operation() -> ApplyReconstructionOutcome:
        replay = (
            db.query(InboxAuditReconstructionRun)
            .filter(
                InboxAuditReconstructionRun.idempotency_key == command.idempotency_key
            )
            .one_or_none()
        )
        if replay is not None:
            if (
                replay.manifest_sha256 != command.expected_manifest_sha256
                or replay.source_watermark != command.expected_source_watermark
            ):
                raise ReconstructionError(
                    "Idempotency key was already used for different evidence"
                )
            return ApplyReconstructionOutcome(
                manifest_sha256=replay.manifest_sha256,
                applied=replay.applied_count,
                exceptions=replay.exception_count,
            )
        manifest = preview_reconstruction(db)
        if manifest.sha256 != command.expected_manifest_sha256:
            raise ReconstructionError("Reviewed manifest hash no longer matches")
        if manifest.source_watermark != command.expected_source_watermark:
            raise ReconstructionError("Source evidence changed after review")
        if not command.approval_reference.strip():
            raise ReconstructionError("Approval reference is required")
        applied = 0
        exceptions = 0
        assignments = {
            row.id: row for row in db.query(InboxConversationAssignment).all()
        }
        for item in manifest.items:
            if item.evidence_grade is InboxAuditEvidenceGrade.unknown:
                exceptions += 1
                continue
            if item.kind is ReconstructionKind.assignment_started:
                assignment = assignments[item.subject_id]
                db.add(
                    InboxRoutingEvent(
                        conversation_id=assignment.conversation_id,
                        event_type=InboxRoutingEventType.assigned,
                        service_team_id=assignment.service_team_id,
                        person_id=assignment.person_id,
                        actor_person_id=item.actor_person_id,
                        decision_mode=InboxRoutingDecisionMode.system,
                        reason_code="historical_assignment_record",
                        source=InboxAuditSource.historical_backfill,
                        source_id=item.source_id,
                        evidence_grade=item.evidence_grade,
                        occurred_at=item.occurred_at,
                    )
                )
            elif item.kind is ReconstructionKind.status_changed:
                db.add(
                    InboxStatusTransitionEvent(
                        conversation_id=item.subject_id,
                        previous_status=item.previous_value,
                        status=item.value,
                        actor_person_id=item.actor_person_id,
                        reason_code="historical_reconstruction",
                        source=InboxAuditSource.historical_backfill,
                        source_id=item.source_id,
                        evidence_grade=item.evidence_grade,
                        occurred_at=item.occurred_at,
                    )
                )
            elif item.kind is ReconstructionKind.presence_changed:
                db.add(
                    InboxAgentPresenceEvent(
                        person_id=item.subject_id,
                        previous_status=item.previous_value,
                        status=item.value,
                        actor_person_id=None,
                        reason_code="historical_reconstruction",
                        source=InboxAuditSource.historical_backfill,
                        source_id=item.source_id,
                        evidence_grade=item.evidence_grade,
                        occurred_at=item.occurred_at,
                    )
                )
            applied += 1
        db.flush()
        db.add(
            InboxAuditReconstructionRun(
                idempotency_key=command.idempotency_key,
                manifest_sha256=manifest.sha256,
                source_watermark=manifest.source_watermark,
                approval_reference=command.approval_reference.strip(),
                actor_person_id=command.actor_person_id,
                applied_count=applied,
                exception_count=exceptions,
            )
        )
        db.flush()
        return ApplyReconstructionOutcome(
            manifest_sha256=manifest.sha256,
            applied=applied,
            exceptions=exceptions,
        )

    return execute_owner_command(
        db,
        definition=_BACKFILL_COMMAND,
        context=CommandContext.system(
            actor=f"person:{command.actor_person_id}",
            scope="team-inbox:audit-reconstruction",
            reason=f"approved reconstruction {command.approval_reference}",
            idempotency_key=command.idempotency_key,
        ),
        operation=operation,
    )
