"""Read-only review queue for live CPE/ONTs without an active assignment.

This detector deliberately never creates or reactivates ``OntAssignment``.
RADIUS and GenieACS are observations, not assignment authority: they can prove
that staff should review a mismatch, but only the assignment owner may repair
it after the customer/device identity is verified.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID

from sqlalchemy import exists, func, select
from sqlalchemy.engine import Row
from sqlalchemy.orm import Session

from app.models.network import CPEDevice, OntAssignment, OntUnit
from app.models.radius_active_session import RadiusActiveSession
from app.models.tr069 import Tr069CpeDevice
from app.services.network.radius_sessions import ACTIVE_SESSION_FRESHNESS


class AssignmentDriftSeverity(StrEnum):
    blocking = "blocking"
    advisory = "advisory"


@dataclass(frozen=True, slots=True)
class LiveDeviceAssignmentGap:
    """One review item backed by an independent live-service observation."""

    severity: AssignmentDriftSeverity
    code: str
    ont_unit_id: UUID
    ont_serial_number: str | None
    tr069_device_id: UUID
    cpe_device_id: UUID | None
    subscriber_id: UUID | None
    subscription_id: UUID | None
    observed_at: datetime
    evidence: str
    next_action: str


@dataclass(frozen=True, slots=True)
class LiveDeviceAssignmentDrift:
    evaluated_at: datetime
    blocking_count: int
    advisory_count: int
    rows: tuple[LiveDeviceAssignmentGap, ...]

    @property
    def total_count(self) -> int:
        return self.blocking_count + self.advisory_count


def _active_assignment_exists():
    return exists(
        select(OntAssignment.id).where(
            OntAssignment.ont_unit_id == Tr069CpeDevice.ont_unit_id,
            OntAssignment.active.is_(True),
        )
    )


def _fresh_radius_observed_at(cutoff: datetime):
    observed_at = func.coalesce(
        RadiusActiveSession.last_update,
        RadiusActiveSession.session_start,
        RadiusActiveSession.created_at,
    )
    return (
        select(func.max(observed_at))
        .where(
            CPEDevice.subscription_id.is_not(None),
            RadiusActiveSession.subscription_id == CPEDevice.subscription_id,
            observed_at >= cutoff,
        )
        .correlate(CPEDevice)
        .scalar_subquery()
    )


def find_live_devices_without_active_assignment(
    db: Session,
    *,
    now: datetime | None = None,
    recent_inform_within: timedelta = timedelta(hours=24),
    limit: int = 100,
) -> LiveDeviceAssignmentDrift:
    """Build the bounded operator queue without mutating assignment state.

    A fresh, exact-subscription RADIUS session is blocking/high-confidence.
    A recent Inform alone is advisory because inventory and commissioning ONTs
    may legitimately contact GenieACS before customer assignment.
    """

    evaluated_at = now or datetime.now(UTC)
    if evaluated_at.tzinfo is None:
        evaluated_at = evaluated_at.replace(tzinfo=UTC)
    evaluated_at = evaluated_at.astimezone(UTC)
    row_limit = max(1, min(int(limit), 500))
    radius_cutoff = evaluated_at - ACTIVE_SESSION_FRESHNESS
    inform_cutoff = evaluated_at - recent_inform_within
    radius_observed_at = _fresh_radius_observed_at(radius_cutoff)
    no_assignment = ~_active_assignment_exists()

    radius_ids = (
        select(Tr069CpeDevice.id)
        .join(CPEDevice, Tr069CpeDevice.cpe_device_id == CPEDevice.id)
        .join(OntUnit, Tr069CpeDevice.ont_unit_id == OntUnit.id)
        .where(
            Tr069CpeDevice.is_active.is_(True),
            Tr069CpeDevice.ont_unit_id.is_not(None),
            CPEDevice.subscription_id.is_not(None),
            radius_observed_at.is_not(None),
            no_assignment,
        )
        .distinct()
    )
    blocking_count = int(
        db.scalar(select(func.count()).select_from(radius_ids.subquery())) or 0
    )

    radius_rows = db.execute(
        select(Tr069CpeDevice, CPEDevice, OntUnit, radius_observed_at)
        .join(CPEDevice, Tr069CpeDevice.cpe_device_id == CPEDevice.id)
        .join(OntUnit, Tr069CpeDevice.ont_unit_id == OntUnit.id)
        .where(
            Tr069CpeDevice.is_active.is_(True),
            Tr069CpeDevice.ont_unit_id.is_not(None),
            CPEDevice.subscription_id.is_not(None),
            radius_observed_at.is_not(None),
            no_assignment,
        )
        .order_by(radius_observed_at.desc(), Tr069CpeDevice.id)
        .limit(row_limit)
    ).all()

    advisory_ids = (
        select(Tr069CpeDevice.id)
        .outerjoin(CPEDevice, Tr069CpeDevice.cpe_device_id == CPEDevice.id)
        .join(OntUnit, Tr069CpeDevice.ont_unit_id == OntUnit.id)
        .where(
            Tr069CpeDevice.is_active.is_(True),
            Tr069CpeDevice.ont_unit_id.is_not(None),
            Tr069CpeDevice.last_inform_at.is_not(None),
            Tr069CpeDevice.last_inform_at >= inform_cutoff,
            radius_observed_at.is_(None),
            no_assignment,
        )
        .distinct()
    )
    advisory_count = int(
        db.scalar(select(func.count()).select_from(advisory_ids.subquery())) or 0
    )
    remaining = max(row_limit - len(radius_rows), 0)
    advisory_rows: Sequence[Row[tuple[Tr069CpeDevice, CPEDevice, OntUnit]]] = ()
    if remaining:
        advisory_rows = db.execute(
            select(Tr069CpeDevice, CPEDevice, OntUnit)
            .outerjoin(CPEDevice, Tr069CpeDevice.cpe_device_id == CPEDevice.id)
            .join(OntUnit, Tr069CpeDevice.ont_unit_id == OntUnit.id)
            .where(
                Tr069CpeDevice.is_active.is_(True),
                Tr069CpeDevice.ont_unit_id.is_not(None),
                Tr069CpeDevice.last_inform_at.is_not(None),
                Tr069CpeDevice.last_inform_at >= inform_cutoff,
                radius_observed_at.is_(None),
                no_assignment,
            )
            .order_by(Tr069CpeDevice.last_inform_at.desc(), Tr069CpeDevice.id)
            .limit(remaining)
        ).all()

    rows: list[LiveDeviceAssignmentGap] = []
    for tr069, cpe, ont, observed_at in radius_rows:
        rows.append(
            LiveDeviceAssignmentGap(
                severity=AssignmentDriftSeverity.blocking,
                code="network.cpe_assignment_drift.active_radius_without_assignment",
                ont_unit_id=ont.id,
                ont_serial_number=ont.serial_number,
                tr069_device_id=tr069.id,
                cpe_device_id=cpe.id,
                subscriber_id=cpe.subscriber_id,
                subscription_id=cpe.subscription_id,
                observed_at=observed_at,
                evidence="Fresh exact-subscription RADIUS session",
                next_action=(
                    "Verify the customer and replacement ONT, then repair through "
                    "the ONT assignment command; never auto-assign from this signal."
                ),
            )
        )
    for tr069, cpe, ont in advisory_rows:
        assert tr069.last_inform_at is not None
        rows.append(
            LiveDeviceAssignmentGap(
                severity=AssignmentDriftSeverity.advisory,
                code="network.cpe_assignment_drift.recent_inform_without_assignment",
                ont_unit_id=ont.id,
                ont_serial_number=ont.serial_number,
                tr069_device_id=tr069.id,
                cpe_device_id=cpe.id if cpe is not None else None,
                subscriber_id=cpe.subscriber_id if cpe is not None else None,
                subscription_id=cpe.subscription_id if cpe is not None else None,
                observed_at=tr069.last_inform_at,
                evidence="Recent GenieACS Inform without live RADIUS corroboration",
                next_action=(
                    "Confirm this is customer-serving equipment before using the ONT "
                    "assignment command; commissioning-only devices may be legitimate."
                ),
            )
        )

    return LiveDeviceAssignmentDrift(
        evaluated_at=evaluated_at,
        blocking_count=blocking_count,
        advisory_count=advisory_count,
        rows=tuple(rows),
    )


__all__ = (
    "AssignmentDriftSeverity",
    "LiveDeviceAssignmentDrift",
    "LiveDeviceAssignmentGap",
    "find_live_devices_without_active_assignment",
)
