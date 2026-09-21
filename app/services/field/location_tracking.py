"""Native field technician location ingest and presence snapshots."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError
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
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
    execute_owner_savepoint,
)

MAX_BATCH_PINGS = 200
MAX_FUTURE_CLOCK_SKEW = timedelta(minutes=5)
logger = logging.getLogger(__name__)

_OWNER = "operations.field_location_ingest"
_INGEST_CONCERN = "field-location ping ingest"
_SHARING_CONCERN = "field-location sharing preference"
_RECORD_PING_DEFINITION = OwnerCommandDefinition(
    owner=_OWNER, concern=_INGEST_CONCERN, name="record_ping"
)
_RECORD_BATCH_DEFINITION = OwnerCommandDefinition(
    owner=_OWNER, concern=_INGEST_CONCERN, name="record_batch"
)
_SET_SHARING_DEFINITION = OwnerCommandDefinition(
    owner=_OWNER, concern=_SHARING_CONCERN, name="set_sharing"
)


def _command_context(principal: dict[str, Any], *, reason: str) -> CommandContext:
    actor = str(
        principal.get("principal_id") or principal.get("person_id") or "unknown"
    )
    return CommandContext.system(actor=actor, scope=_OWNER, reason=reason)


@dataclass(frozen=True, slots=True)
class LocationPingCommand:
    latitude: float
    longitude: float
    accuracy_m: float | None = None
    captured_at: datetime | None = None
    crm_work_order_id: str | None = None
    source: str = "mobile"
    status: str | None = None
    client_observation_id: UUID | None = None


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
class LocationReplayInfo:
    """Observability record for a duplicate-submission ping.

    Always counted inside ``LocationBatchOutcome.accepted`` — never as an
    error — so an already-shipped client's ``accepted + len(errors) ==
    total_sent`` invariant holds whether or not it reads this field.
    """

    index: int
    ping_id: UUID


@dataclass(frozen=True, slots=True)
class LocationBatchOutcome:
    accepted: int
    errors: tuple[LocationIngestIssue, ...]
    presence: FieldTechPresence
    transitions: tuple[LocationTransition, ...]
    replays: tuple[LocationReplayInfo, ...] = ()


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
        def _operation() -> FieldTechPresence:
            presence = FieldLocationTracking.get_or_create_presence(db, principal)
            presence.location_sharing_enabled = bool(enabled)
            next_status = _validate_status(status)
            if next_status is not None:
                presence.status = next_status
            elif not enabled:
                presence.status = "off_shift"
            presence.last_seen_at = _now()
            db.flush()
            return presence

        return execute_owner_command(
            db,
            definition=_SET_SHARING_DEFINITION,
            context=_command_context(principal, reason="set_sharing"),
            operation=_operation,
        )

    @staticmethod
    def _find_existing_ping(
        db: Session,
        *,
        technician_id: UUID,
        client_observation_id: UUID,
    ) -> FieldTechLocationPing | None:
        return db.scalar(
            select(FieldTechLocationPing).where(
                FieldTechLocationPing.technician_id == technician_id,
                FieldTechLocationPing.client_observation_id == client_observation_id,
            )
        )

    @staticmethod
    def _replay_for(
        db: Session,
        principal: dict[str, Any],
        client_observation_id: UUID | None,
    ) -> tuple[FieldTechLocationPing, FieldTechPresence, bool] | None:
        """Re-read an already-persisted ping after a race-lost insert.

        Mirrors field_transitions.apply's IntegrityError recovery shape: the
        losing writer's own insert never lands, so it re-reads the winner's
        row under the same (technician_id, client_observation_id) key and
        returns it as a replay instead of surfacing the conflict.
        """

        if client_observation_id is None:
            return None
        profile = _profile_from_principal(db, principal)
        existing = FieldLocationTracking._find_existing_ping(
            db,
            technician_id=profile.id,
            client_observation_id=client_observation_id,
        )
        if existing is None:
            return None
        presence = FieldLocationTracking.get_or_create_presence(db, principal)
        return existing, presence, True

    @staticmethod
    def _ingest_ping(
        db: Session,
        principal: dict[str, Any],
        *,
        command: LocationPingCommand,
    ) -> tuple[FieldTechLocationPing, FieldTechPresence, bool]:
        presence = FieldLocationTracking.get_or_create_presence(db, principal)
        if command.client_observation_id is not None:
            # A replay is returned unconditionally, before any of today's
            # validation runs: it is the recorded outcome of an already-
            # accepted ping, not a new decision, so a work-order reassignment
            # or other state change since the original accept must not turn
            # a legitimate retry into a fresh rejection.
            existing = FieldLocationTracking._find_existing_ping(
                db,
                technician_id=presence.technician_id,
                client_observation_id=command.client_observation_id,
            )
            if existing is not None:
                return existing, presence, True
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
                float(command.accuracy_m) if command.accuracy_m is not None else None
            ),
            captured_at=captured,
            received_at=now,
            source=command.source or "mobile",
            client_observation_id=command.client_observation_id,
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
                float(command.accuracy_m) if command.accuracy_m is not None else None
            )
            presence.last_location_at = captured

        # The flush surfaces a row-level integrity conflict (a duplicate
        # (technician_id, client_observation_id), or a concurrent writer
        # racing this same key) inside this one ping's boundary instead of
        # at the eventual owner-command commit, where it would be
        # indistinguishable from every other row in a batch.
        db.flush()
        return ping, presence, False

    @staticmethod
    def record_ping(
        db: Session,
        principal: dict[str, Any],
        *,
        command: LocationPingCommand,
        commit: bool = True,
    ) -> tuple[FieldTechLocationPing, FieldTechPresence, bool]:
        def _operation() -> tuple[FieldTechLocationPing, FieldTechPresence, bool]:
            return FieldLocationTracking._ingest_ping(db, principal, command=command)

        if not commit:
            # Called from inside record_batch's already-open owner command and
            # per-row savepoint; the batch owns the transaction boundary and
            # its own IntegrityError recovery (see record_batch below), so
            # this call is a plain participant, not its own owner command.
            return _operation()

        try:
            return execute_owner_command(
                db,
                definition=_RECORD_PING_DEFINITION,
                context=_command_context(principal, reason="record_ping"),
                operation=_operation,
            )
        except (IntegrityError, DBAPIError):
            # execute_owner_command has already rolled back the whole
            # command transaction by the time this exception reaches here,
            # leaving the session transaction-free — exactly the state
            # field_transitions.apply's own db.rollback() produces before its
            # re-read. A genuine concurrent double-submit of the same
            # (technician_id, client_observation_id) loses this race; the
            # loser recovers by re-reading the winner's row as a replay
            # instead of surfacing the conflict.
            replay = FieldLocationTracking._replay_for(
                db, principal, command.client_observation_id
            )
            if replay is None:
                raise
            return replay

    @staticmethod
    def record_batch(
        db: Session,
        principal: dict[str, Any],
        pings: Sequence[LocationPingCommand],
    ) -> LocationBatchOutcome:
        if len(pings) > MAX_BATCH_PINGS:
            raise HTTPException(status_code=422, detail="Batch exceeds 200 pings")

        def _operation() -> tuple[
            int,
            list[LocationIngestIssue],
            FieldTechPresence | None,
            list[LocationReplayInfo],
        ]:
            accepted = 0
            errors: list[LocationIngestIssue] = []
            replays: list[LocationReplayInfo] = []
            last_presence: FieldTechPresence | None = None
            for index, command in enumerate(pings):

                def _ingest_row(
                    command: LocationPingCommand = command,
                ) -> tuple[FieldTechLocationPing, FieldTechPresence, bool]:
                    return FieldLocationTracking.record_ping(
                        db,
                        principal,
                        command=command,
                        commit=False,
                    )

                try:
                    ping, last_presence, replayed = execute_owner_savepoint(
                        db, _ingest_row
                    )
                    accepted += 1
                    if replayed:
                        replays.append(LocationReplayInfo(index=index, ping_id=ping.id))
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
                except (IntegrityError, DBAPIError):
                    # A per-row savepoint isolates this failure: the flush
                    # inside the savepoint has already been rolled back by
                    # execute_owner_savepoint, so every other row in the
                    # batch, and the batch's own commit, are unaffected. If
                    # this row carried a client_observation_id, the conflict
                    # is a duplicate-submission race rather than a genuine
                    # error: re-read the winner's row (still visible — only
                    # the savepoint rolled back, not the batch transaction)
                    # and count it as an accepted replay, exactly like a
                    # non-racing duplicate already would be.
                    replay = FieldLocationTracking._replay_for(
                        db, principal, command.client_observation_id
                    )
                    if replay is not None:
                        ping, last_presence, _ = replay
                        accepted += 1
                        replays.append(LocationReplayInfo(index=index, ping_id=ping.id))
                    else:
                        errors.append(
                            LocationIngestIssue(
                                index=index,
                                code="ping_conflict",
                                detail="Ping conflicted with an existing record",
                            )
                        )
            return accepted, errors, last_presence, replays

        accepted, errors, last_presence, replays = execute_owner_command(
            db,
            definition=_RECORD_BATCH_DEFINITION,
            context=_command_context(principal, reason="record_batch"),
            operation=_operation,
        )
        presence = (
            last_presence
            if last_presence is not None
            else FieldLocationTracking.get_or_create_presence(db, principal)
        )

        # Geofence auto-status is a deliberately separate follow-up operation,
        # not a participant of the owner command above. field_transitions.apply
        # (called by geofence.evaluate) commits its own root transaction, and
        # composing that inside the ingest owner command would both violate
        # "one owner command commits one transaction" and trip the owner
        # command boundary guard (apply()'s own commit call would raise,
        # because only the active owner command may complete its own
        # transaction).
        # Running it here, once the ingest command has already committed,
        # keeps ping ingest and geofence auto-start as two independently
        # failing operations: a geofence failure never unwinds ingested pings.
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
        return LocationBatchOutcome(
            accepted=accepted,
            errors=tuple(errors),
            presence=presence,
            transitions=tuple(transitions),
            replays=tuple(replays),
        )


field_location_tracking = FieldLocationTracking()
