"""Vendor-side fiber splice capture on assigned installation projects.

Thin adapter over the shared splice-proposal owner
(``app.services.network.fiber_splice_proposals``). A vendor crew records the
splices it built as reviewed change requests; Sub review and application stay
with ``app.services.fiber_change_requests``. Scope is decidable: the named
work order must belong to a native project whose installation project is
actively assigned to this vendor.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.models.fiber_change_request import FiberChangeRequest
from app.models.vendor_routes import InstallationProject
from app.models.work_order import WorkOrder
from app.services.common import coerce_uuid
from app.services.network import (
    fiber_inventory_proposals,
    fiber_job_evidence,
    fiber_splice_plans,
    fiber_splice_proposals,
)
from app.services.network.fiber_inventory_proposals import (
    CableRegistrationReceipt,
    StrandDamageReceipt,
)
from app.services.network.fiber_splice_proposals import (
    SpliceProposalError,
    SpliceProposalReceipt,
    SpliceProposalStatus,
    VendorActor,
)

_MAX_PROPOSAL_ROWS = 200


def _scoped_vendor_work_order(
    db: Session, vendor_uuid: uuid.UUID, work_order_public_id: str
) -> WorkOrder:
    row = (
        db.query(WorkOrder)
        .filter(WorkOrder.public_id == work_order_public_id)
        .filter(WorkOrder.is_active.is_(True))
        .one_or_none()
    )
    if row is None or row.project_id is None:
        raise SpliceProposalError(
            code="work_order_not_found",
            message="Work order not found",
            kind="not_found",
        )
    assignment = (
        db.query(InstallationProject)
        .filter(InstallationProject.project_id == row.project_id)
        .filter(InstallationProject.assigned_vendor_id == vendor_uuid)
        .filter(InstallationProject.is_active.is_(True))
        .first()
    )
    if assignment is None:
        raise SpliceProposalError(
            code="work_order_not_found",
            message="Work order not found",
            kind="not_found",
        )
    return row


def propose_splice(
    db: Session,
    *,
    vendor_id: str,
    vendor_user_id: str | None,
    work_order_id: str,
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
    plan_item_id: str | None = None,
) -> SpliceProposalReceipt:
    """File one reviewed splice proposal for the vendor's assigned project."""

    vendor_uuid = coerce_uuid(vendor_id)
    work_order = _scoped_vendor_work_order(db, vendor_uuid, work_order_id)
    actor = VendorActor(
        vendor_id=vendor_uuid,
        vendor_user_id=coerce_uuid(vendor_user_id) if vendor_user_id else None,
    )
    return fiber_splice_proposals.propose_splice(
        db,
        actor=actor,
        closure_id=closure_id,
        from_strand_id=from_strand_id,
        from_strand_end=from_strand_end,
        to_strand_id=to_strand_id,
        to_strand_end=to_strand_end,
        tray_id=tray_id,
        position=position,
        splice_type=splice_type,
        loss_db=loss_db,
        note=note,
        work_order=work_order,
        plan_item_id=plan_item_id,
    )


def get_splice_plan(
    db: Session,
    *,
    vendor_id: str,
    work_order_id: str,
) -> dict:
    """Project the assigned work order's live cut sheet for the vendor crew."""

    vendor_uuid = coerce_uuid(vendor_id)
    work_order = _scoped_vendor_work_order(db, vendor_uuid, work_order_id)
    view = fiber_splice_plans.view_for_work_order(db, work_order.id)
    diff = fiber_splice_plans.diff_for_work_order(db, work_order.id)
    return {
        "work_order_id": work_order.public_id,
        "plan": view.to_dict() if view else None,
        "diff": diff.to_dict() if diff else None,
    }


def register_cable(
    db: Session,
    *,
    vendor_id: str,
    vendor_user_id: str | None,
    work_order_id: str,
    name: str,
    fiber_count: int,
    segment_type: str | None = None,
    cable_type: str | None = None,
    fibers_per_tube: int | None = None,
    color_standard: str | None = None,
    length_m: float | None = None,
    notes: str | None = None,
) -> CableRegistrationReceipt:
    """Register cable the vendor built as a reviewed (inactive) change request."""

    vendor_uuid = coerce_uuid(vendor_id)
    work_order = _scoped_vendor_work_order(db, vendor_uuid, work_order_id)
    actor = VendorActor(
        vendor_id=vendor_uuid,
        vendor_user_id=coerce_uuid(vendor_user_id) if vendor_user_id else None,
    )
    return fiber_inventory_proposals.register_cable(
        db,
        actor=actor,
        name=name,
        fiber_count=fiber_count,
        segment_type=segment_type,
        cable_type=cable_type,
        fibers_per_tube=fibers_per_tube,
        color_standard=color_standard,
        length_m=length_m,
        notes=notes,
        work_order=work_order,
    )


def report_strand_damage(
    db: Session,
    *,
    vendor_id: str,
    vendor_user_id: str | None,
    work_order_id: str,
    note: str,
    strand_id: str | None = None,
    segment_id: str | None = None,
    tube_number: int | None = None,
) -> StrandDamageReceipt:
    """Report strand or tube damage found on the assigned project."""

    vendor_uuid = coerce_uuid(vendor_id)
    work_order = _scoped_vendor_work_order(db, vendor_uuid, work_order_id)
    actor = VendorActor(
        vendor_id=vendor_uuid,
        vendor_user_id=coerce_uuid(vendor_user_id) if vendor_user_id else None,
    )
    return fiber_inventory_proposals.report_strand_damage(
        db,
        actor=actor,
        note=note,
        strand_id=strand_id,
        segment_id=segment_id,
        tube_number=tube_number,
        work_order=work_order,
    )


def get_job_evidence(
    db: Session,
    *,
    vendor_id: str,
    work_order_id: str,
) -> dict:
    """One scoped view of every piece of fiber evidence on the vendor's job."""

    from app.services.field.transitions import resolve_fiber_as_built_evidence

    vendor_uuid = coerce_uuid(vendor_id)
    work_order = _scoped_vendor_work_order(db, vendor_uuid, work_order_id)
    summary = fiber_job_evidence.summarize(db, work_order)
    evidence = resolve_fiber_as_built_evidence(db, work_order)
    payload = summary.to_dict()
    payload["as_built_required"] = evidence.required
    payload["as_built_satisfied"] = evidence.satisfied
    return payload


def list_splice_proposals(
    db: Session,
    *,
    vendor_id: str,
    limit: int = 50,
) -> list[SpliceProposalStatus]:
    """List this vendor's own splice change requests, newest first."""

    vendor_uuid = coerce_uuid(vendor_id)
    rows = (
        db.query(FiberChangeRequest)
        .filter(FiberChangeRequest.asset_type == "fiber_splice")
        .filter(FiberChangeRequest.requested_by_vendor_id == vendor_uuid)
        .order_by(FiberChangeRequest.created_at.desc())
        .limit(max(1, min(limit, _MAX_PROPOSAL_ROWS)))
        .all()
    )
    return [fiber_splice_proposals.proposal_status(row) for row in rows]
