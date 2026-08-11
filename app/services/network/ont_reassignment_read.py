"""Read projections for controlled ONT reassignment forms."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload

from app.models.network import OLTDevice, OntAssignment, OntUnit, PonPort


@dataclass(frozen=True, slots=True)
class OntInventoryChoice:
    ont_unit_id: uuid.UUID
    serial_number: str
    mac_address: str | None
    olt_id: uuid.UUID
    olt_name: str
    pon_port_id: uuid.UUID
    pon_port_name: str | None


@dataclass(frozen=True, slots=True)
class ActiveAssignmentChoice:
    assignment: OntAssignment | None
    error: str | None = None


def active_assignment_for_reassignment(
    db: Session,
    *,
    subscription_id: uuid.UUID,
) -> ActiveAssignmentChoice:
    """Return the single active ONT assignment for a subscription form."""

    assignments = db.scalars(
        select(OntAssignment)
        .options(
            joinedload(OntAssignment.ont_unit).joinedload(OntUnit.olt_device),
            joinedload(OntAssignment.pon_port),
        )
        .where(
            OntAssignment.subscription_id == subscription_id,
            OntAssignment.active.is_(True),
        )
        .order_by(OntAssignment.id)
        .limit(2)
    ).all()
    if len(assignments) > 1:
        return ActiveAssignmentChoice(
            assignment=None,
            error="This subscription has ambiguous active ONT assignments.",
        )
    return ActiveAssignmentChoice(
        assignment=assignments[0] if assignments else None,
    )


def eligible_reassignment_targets(
    db: Session,
    *,
    search: str | None = None,
    exclude_ont_unit_id: uuid.UUID | None = None,
    limit: int = 50,
) -> tuple[OntInventoryChoice, ...]:
    """Return existing unassigned ONTs that can be selected for reassignment."""

    active_assignment = (
        select(OntAssignment.id)
        .where(
            OntAssignment.ont_unit_id == OntUnit.id,
            OntAssignment.active.is_(True),
        )
        .exists()
    )
    stmt = (
        select(OntUnit, PonPort, OLTDevice)
        .join(PonPort, PonPort.id == OntUnit.pon_port_id)
        .join(OLTDevice, OLTDevice.id == OntUnit.olt_device_id)
        .where(PonPort.is_active.is_(True))
        .where(OLTDevice.is_active.is_(True))
        .where(~active_assignment)
        .order_by(OLTDevice.name, PonPort.name, OntUnit.serial_number)
        .limit(max(1, min(limit, 100)))
    )
    if exclude_ont_unit_id is not None:
        stmt = stmt.where(OntUnit.id != exclude_ont_unit_id)
    normalized_search = (search or "").strip()
    if normalized_search:
        like = f"%{normalized_search.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(OntUnit.serial_number).like(like),
                func.lower(func.coalesce(OntUnit.vendor_serial_number, "")).like(like),
                func.lower(func.coalesce(OntUnit.mac_address, "")).like(like),
                func.lower(func.coalesce(OLTDevice.name, "")).like(like),
            )
        )
    return tuple(
        OntInventoryChoice(
            ont_unit_id=ont.id,
            serial_number=ont.serial_number,
            mac_address=ont.mac_address,
            olt_id=olt.id,
            olt_name=olt.name,
            pon_port_id=pon.id,
            pon_port_name=pon.name,
        )
        for ont, pon, olt in db.execute(stmt).all()
    )
