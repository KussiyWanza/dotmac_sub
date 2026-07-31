"""Per-job fiber evidence summary: one read-only view of what was recorded.

Aggregates the fiber evidence naming one native work order — tests (with
derived-verdict failures and assertion conflicts), topology source
observations, splice proposals by review status, the live cut sheet's
progress, field attachments, and pending inventory proposals — so a
technician, vendor, or reviewer sees in one place what exists and what is
missing. Strictly read-only: every fact belongs to its named owner; this
projection only counts and labels."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.models.fiber_change_request import (
    FiberChangeRequest,
    FiberChangeRequestStatus,
)
from app.models.fiber_splice_plan import FiberSplicePlanStatus
from app.models.fiber_topology_field_observation import FiberTopologyFieldObservation
from app.models.field_attachment import FieldAttachment
from app.models.field_fiber import FieldFiberTestResult
from app.models.work_order import WorkOrder
from app.services.network import fiber_splice_plans


@dataclass(frozen=True)
class SpliceProposalCounts:
    """This work order's splice change requests by review status."""

    pending: int
    applied: int
    rejected: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "pending": self.pending,
            "applied": self.applied,
            "rejected": self.rejected,
        }


@dataclass(frozen=True)
class JobPlanSummary:
    """The live cut sheet's derived progress for this work order."""

    plan_id: uuid.UUID
    status: FiberSplicePlanStatus
    item_count: int
    executed_count: int
    unexecuted_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "status": self.status.value,
            "item_count": self.item_count,
            "executed_count": self.executed_count,
            "unexecuted_count": self.unexecuted_count,
        }


@dataclass(frozen=True)
class FiberJobEvidenceSummary:
    """Everything recorded against one work order, counted and labelled."""

    work_order_id: uuid.UUID
    work_order_public_id: str
    fiber_test_count: int
    derived_failed_count: int
    assertion_conflict_count: int
    source_observation_count: int
    splice_proposals: SpliceProposalCounts
    unplanned_splice_count: int
    plan: JobPlanSummary | None
    attachment_count: int
    pending_inventory_proposals: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "work_order_id": self.work_order_id,
            "work_order_public_id": self.work_order_public_id,
            "fiber_test_count": self.fiber_test_count,
            "derived_failed_count": self.derived_failed_count,
            "assertion_conflict_count": self.assertion_conflict_count,
            "source_observation_count": self.source_observation_count,
            "splice_proposals": self.splice_proposals.to_dict(),
            "unplanned_splice_count": self.unplanned_splice_count,
            "plan": self.plan.to_dict() if self.plan else None,
            "attachment_count": self.attachment_count,
            "pending_inventory_proposals": self.pending_inventory_proposals,
        }


def _splice_counts(
    db: Session, work_order: WorkOrder
) -> tuple[SpliceProposalCounts, int]:
    rows = (
        db.query(FiberChangeRequest)
        .filter(FiberChangeRequest.asset_type == "fiber_splice")
        .filter(
            FiberChangeRequest.payload["work_order_id"].as_string()
            == str(work_order.id)
        )
        .all()
    )
    pending = sum(1 for row in rows if row.status == FiberChangeRequestStatus.pending)
    applied = sum(1 for row in rows if row.status == FiberChangeRequestStatus.applied)
    rejected = sum(1 for row in rows if row.status == FiberChangeRequestStatus.rejected)
    unplanned = sum(
        1
        for row in rows
        if row.status != FiberChangeRequestStatus.rejected
        and not (row.payload or {}).get("plan_item_id")
    )
    return SpliceProposalCounts(pending, applied, rejected), unplanned


def _pending_inventory_count(db: Session, work_order: WorkOrder) -> int:
    rows = (
        db.query(FiberChangeRequest)
        .filter(FiberChangeRequest.asset_type.in_(("fiber_segment", "fiber_strand")))
        .filter(FiberChangeRequest.status == FiberChangeRequestStatus.pending)
        .all()
    )
    return sum(
        1
        for row in rows
        if ((row.payload or {}).get("provenance") or {}).get("work_order_id")
        == str(work_order.id)
    )


def summarize(db: Session, work_order: WorkOrder) -> FiberJobEvidenceSummary:
    """Count every piece of fiber evidence naming this exact work order."""

    tests = (
        db.query(FieldFiberTestResult)
        .filter(FieldFiberTestResult.work_order_mirror_id == work_order.id)
        .all()
    )
    observation_count = (
        db.query(FiberTopologyFieldObservation)
        .filter(FiberTopologyFieldObservation.work_order_id == work_order.id)
        .count()
    )
    attachment_count = (
        db.query(FieldAttachment)
        .filter(FieldAttachment.work_order_mirror_id == work_order.id)
        .filter(FieldAttachment.is_active.is_(True))
        .count()
    )
    splice_counts, unplanned = _splice_counts(db, work_order)

    plan_view = fiber_splice_plans.view_for_work_order(db, work_order.id)
    plan = (
        JobPlanSummary(
            plan_id=plan_view.plan_id,
            status=plan_view.status,
            item_count=len(plan_view.items),
            executed_count=plan_view.executed_count,
            unexecuted_count=plan_view.unexecuted_count,
        )
        if plan_view is not None
        else None
    )

    return FiberJobEvidenceSummary(
        work_order_id=work_order.id,
        work_order_public_id=work_order.public_id,
        fiber_test_count=len(tests),
        derived_failed_count=sum(1 for test in tests if test.derived_passed is False),
        assertion_conflict_count=sum(1 for test in tests if test.assertion_conflict),
        source_observation_count=observation_count,
        splice_proposals=splice_counts,
        unplanned_splice_count=unplanned,
        plan=plan,
        attachment_count=attachment_count,
        pending_inventory_proposals=_pending_inventory_count(db, work_order),
    )
