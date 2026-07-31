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
from app.services.network import fiber_splice_proposals
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
    )


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
