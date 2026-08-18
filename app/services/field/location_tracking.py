"""Native field technician location ingest and presence snapshots."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Sequence
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.dispatch import DispatchQueueStatus, WorkOrderAssignmentQueue
from app.models.field_location import (
    FIELD_PRESENCE_STATUSES,
    FieldTechLocationPing,
    FieldTechPresence,
)
from app.models.work_order import WorkOrder
from app.services.field.jobs import _profile_from_principal
from app.services.field.work_order_status import FIELD_OPEN_WORK_ORDER_STATUSES

MAX_BATCH_PINGS = 200
MAX_FUTURE_CLOCK_SKEW = timedelta(minutes=5)
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LocationPingCommand:
    latitude: float
    longitude: float
    accuracy_m: float | None = None
    captured_at: datetime | None = None
    crm_work_order_id: str | None = None
    source: str = "mobile"
    status: str | None = None


@dataclass(frozen=True, slots=True)
class LocationIngestIssue:
    index: int
    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class LocationTransition:
    crm_work_order_id: str
    event: str
    distance_m: float


@dataclass(frozen=True, slots=True)
class LocationBatchOutcome:
    accepted: int
    errors: tuple[LocationIngestIssue, ...]
    presence: FieldTechPresence
    transitions: tuple[LocationTransition, ...]


class LocationPingRejected(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime | None) -> datetime:
    if value is None:
        return _now()
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _validate_status(status: str | None) -> str | None:
    if status is None:
        return None
    normalized = status.strip().lower()
    if normalized not in FIELD_PRESENCE_STATUSES:
        raise HTTPException(status_code=422, detail=f"Unsupported status: {status}")
    return normalized


class FieldLocationTracking:
    @staticmethod
    def _validate_work_order_tag(
        db: Session,
        *,
        technician_id: UUID,
        crm_work_order_id: str | None,
    ) -> None:
        if crm_work_order_id is None:
            return
        work_order = db.scalar(
            select(WorkOrder).where(WorkOrder.public_id == crm_work_order_id)
        )
        if work_order is None or not work_order.is_active:
            raise LocationPingRejected(
                "work_order_not_found", "Tagged work order was not found"
            )
        if work_order.status not in FIELD_OPEN_WORK_ORDER_STATUSES:
            raise LocationPingRejected(
                "work_order_not_trackable", "Tagged work order is not active"
            )
        assignment = db.scalar(
            select(WorkOrderAssignmentQueue)
            .where(WorkOrderAssignmentQueue.work_order_mirror_id == work_order.id)
            .order_by(
                WorkOrderAssignmentQueue.updated_at.desc(),
                WorkOrderAssignmentQueue.created_at.desc(),
            )
            .limit(1)
        )
        if (
            assignment is None
            or assignment.status != DispatchQueueStatus.assigned
            or assignment.assigned_technician_id != technician_id
        ):
            raise LocationPingRejected(
                "technician_not_assigned",
                "Technician is not assigned to the tagged work order",
            )

    @staticmethod
    def get_or_create_presence(
        db: Session,
        principal: dict[str, Any],
    ) -> FieldTechPresence:
        profile = _profile_from_principal(db, principal)
        presence = (
            db.query(FieldTechPresence)
            .filter(FieldTechPresence.technician_id == profile.id)
            .one_or_none()
        )
        if presence is None:
            presence = FieldTechPresence(
                technician_id=profile.id,
                person_id=profile.person_id,
            )
            db.add(presence)
            db.flush()
        return presence

    @staticmethod
    def set_sharing(
        db: Session,
        principal: dict[str, Any],
        *,
        enabled: bool,
        status: str | None = None,
    ) -> FieldTechPresence:
        presence = FieldLocationTracking.get_or_create_presence(db, principal)
        presence.location_sharing_enabled = bool(enabled)
        next_status = _validate_status(status)
        if next_status is not None:
            presence.status = next_status
        elif not enabled:
            presence.status = "off_shift"
        presence.last_seen_at = _now()
        db.commit()
        db.refresh(presence)
        return presence

    @staticmethod
    def record_ping(
        db: Session,
        principal: dict[str, Any],
        *,
        command: LocationPingCommand,
        commit: bool = True,
    ) -> tuple[FieldTechLocationPing, FieldTechPresence]:
        presence = FieldLocationTracking.get_or_create_presence(db, principal)
        FieldLocationTracking._validate_work_order_tag(
            db,
            technician_id=presence.technician_id,
            crm_work_order_id=command.crm_work_order_id,
        )
        next_status = _validate_status(command.status)
        captured = _as_utc(command.captured_at)
        now = _now()
        if captured > now + MAX_FUTURE_CLOCK_SKEW:
            raise LocationPingRejected(
                "captured_at_in_future",
                "Location timestamp is too far in the future",
            )
        ping = FieldTechLocationPing(
            technician_id=presence.technician_id,
            person_id=presence.person_id,
            crm_work_order_id=command.crm_work_order_id,
            latitude=float(command.latitude),
            longitude=float(command.longitude),
            accuracy_m=(
                float(command.accuracy_m)
                if command.accuracy_m is not None
                else None
            ),
            captured_at=captured,
            received_at=now,
            source=command.source or "mobile",
        )
        db.add(ping)

        if next_status is not None:
            presence.status = next_status
        presence.last_seen_at = now
        prior = presence.last_location_at
        if prior is not None and prior.tzinfo is None:
            prior = prior.replace(tzinfo=UTC)
        if prior is None or captured >= prior:
            presence.last_latitude = float(command.latitude)
            presence.last_longitude = float(command.longitude)
            presence.last_location_accuracy_m = (
                float(command.accuracy_m)
                if command.accuracy_m is not None
                else None
            )
            presence.last_location_at = captured

        if commit:
            db.commit()
            db.refresh(ping)
            db.refresh(presence)
        else:
            db.flush()
        return ping, presence

    @staticmethod
    def record_batch(
        db: Session,
        principal: dict[str, Any],
        pings: Sequence[LocationPingCommand],
    ) -> LocationBatchOutcome:
        if len(pings) > MAX_BATCH_PINGS:
            raise HTTPException(status_code=422, detail="Batch exceeds 200 pings")

        accepted = 0
        errors: list[LocationIngestIssue] = []
        last_presence: FieldTechPresence | None = None
        for index, command in enumerate(pings):
            try:
                _, last_presence = FieldLocationTracking.record_ping(
                    db,
                    principal,
                    command=command,
                    commit=False,
                )
                accepted += 1
            except LocationPingRejected as exc:
                errors.append(
                    LocationIngestIssue(
                        index=index, code=exc.code, detail=exc.detail
                    )
                )
            except HTTPException as exc:
                errors.append(
                    LocationIngestIssue(
                        index=index, code="invalid_ping", detail=str(exc.detail)
                    )
                )
            except (TypeError, ValueError) as exc:
                errors.append(
                    LocationIngestIssue(
                        index=index, code="invalid_ping", detail=str(exc)
                    )
                )

        db.commit()
        presence = (
            last_presence
            if last_presence is not None
            else FieldLocationTracking.get_or_create_presence(db, principal)
        )
        db.refresh(presence)
        transitions: list[LocationTransition] = []
        if presence.last_latitude is not None and presence.last_longitude is not None:
            try:
                from app.services.field import geofence

                transitions = [
                    LocationTransition(
                        crm_work_order_id=item["crm_work_order_id"],
                        event=item["event"],
                        distance_m=item["distance_m"],
                    )
                    for item in geofence.evaluate(
                        db,
                        principal,
                        presence.last_latitude,
                        presence.last_longitude,
                    )
                ]
            except Exception:
                logger.exception("geofence_evaluate_failed")
            db.refresh(presence)
        return LocationBatchOutcome(
            accepted=accepted,
            errors=tuple(errors),
            presence=presence,
            transitions=tuple(transitions),
        )


field_location_tracking = FieldLocationTracking()
