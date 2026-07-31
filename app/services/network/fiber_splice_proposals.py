"""Reviewed splice-proposal intake shared by field and vendor capture.

One owner validates closure, tray, and exact strand-end identities, dedupes
against pending proposals, records the physical-link decision through the
canonical continuity owner, and files the reviewed change request. Field and
vendor transports stay thin adapters that supply a typed actor; review and
application remain with ``app.services.fiber_change_requests``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.fiber_change_request import (
    FiberChangeRequest,
    FiberChangeRequestOperation,
    FiberChangeRequestStatus,
)
from app.models.network import (
    FiberSplice,
    FiberSpliceClosure,
    FiberSpliceTray,
    FiberStrand,
    FiberStrandStatus,
)
from app.models.work_order import WorkOrder
from app.services import fiber_change_requests
from app.services.common import coerce_uuid
from app.services.domain_errors import DomainError
from app.services.network import fiber_physical_continuity
from app.services.network.fiber_color_code import (
    StrandColorCode,
    derive_segment_strand_colors,
)

_SPLICEABLE_STRAND_STATUSES = {
    FiberStrandStatus.available,
    FiberStrandStatus.reserved,
}


class SpliceProposalError(DomainError):
    """Stable splice-intake failures for transports to translate.

    ``kind`` names the failure class (``not_found`` | ``conflict`` |
    ``invalid``); transports map it to their own status vocabulary.
    """

    def __init__(self, *, code: str, message: str, kind: str) -> None:
        super().__init__(code=f"network.fiber_splice_proposals.{code}", message=message)
        self.kind = kind


def _not_found(code: str, message: str) -> SpliceProposalError:
    return SpliceProposalError(code=code, message=message, kind="not_found")


def _conflict(code: str, message: str) -> SpliceProposalError:
    return SpliceProposalError(code=code, message=message, kind="conflict")


def _invalid(code: str, message: str) -> SpliceProposalError:
    return SpliceProposalError(code=code, message=message, kind="invalid")


@dataclass(frozen=True)
class FieldTechnicianActor:
    """A Sub field technician capturing a splice inside their job scope."""

    technician_id: uuid.UUID
    person_id: uuid.UUID
    system_user_id: uuid.UUID | None


@dataclass(frozen=True)
class VendorActor:
    """A native vendor crew capturing a splice on their assigned project."""

    vendor_id: uuid.UUID
    vendor_user_id: uuid.UUID | None


SpliceProposalActor = FieldTechnicianActor | VendorActor


@dataclass(frozen=True)
class SpliceProposalReceipt:
    """Typed acknowledgement for a proposed splice change request."""

    change_request_id: uuid.UUID
    status: FiberChangeRequestStatus
    replayed: bool
    closure_id: uuid.UUID
    from_strand_id: uuid.UUID
    from_strand_end: str
    to_strand_id: uuid.UUID
    to_strand_end: str
    work_order_public_id: str | None
    from_strand_colors: StrandColorCode | None
    to_strand_colors: StrandColorCode | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "change_request_id": self.change_request_id,
            "status": self.status.value,
            "replayed": self.replayed,
            "closure_id": self.closure_id,
            "from_strand_id": self.from_strand_id,
            "from_strand_end": self.from_strand_end,
            "to_strand_id": self.to_strand_id,
            "to_strand_end": self.to_strand_end,
            "work_order_public_id": self.work_order_public_id,
            "from_strand_colors": self.from_strand_colors.to_dict()
            if self.from_strand_colors
            else None,
            "to_strand_colors": self.to_strand_colors.to_dict()
            if self.to_strand_colors
            else None,
        }


@dataclass(frozen=True)
class SpliceProposalStatus:
    """Review status of one proposed splice change request."""

    change_request_id: uuid.UUID
    status: FiberChangeRequestStatus
    operation: FiberChangeRequestOperation
    closure_id: uuid.UUID | None
    from_strand_id: uuid.UUID | None
    from_strand_end: str | None
    to_strand_id: uuid.UUID | None
    to_strand_end: str | None
    splice_type: str | None
    loss_db: float | None
    work_order_public_id: str | None
    from_strand_colors: StrandColorCode | None
    to_strand_colors: StrandColorCode | None
    review_notes: str | None
    reviewed_at: datetime | None
    applied_at: datetime | None
    created_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "change_request_id": self.change_request_id,
            "status": self.status.value,
            "operation": self.operation.value,
            "closure_id": self.closure_id,
            "from_strand_id": self.from_strand_id,
            "from_strand_end": self.from_strand_end,
            "to_strand_id": self.to_strand_id,
            "to_strand_end": self.to_strand_end,
            "splice_type": self.splice_type,
            "loss_db": self.loss_db,
            "work_order_public_id": self.work_order_public_id,
            "from_strand_colors": self.from_strand_colors.to_dict()
            if self.from_strand_colors
            else None,
            "to_strand_colors": self.to_strand_colors.to_dict()
            if self.to_strand_colors
            else None,
            "review_notes": self.review_notes,
            "reviewed_at": self.reviewed_at,
            "applied_at": self.applied_at,
            "created_at": self.created_at,
        }


def _uuid_or_invalid(value: str | None, field_name: str) -> uuid.UUID:
    try:
        return coerce_uuid(str(value))
    except ValueError as exc:
        raise _invalid("invalid_identifier", f"{field_name} must be a UUID") from exc


def _strand_end_or_invalid(value: str, field_name: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in {"a", "b"}:
        raise _invalid("invalid_strand_end", f"{field_name} must be a or b")
    return normalized


def _uuid_or_none(value: Any) -> uuid.UUID | None:
    if value in (None, ""):
        return None
    try:
        return coerce_uuid(str(value))
    except ValueError:
        return None


def _load_spliceable_strand(db: Session, strand_id, label: str) -> FiberStrand:
    strand = db.get(FiberStrand, strand_id)
    if strand is None or not strand.is_active:
        raise _not_found("strand_not_found", f"{label} strand not found")
    if strand.status not in _SPLICEABLE_STRAND_STATUSES:
        raise _invalid(
            "strand_not_spliceable",
            (
                f"{label} strand is {strand.status.value}; only available or "
                "reserved strands can be spliced"
            ),
        )
    return strand


def pending_splice_requests(db: Session) -> list[FiberChangeRequest]:
    return (
        db.query(FiberChangeRequest)
        .filter(FiberChangeRequest.asset_type == "fiber_splice")
        .filter(FiberChangeRequest.status == FiberChangeRequestStatus.pending)
        .all()
    )


def proposal_receipt(
    request: FiberChangeRequest, *, replayed: bool
) -> SpliceProposalReceipt:
    payload = request.payload or {}
    return SpliceProposalReceipt(
        change_request_id=request.id,
        status=request.status,
        replayed=replayed,
        closure_id=coerce_uuid(str(payload["closure_id"])),
        from_strand_id=coerce_uuid(str(payload["from_strand_id"])),
        from_strand_end=str(payload["from_strand_end"]),
        to_strand_id=coerce_uuid(str(payload["to_strand_id"])),
        to_strand_end=str(payload["to_strand_end"]),
        work_order_public_id=payload.get("work_order_public_id"),
        from_strand_colors=StrandColorCode.from_payload(
            payload.get("from_strand_colors")
        ),
        to_strand_colors=StrandColorCode.from_payload(payload.get("to_strand_colors")),
    )


def proposal_status(request: FiberChangeRequest) -> SpliceProposalStatus:
    payload = request.payload or {}
    loss_db = payload.get("loss_db")
    return SpliceProposalStatus(
        change_request_id=request.id,
        status=request.status,
        operation=request.operation,
        closure_id=_uuid_or_none(payload.get("closure_id")),
        from_strand_id=_uuid_or_none(payload.get("from_strand_id")),
        from_strand_end=payload.get("from_strand_end"),
        to_strand_id=_uuid_or_none(payload.get("to_strand_id")),
        to_strand_end=payload.get("to_strand_end"),
        splice_type=payload.get("splice_type"),
        loss_db=float(loss_db) if loss_db is not None else None,
        work_order_public_id=payload.get("work_order_public_id"),
        from_strand_colors=StrandColorCode.from_payload(
            payload.get("from_strand_colors")
        ),
        to_strand_colors=StrandColorCode.from_payload(payload.get("to_strand_colors")),
        review_notes=request.review_notes,
        reviewed_at=request.reviewed_at,
        applied_at=request.applied_at,
        created_at=request.created_at,
    )


def _actor_provenance(actor: SpliceProposalActor) -> tuple[str, str, dict[str, Any]]:
    """Resolve proposer label, default reason, and typed payload identity."""
    if isinstance(actor, FieldTechnicianActor):
        return (
            f"field-technician:{actor.technician_id}",
            "Field-captured exact fiber core splice",
            {
                "field_actor": {
                    "technician_id": str(actor.technician_id),
                    "person_id": str(actor.person_id),
                    "system_user_id": str(actor.system_user_id)
                    if actor.system_user_id
                    else None,
                }
            },
        )
    return (
        f"vendor:{actor.vendor_id}",
        "Vendor-captured exact fiber core splice",
        {
            "vendor_actor": {
                "vendor_id": str(actor.vendor_id),
                "vendor_user_id": str(actor.vendor_user_id)
                if actor.vendor_user_id
                else None,
            }
        },
    )


def propose_splice(
    db: Session,
    *,
    actor: SpliceProposalActor,
    closure_id: str,
    from_strand_id: str,
    from_strand_end: str,
    to_strand_id: str,
    to_strand_end: str,
    tray_id: str | None = None,
    position: int | None = None,
    splice_type: str | None = None,
    loss_db: float | None = None,
    note: str | None = None,
    work_order: WorkOrder | None = None,
) -> SpliceProposalReceipt:
    """Validate and file one reviewed splice proposal for the given actor."""

    closure_uuid = _uuid_or_invalid(closure_id, "closure_id")
    from_uuid = _uuid_or_invalid(from_strand_id, "from_strand_id")
    to_uuid = _uuid_or_invalid(to_strand_id, "to_strand_id")
    from_end = _strand_end_or_invalid(from_strand_end, "from_strand_end")
    to_end = _strand_end_or_invalid(to_strand_end, "to_strand_end")
    if from_uuid == to_uuid:
        raise _invalid("self_splice", "A strand cannot be spliced to itself")

    closure = db.get(FiberSpliceClosure, closure_uuid)
    if closure is None or not closure.is_active:
        raise _not_found("closure_not_found", "Splice closure not found")

    from_strand = _load_spliceable_strand(db, from_uuid, "from")
    to_strand = _load_spliceable_strand(db, to_uuid, "to")

    tray_uuid = _uuid_or_invalid(tray_id, "tray_id") if tray_id else None
    if tray_uuid is not None:
        tray = db.get(FiberSpliceTray, tray_uuid)
        if tray is None:
            raise _not_found("tray_not_found", "Splice tray not found")
        if tray.closure_id != closure.id:
            raise _invalid(
                "tray_closure_mismatch",
                "Splice tray does not belong to this closure",
            )
        if position is not None:
            occupied = (
                db.query(FiberSplice)
                .filter(FiberSplice.tray_id == tray.id)
                .filter(FiberSplice.position == position)
                .first()
            )
            if occupied is not None:
                raise _conflict(
                    "tray_position_occupied",
                    "That tray position is already occupied",
                )

    existing = (
        db.query(FiberSplice)
        .filter(
            or_(
                (FiberSplice.from_strand_id == from_uuid)
                & (FiberSplice.to_strand_id == to_uuid),
                (FiberSplice.from_strand_id == to_uuid)
                & (FiberSplice.to_strand_id == from_uuid),
            )
        )
        .first()
    )
    if existing is not None:
        raise _conflict(
            "splice_exists", "A splice between these strands already exists"
        )

    pair = {(str(from_uuid), from_end), (str(to_uuid), to_end)}
    for request in pending_splice_requests(db):
        payload = request.payload or {}
        if {
            (str(payload.get("from_strand_id")), payload.get("from_strand_end")),
            (str(payload.get("to_strand_id")), payload.get("to_strand_end")),
        } == pair:
            return proposal_receipt(request, replayed=True)

    proposed_by, default_reason, actor_payload = _actor_provenance(actor)
    try:
        decision = fiber_physical_continuity.propose_physical_link(
            db,
            "core_splice",
            "connect",
            proposed_by=proposed_by,
            reason=note or default_reason,
            first_strand_id=from_uuid,
            first_strand_end=from_end,
            second_strand_id=to_uuid,
            second_strand_end=to_end,
            splice_closure_id=closure.id,
            splice_tray_id=tray_uuid,
            position=position,
            splice_type=splice_type,
            insertion_loss_db=loss_db,
        )
    except fiber_physical_continuity.FiberPhysicalContinuityError as exc:
        raise _invalid("continuity_rejected", str(exc)) from exc

    from_colors = derive_segment_strand_colors(
        from_strand.segment, from_strand.strand_number
    )
    to_colors = derive_segment_strand_colors(to_strand.segment, to_strand.strand_number)
    payload = {
        "closure_id": str(closure.id),
        "from_strand_id": str(from_uuid),
        "from_strand_end": from_end,
        "to_strand_id": str(to_uuid),
        "to_strand_end": to_end,
        "tray_id": str(tray_uuid) if tray_uuid else None,
        "position": position,
        "splice_type": splice_type,
        "loss_db": loss_db,
        "notes": note,
        "work_order_id": str(work_order.id) if work_order else None,
        "work_order_public_id": work_order.public_id if work_order else None,
        "from_strand_colors": from_colors.to_dict() if from_colors else None,
        "to_strand_colors": to_colors.to_dict() if to_colors else None,
        "physical_link_decision_id": str(decision.id),
        **actor_payload,
    }
    request = fiber_change_requests.create_request(
        db,
        asset_type="fiber_splice",
        asset_id=str(decision.id),
        operation=FiberChangeRequestOperation.create,
        payload=payload,
        requested_by_person_id=None,
        requested_by_vendor_id=str(actor.vendor_id)
        if isinstance(actor, VendorActor)
        else None,
    )
    return proposal_receipt(request, replayed=False)
