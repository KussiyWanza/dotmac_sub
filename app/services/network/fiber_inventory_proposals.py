"""Reviewed field/vendor inventory intake: cable registration and damage.

Field crews and vendors can register the cable they hung and report the
damage they found — as reviewed change requests, never direct mutations.
Review and application stay with ``app.services.fiber_change_requests``
(``network.fiber_asset_changes``); this intake only validates identities,
builds typed payloads with a retained provenance section, and files the
requests. Registered cables enter inactive: activation remains with the
reviewed connectivity flow.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.models.fiber_change_request import (
    FiberChangeRequest,
    FiberChangeRequestOperation,
    FiberChangeRequestStatus,
)
from app.models.network import (
    FiberCableType,
    FiberColorStandard,
    FiberSegment,
    FiberSegmentType,
    FiberStrand,
    FiberStrandStatus,
)
from app.models.work_order import WorkOrder
from app.services import fiber_change_requests
from app.services.common import coerce_uuid
from app.services.domain_errors import DomainError
from app.services.network.fiber_splice_proposals import (
    FieldTechnicianActor,
    SpliceProposalActor,
    VendorActor,
)

_MAX_TUBE_SCOPE_STRANDS = 24


class FiberInventoryProposalError(DomainError):
    """Stable inventory-intake failures for transports to translate."""

    def __init__(self, *, code: str, message: str, kind: str) -> None:
        super().__init__(
            code=f"network.fiber_inventory_proposals.{code}", message=message
        )
        self.kind = kind


def _not_found(code: str, message: str) -> FiberInventoryProposalError:
    return FiberInventoryProposalError(code=code, message=message, kind="not_found")


def _conflict(code: str, message: str) -> FiberInventoryProposalError:
    return FiberInventoryProposalError(code=code, message=message, kind="conflict")


def _invalid(code: str, message: str) -> FiberInventoryProposalError:
    return FiberInventoryProposalError(code=code, message=message, kind="invalid")


def _provenance(
    actor: SpliceProposalActor, work_order: WorkOrder | None
) -> dict[str, Any]:
    if isinstance(actor, FieldTechnicianActor):
        identity: dict[str, Any] = {
            "kind": "field_technician",
            "technician_id": str(actor.technician_id),
            "person_id": str(actor.person_id),
            "system_user_id": str(actor.system_user_id)
            if actor.system_user_id
            else None,
        }
    else:
        identity = {
            "kind": "vendor",
            "vendor_id": str(actor.vendor_id),
            "vendor_user_id": str(actor.vendor_user_id)
            if actor.vendor_user_id
            else None,
        }
    return {
        **identity,
        "work_order_id": str(work_order.id) if work_order else None,
        "work_order_public_id": work_order.public_id if work_order else None,
    }


def _vendor_id_or_none(actor: SpliceProposalActor) -> str | None:
    return str(actor.vendor_id) if isinstance(actor, VendorActor) else None


@dataclass(frozen=True)
class CableRegistrationReceipt:
    """Typed acknowledgement for a proposed cable registration."""

    change_request_id: uuid.UUID
    status: FiberChangeRequestStatus
    name: str
    fiber_count: int
    work_order_public_id: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "change_request_id": self.change_request_id,
            "status": self.status.value,
            "name": self.name,
            "fiber_count": self.fiber_count,
            "work_order_public_id": self.work_order_public_id,
        }


@dataclass(frozen=True)
class StrandDamageReceipt:
    """Typed acknowledgement for proposed strand damage records."""

    change_request_ids: tuple[uuid.UUID, ...]
    strand_ids: tuple[uuid.UUID, ...]
    tube_number: int | None
    work_order_public_id: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "change_request_ids": list(self.change_request_ids),
            "strand_ids": list(self.strand_ids),
            "tube_number": self.tube_number,
            "work_order_public_id": self.work_order_public_id,
        }


def register_cable(
    db: Session,
    *,
    actor: SpliceProposalActor,
    name: str,
    fiber_count: int,
    segment_type: str | None = None,
    cable_type: str | None = None,
    fibers_per_tube: int | None = None,
    color_standard: str | None = None,
    length_m: float | None = None,
    notes: str | None = None,
    work_order: WorkOrder | None = None,
) -> CableRegistrationReceipt:
    """File one reviewed registration for a newly built (inactive) cable."""

    cleaned_name = (name or "").strip()
    if not cleaned_name:
        raise _invalid("name_required", "A cable name is required")
    if fiber_count < 1:
        raise _invalid("invalid_fiber_count", "fiber_count must be positive")
    if fibers_per_tube is not None and fibers_per_tube < 1:
        raise _invalid("invalid_fibers_per_tube", "fibers_per_tube must be positive")
    if segment_type is not None and segment_type not in {
        item.value for item in FiberSegmentType
    }:
        raise _invalid("invalid_segment_type", "Unknown segment_type")
    if cable_type is not None and cable_type not in {
        item.value for item in FiberCableType
    }:
        raise _invalid("invalid_cable_type", "Unknown cable_type")
    if color_standard is not None and color_standard not in {
        item.value for item in FiberColorStandard
    }:
        raise _invalid("invalid_color_standard", "Unknown color_standard")
    existing = (
        db.query(FiberSegment).filter(FiberSegment.name == cleaned_name).one_or_none()
    )
    if existing is not None:
        raise _conflict("cable_name_exists", "A cable with this name already exists")
    pending_same_name = any(
        (row.payload or {}).get("name") == cleaned_name
        for row in db.query(FiberChangeRequest)
        .filter(FiberChangeRequest.asset_type == "fiber_segment")
        .filter(FiberChangeRequest.status == FiberChangeRequestStatus.pending)
        .filter(FiberChangeRequest.operation == FiberChangeRequestOperation.create)
        .all()
    )
    if pending_same_name:
        raise _conflict(
            "cable_registration_pending",
            "A registration for this cable name is already awaiting review",
        )

    payload: dict[str, Any] = {
        "name": cleaned_name,
        "fiber_count": fiber_count,
        "is_active": False,
        "provenance": _provenance(actor, work_order),
    }
    if segment_type is not None:
        payload["segment_type"] = segment_type
    if cable_type is not None:
        payload["cable_type"] = cable_type
    if fibers_per_tube is not None:
        payload["fibers_per_tube"] = fibers_per_tube
    if color_standard is not None:
        payload["color_standard"] = color_standard
    if length_m is not None:
        payload["length_m"] = length_m
    if notes:
        payload["notes"] = notes

    request = fiber_change_requests.create_request(
        db,
        asset_type="fiber_segment",
        asset_id=None,
        operation=FiberChangeRequestOperation.create,
        payload=payload,
        requested_by_person_id=None,
        requested_by_vendor_id=_vendor_id_or_none(actor),
    )
    return CableRegistrationReceipt(
        change_request_id=request.id,
        status=request.status,
        name=cleaned_name,
        fiber_count=fiber_count,
        work_order_public_id=work_order.public_id if work_order else None,
    )


def _tube_strand_numbers(segment: FiberSegment, tube_number: int) -> list[int]:
    if segment.fibers_per_tube is None or segment.fibers_per_tube < 1:
        raise _invalid(
            "tube_construction_undeclared",
            "The cable does not declare fibers_per_tube; report exact strands",
        )
    if tube_number < 1:
        raise _invalid("invalid_tube_number", "tube_number must be positive")
    start = (tube_number - 1) * segment.fibers_per_tube + 1
    end = tube_number * segment.fibers_per_tube
    if segment.fiber_count is not None and start > segment.fiber_count:
        raise _invalid(
            "tube_out_of_range",
            "The tube number is outside the cable's declared core count",
        )
    return list(range(start, end + 1))


def report_strand_damage(
    db: Session,
    *,
    actor: SpliceProposalActor,
    note: str,
    strand_id: str | None = None,
    segment_id: str | None = None,
    tube_number: int | None = None,
    work_order: WorkOrder | None = None,
) -> StrandDamageReceipt:
    """File reviewed damage records for one strand or one derived tube."""

    cleaned_note = (note or "").strip()
    if not cleaned_note:
        raise _invalid("note_required", "A damage description is required")
    if bool(strand_id) == bool(segment_id):
        raise _invalid(
            "invalid_scope",
            "Report either one exact strand or one segment tube, not both",
        )

    strands: list[FiberStrand]
    if strand_id:
        try:
            strand_uuid = coerce_uuid(str(strand_id))
        except ValueError as exc:
            raise _invalid("invalid_identifier", "strand_id must be a UUID") from exc
        strand = db.get(FiberStrand, strand_uuid)
        if strand is None or not strand.is_active:
            raise _not_found("strand_not_found", "Strand not found")
        strands = [strand]
    else:
        if tube_number is None:
            raise _invalid(
                "tube_number_required",
                "tube_number is required for a segment-scoped report",
            )
        try:
            segment_uuid = coerce_uuid(str(segment_id))
        except ValueError as exc:
            raise _invalid("invalid_identifier", "segment_id must be a UUID") from exc
        segment = db.get(FiberSegment, segment_uuid)
        if segment is None:
            raise _not_found("segment_not_found", "Cable segment not found")
        numbers = _tube_strand_numbers(segment, tube_number)
        if len(numbers) > _MAX_TUBE_SCOPE_STRANDS:
            raise _invalid(
                "tube_scope_too_large",
                "The tube covers more strands than one report may carry",
            )
        strands = (
            db.query(FiberStrand)
            .filter(FiberStrand.segment_id == segment.id)
            .filter(FiberStrand.strand_number.in_(numbers))
            .filter(FiberStrand.is_active.is_(True))
            .order_by(FiberStrand.strand_number.asc())
            .all()
        )
        if not strands:
            raise _not_found(
                "tube_strands_not_found",
                "No exact strands are inventoried for that tube",
            )

    already_damaged = [
        strand for strand in strands if strand.status == FiberStrandStatus.damaged
    ]
    targets = [strand for strand in strands if strand not in already_damaged]
    if not targets:
        raise _conflict(
            "already_damaged", "Every strand in scope is already marked damaged"
        )

    provenance = _provenance(actor, work_order)
    request_ids: list[uuid.UUID] = []
    for strand in targets:
        request = fiber_change_requests.create_request(
            db,
            asset_type="fiber_strand",
            asset_id=str(strand.id),
            operation=FiberChangeRequestOperation.update,
            payload={
                "status": FiberStrandStatus.damaged.value,
                "notes": cleaned_note,
                "provenance": provenance,
            },
            requested_by_person_id=None,
            requested_by_vendor_id=_vendor_id_or_none(actor),
        )
        request_ids.append(request.id)

    return StrandDamageReceipt(
        change_request_ids=tuple(request_ids),
        strand_ids=tuple(strand.id for strand in targets),
        tube_number=tube_number,
        work_order_public_id=work_order.public_id if work_order else None,
    )


def pending_inventory_requests(db: Session) -> list[FiberChangeRequest]:
    """Pending field/vendor inventory proposals (for review worklists)."""

    return (
        db.query(FiberChangeRequest)
        .filter(FiberChangeRequest.asset_type.in_(("fiber_segment", "fiber_strand")))
        .filter(FiberChangeRequest.status == FiberChangeRequestStatus.pending)
        .all()
    )
