"""Native field job transition events for imported work-order mirrors."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.fiber_change_request import FiberChangeRequest
from app.models.fiber_topology_field_observation import FiberTopologyFieldObservation
from app.models.field_attachment import FieldAttachment
from app.models.field_fiber import FieldFiberTestResult
from app.models.field_job_event import FIELD_JOB_EVENTS, FieldJobEvent
from app.models.field_worklog import FieldWorkLog
from app.models.work_order import WorkOrder
from app.schemas.field import FieldCompletionRequirements
from app.services.common import coerce_uuid
from app.services.field.jobs import _profile_from_principal, _scoped_query
from app.services.field.source import mark_sub_authoritative
from app.services.field.work_order_status import WorkOrderStatus

_CLOCK_SKEW_FLAG_SECONDS = 15 * 60
_UNABLE_REASONS = {
    "customer_absent",
    "no_access",
    "site_not_ready",
    "needs_parts",
    "unsafe",
    "other",
}

_SCHEDULED = WorkOrderStatus.scheduled.value
_DISPATCHED = WorkOrderStatus.dispatched.value
_IN_PROGRESS = WorkOrderStatus.in_progress.value
_PAUSED = WorkOrderStatus.paused.value
_COMPLETED = WorkOrderStatus.completed.value
_CANCELED = WorkOrderStatus.canceled.value

_EVENT_TO_STATUS: dict[str, str | None] = {
    "accept": None,
    "en_route": _DISPATCHED,
    "arrived": None,
    "start": _IN_PROGRESS,
    "pause": _PAUSED,
    "hold": _PAUSED,
    "resume": _IN_PROGRESS,
    "complete": _COMPLETED,
    "unable_to_complete": _CANCELED,
}

_TRANSITION_ALLOWED_FROM: dict[str, set[str]] = {
    "accept": {_SCHEDULED, _DISPATCHED},
    "en_route": {_SCHEDULED, _DISPATCHED, _PAUSED},
    "arrived": {_SCHEDULED, _DISPATCHED, _IN_PROGRESS, _PAUSED},
    "start": {_SCHEDULED, _DISPATCHED},
    "pause": {_IN_PROGRESS},
    "hold": {_IN_PROGRESS},
    "resume": {_PAUSED},
    "complete": {_IN_PROGRESS},
    "unable_to_complete": {_SCHEDULED, _DISPATCHED, _IN_PROGRESS, _PAUSED},
}


def serialize_event(event: FieldJobEvent) -> dict:
    return {
        "id": event.id,
        "crm_work_order_id": event.work_order_mirror.public_id,
        "event": event.event,
        "previous_status": event.previous_status,
        "new_status": event.new_status,
        "person_id": event.person_id,
        "system_user_id": event.system_user_id,
        "latitude": event.latitude,
        "longitude": event.longitude,
        "note": event.note,
        "payload": event.payload or {},
        "occurred_at": event.occurred_at,
        "received_at": event.received_at,
        "client_event_id": event.client_event_id,
    }


class FieldTransitions:
    @staticmethod
    def completion_requirements(db: Session) -> FieldCompletionRequirements:
        """Return the same completion policy enforced by ``apply``."""
        return resolve_completion_requirements(db)

    @staticmethod
    def list_for_job(
        db: Session,
        principal: dict[str, Any],
        crm_work_order_id: str,
    ) -> list[dict]:
        row = _scoped_work_order(db, principal, crm_work_order_id)
        events = (
            db.query(FieldJobEvent)
            .filter(FieldJobEvent.work_order_mirror_id == row.id)
            .order_by(FieldJobEvent.occurred_at.asc(), FieldJobEvent.received_at.asc())
            .all()
        )
        return [serialize_event(event) for event in events]

    @staticmethod
    def apply(
        db: Session,
        principal: dict[str, Any],
        crm_work_order_id: str,
        *,
        event: str,
        client_event_id: UUID,
        occurred_at: datetime | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
        note: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict:
        event_value = _normalize_event(event)
        client_uuid = coerce_uuid(client_event_id)
        existing = (
            db.query(FieldJobEvent)
            .filter(FieldJobEvent.client_event_id == client_uuid)
            .one_or_none()
        )
        if existing is not None:
            row = _scoped_work_order_by_pk(db, principal, existing.work_order_mirror_id)
            return {
                "job": row,
                "event": serialize_event(existing),
                "replayed": True,
            }

        profile = _profile_from_principal(db, principal)
        row = (
            _scoped_query(db, profile)
            .filter(WorkOrder.public_id == crm_work_order_id)
            .with_for_update()
            .one_or_none()
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Job not found")
        if row.status not in _TRANSITION_ALLOWED_FROM[event_value]:
            raise HTTPException(
                status_code=409,
                detail=f"Cannot {event_value} a job in status {row.status}",
            )

        now = datetime.now(UTC)
        occurred = _as_utc(occurred_at) if occurred_at else now
        event_payload = dict(payload or {})
        skew = abs((now - occurred).total_seconds())
        if skew > _CLOCK_SKEW_FLAG_SECONDS:
            event_payload["clock_skew_seconds"] = int(skew)

        if event_value == "unable_to_complete":
            reason = event_payload.get("reason")
            reason = reason.strip() if isinstance(reason, str) else reason
            if not reason:
                raise HTTPException(
                    status_code=422,
                    detail="unable_to_complete requires a reason",
                )
            if reason not in _UNABLE_REASONS:
                raise HTTPException(
                    status_code=422,
                    detail=f"Invalid reason '{reason}'",
                )
            event_payload["reason"] = reason

        if event_value == "complete":
            _check_completion_gate(db, row, event_payload)
        if event_value in {"en_route", "arrived"}:
            from app.services.field.movements import validate_destination_payload

            validate_destination_payload(row, event_payload)

        previous_status = row.status
        new_status = _target_status(event_value, previous_status)
        if new_status is not None and new_status != previous_status:
            row.status = new_status
            _apply_status_timestamps(row, event_value, occurred)

        _mark_sub_authoritative(row, event_value, client_uuid, occurred)
        event_row = FieldJobEvent(
            work_order_mirror_id=row.id,
            author_technician_id=profile.id,
            person_id=profile.person_id,
            system_user_id=profile.system_user_id,
            event=event_value,
            previous_status=previous_status,
            new_status=row.status,
            latitude=latitude,
            longitude=longitude,
            note=(note or "").strip() or None,
            payload=event_payload or None,
            occurred_at=occurred,
            received_at=now,
            client_event_id=client_uuid,
        )
        db.add(event_row)
        _sync_timer(db, row, profile, event_value, occurred)
        _sync_movement(
            db,
            row,
            profile,
            event_value,
            client_uuid,
            occurred,
            latitude,
            longitude,
            event_payload,
        )
        if event_value in {"complete", "unable_to_complete"}:
            # The field outcome is this owner's committed output. It stages
            # atomically with the transition; the support lifecycle
            # projection handler delivers it to the handoff owner's receipted
            # consumer, which appends the ticket timeline evidence. Field
            # completion never changes ticket status.
            db.flush()
            from app.services.events import EventType, emit_event

            emit_event(
                db,
                EventType.work_order_field_outcome_recorded,
                {
                    "work_order_id": str(row.id),
                    "origin_ticket_id": str(row.origin_ticket_id)
                    if row.origin_ticket_id
                    else None,
                    "field_event_id": str(event_row.id),
                    "outcome": event_value,
                    "occurred_at": occurred.isoformat(),
                    "note": event_row.note,
                    "actor_id": str(profile.system_user_id or profile.person_id),
                },
                actor=str(profile.system_user_id or profile.person_id),
                subscriber_id=row.subscriber_id,
            )
        if event_value in {"en_route", "arrived", "complete", "unable_to_complete"}:
            from app.services import customer_experience_communications

            db.flush()
            customer_experience_communications.request_field_event(
                db,
                work_order=row,
                event=event_value,
                field_event_id=event_row.id,
            )
        if event_value in {"en_route", "complete", "unable_to_complete"}:
            # The customer's chat opens when their technician sets off and
            # closes when the visit ends, so it shares this transaction: a job
            # that departed must not be missing the chat that departure grants.
            from app.services import team_inbox_field_job

            db.flush()
            if event_value == "en_route":
                team_inbox_field_job.open_for_departure(
                    db, work_order=row, profile=profile, now=occurred
                )
            else:
                team_inbox_field_job.close_for_work_order(
                    db, work_order=row, reason=event_value, now=occurred
                )
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            replay = (
                db.query(FieldJobEvent)
                .filter(FieldJobEvent.client_event_id == client_uuid)
                .one_or_none()
            )
            if replay is not None:
                replay_row = db.get(WorkOrder, replay.work_order_mirror_id)
                return {
                    "job": replay_row,
                    "event": serialize_event(replay),
                    "replayed": True,
                }
            raise
        db.refresh(row)
        db.refresh(event_row)
        return {
            "job": row,
            "event": serialize_event(event_row),
            "replayed": False,
        }


def _scoped_work_order(
    db: Session,
    principal: dict[str, Any],
    crm_work_order_id: str,
) -> WorkOrder:
    profile = _profile_from_principal(db, principal)
    row = (
        _scoped_query(db, profile)
        .filter(WorkOrder.public_id == crm_work_order_id)
        .one_or_none()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return row


def _scoped_work_order_by_pk(
    db: Session,
    principal: dict[str, Any],
    work_order_pk,
) -> WorkOrder:
    profile = _profile_from_principal(db, principal)
    row = _scoped_query(db, profile).filter(WorkOrder.id == work_order_pk).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return row


def _normalize_event(event: str) -> str:
    value = (event or "").strip().lower()
    if value not in FIELD_JOB_EVENTS:
        raise HTTPException(status_code=422, detail=f"Unsupported field event: {event}")
    return value


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _target_status(event: str, previous_status: str) -> str | None:
    if event == "en_route" and previous_status == "paused":
        return None
    return _EVENT_TO_STATUS[event]


def _apply_status_timestamps(row: WorkOrder, event: str, occurred: datetime) -> None:
    if event == "start":
        row.started_at = row.started_at or occurred
        row.paused_at = None
        row.resumed_at = None
    elif event in {"pause", "hold"}:
        row.paused_at = occurred
    elif event == "resume":
        row.resumed_at = occurred
        row.paused_at = None
    elif event == "complete":
        row.completed_at = occurred
        row.paused_at = None
    elif event == "unable_to_complete":
        row.paused_at = None


def _completion_gate_enabled(db: Session) -> bool:
    setting = (
        db.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.field)
        .filter(DomainSetting.key == "completion_requires_evidence")
        .filter(DomainSetting.is_active.is_(True))
        .first()
    )
    if setting is None:
        return True
    value = setting.value_json if setting.value_json is not None else setting.value_text
    return str(value).lower() not in {"false", "0", "no"}


def resolve_completion_requirements(db: Session) -> FieldCompletionRequirements:
    """Resolve the canonical field completion contract from domain settings."""
    evidence_required = _completion_gate_enabled(db)
    return FieldCompletionRequirements(
        evidence_required=evidence_required,
        minimum_photo_count=1 if evidence_required else 0,
        customer_signoff_required=evidence_required,
        signature_unavailable_reason_allowed=evidence_required,
    )


@dataclass(frozen=True)
class FieldFiberAsBuiltEvidence:
    """Recorded fiber as-built evidence for one project-scoped work order.

    The requirement applies only to work orders that both declare
    ``requires_as_built_evidence`` and belong to a native project — the same
    population whose vendor deliverables are gated by project verification.
    """

    required: bool
    fiber_test_count: int
    source_observation_count: int
    splice_proposal_count: int

    @property
    def satisfied(self) -> bool:
        return not self.required or bool(
            self.fiber_test_count
            or self.source_observation_count
            or self.splice_proposal_count
        )


def resolve_fiber_as_built_evidence(
    db: Session, row: WorkOrder
) -> FieldFiberAsBuiltEvidence:
    """Count the fiber evidence records naming this exact work order."""
    required = bool(row.requires_as_built_evidence and row.project_id is not None)
    if not required:
        return FieldFiberAsBuiltEvidence(
            required=False,
            fiber_test_count=0,
            source_observation_count=0,
            splice_proposal_count=0,
        )
    fiber_test_count = (
        db.query(FieldFiberTestResult)
        .filter(FieldFiberTestResult.work_order_mirror_id == row.id)
        .count()
    )
    source_observation_count = (
        db.query(FiberTopologyFieldObservation)
        .filter(FiberTopologyFieldObservation.work_order_id == row.id)
        .count()
    )
    splice_proposal_count = (
        db.query(FiberChangeRequest)
        .filter(FiberChangeRequest.asset_type == "fiber_splice")
        .filter(FiberChangeRequest.payload["work_order_id"].as_string() == str(row.id))
        .count()
    )
    return FieldFiberAsBuiltEvidence(
        required=True,
        fiber_test_count=fiber_test_count,
        source_observation_count=source_observation_count,
        splice_proposal_count=splice_proposal_count,
    )


def _check_completion_gate(
    db: Session, row: WorkOrder, payload: dict[str, Any]
) -> None:
    requirements = resolve_completion_requirements(db)
    if not requirements.evidence_required:
        return
    attachments = (
        db.query(FieldAttachment)
        .filter(FieldAttachment.work_order_mirror_id == row.id)
        .filter(FieldAttachment.is_active.is_(True))
        .all()
    )
    photo_count = sum(attachment.kind == "photo" for attachment in attachments)
    has_signature = any(attachment.kind == "signature" for attachment in attachments)
    if photo_count < requirements.minimum_photo_count:
        raise HTTPException(
            status_code=422, detail="Completion requires at least one photo"
        )
    has_allowed_fallback = bool(
        requirements.signature_unavailable_reason_allowed
        and payload.get("signature_unavailable_reason")
    )
    if (
        requirements.customer_signoff_required
        and not has_signature
        and not has_allowed_fallback
    ):
        raise HTTPException(
            status_code=422,
            detail="Completion requires a customer signature or a signature_unavailable_reason",
        )
    fiber_evidence = resolve_fiber_as_built_evidence(db, row)
    if not fiber_evidence.satisfied:
        raise HTTPException(
            status_code=422,
            detail=(
                "Completion of this project work order requires recorded fiber "
                "as-built evidence: a fiber test, a source observation, or a "
                "splice proposal linked to this work order"
            ),
        )


def _mark_sub_authoritative(
    row: WorkOrder,
    event: str,
    client_event_id: UUID,
    occurred_at: datetime,
) -> None:
    mark_sub_authoritative(
        row,
        "transition",
        details={"event": event, "client_event_id": str(client_event_id)},
        occurred_at=occurred_at,
    )


def _sync_timer(
    db: Session,
    row: WorkOrder,
    profile,
    event: str,
    occurred_at: datetime,
) -> None:
    if event in {"start", "resume"}:
        open_log = _open_timer(db, profile.person_id)
        if open_log is None:
            db.add(
                FieldWorkLog(
                    work_order_mirror_id=row.id,
                    author_technician_id=profile.id,
                    person_id=profile.person_id,
                    system_user_id=profile.system_user_id,
                    start_at=occurred_at,
                    notes=f"Auto-started by {event}",
                )
            )
        return
    if event in {"pause", "hold", "complete", "unable_to_complete"}:
        open_log = _open_timer(db, profile.person_id)
        if open_log is not None:
            open_log.end_at = occurred_at
            open_log.minutes = max(
                0,
                int(
                    (
                        _as_utc(open_log.end_at) - _as_utc(open_log.start_at)
                    ).total_seconds()
                    // 60
                ),
            )


def _open_timer(db: Session, person_id) -> FieldWorkLog | None:
    return (
        db.query(FieldWorkLog)
        .filter(FieldWorkLog.person_id == person_id)
        .filter(FieldWorkLog.end_at.is_(None))
        .filter(FieldWorkLog.is_active.is_(True))
        .order_by(FieldWorkLog.start_at.desc())
        .first()
    )


def _sync_movement(
    db: Session,
    row: WorkOrder,
    profile,
    event: str,
    client_ref: UUID,
    occurred_at: datetime,
    latitude: float | None,
    longitude: float | None,
    payload: dict[str, Any],
) -> None:
    if event == "en_route":
        from app.services.field.movements import start_movement

        start_movement(
            db,
            row,
            profile,
            client_ref=client_ref,
            occurred_at=occurred_at,
            latitude=latitude,
            longitude=longitude,
            payload=payload,
        )
    elif event == "arrived":
        from app.services.field.movements import arrive_movement

        arrive_movement(
            db,
            row,
            profile,
            client_ref=client_ref,
            occurred_at=occurred_at,
            latitude=latitude,
            longitude=longitude,
            payload=payload,
        )


field_transitions = FieldTransitions()
