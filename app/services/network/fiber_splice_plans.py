"""Planned splice work (cut sheets): the design-first owner for splicing.

This owner decides what *should* be spliced: draft → issued → cancelled plans
of exact strand-end pairs bound to one native work order. Execution stays with
the reviewed splice intake (``network.fiber_splice_proposals``) and review
with ``fiber_change_requests``; a plan item records only the link to the
change request that executed it, so plan progress is derived from review
state and cannot drift on its own. At most one live (non-cancelled) plan
exists per work order.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.fiber_change_request import (
    FiberChangeRequest,
    FiberChangeRequestStatus,
)
from app.models.fiber_splice_plan import (
    FiberSplicePlan,
    FiberSplicePlanItem,
    FiberSplicePlanStatus,
)
from app.models.network import (
    FiberSpliceClosure,
    FiberSpliceTray,
    FiberStrand,
    FiberStrandStatus,
)
from app.models.work_order import WorkOrder
from app.services.common import coerce_uuid
from app.services.domain_errors import DomainError
from app.services.events import EventType, emit_event
from app.services.network.fiber_color_code import (
    StrandColorCode,
    derive_segment_strand_colors,
)
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

_PLANNABLE_STRAND_STATUSES = {
    FiberStrandStatus.available,
    FiberStrandStatus.reserved,
}
_MAX_ITEMS_PER_PLAN = 500

_LIFECYCLE_CONCERN = "planned splice work (cut sheet) lifecycle"
_EXECUTION_CONCERN = "planned splice execution linkage"


def _definition(concern: str, name: str) -> OwnerCommandDefinition:
    return OwnerCommandDefinition(
        owner="network.fiber_splice_plans",
        concern=concern,
        name=name,
    )


class SplicePlanError(DomainError):
    """Stable splice-plan failures for transports to translate."""

    def __init__(self, *, code: str, message: str, kind: str) -> None:
        super().__init__(code=f"network.fiber_splice_plans.{code}", message=message)
        self.kind = kind


def _not_found(code: str, message: str) -> SplicePlanError:
    return SplicePlanError(code=code, message=message, kind="not_found")


def _conflict(code: str, message: str) -> SplicePlanError:
    return SplicePlanError(code=code, message=message, kind="conflict")


def _invalid(code: str, message: str) -> SplicePlanError:
    return SplicePlanError(code=code, message=message, kind="invalid")


@dataclass(frozen=True)
class SplicePlanItemExecution:
    """Review state of the change request that executed one plan item."""

    change_request_id: uuid.UUID
    status: FiberChangeRequestStatus

    def to_dict(self) -> dict[str, Any]:
        return {
            "change_request_id": self.change_request_id,
            "status": self.status.value,
        }


@dataclass(frozen=True)
class SplicePlanItemView:
    """One planned splice with its derived colors and execution state."""

    item_id: uuid.UUID
    position_index: int
    closure_id: uuid.UUID
    tray_id: uuid.UUID | None
    tray_position: int | None
    from_strand_id: uuid.UUID
    from_strand_end: str
    to_strand_id: uuid.UUID
    to_strand_end: str
    splice_type: str
    expected_loss_db: float | None
    notes: str | None
    from_strand_colors: StrandColorCode | None
    to_strand_colors: StrandColorCode | None
    execution: SplicePlanItemExecution | None

    @property
    def executed(self) -> bool:
        """Executed means linked to a request that was not rejected."""
        return (
            self.execution is not None
            and self.execution.status != FiberChangeRequestStatus.rejected
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "position_index": self.position_index,
            "closure_id": self.closure_id,
            "tray_id": self.tray_id,
            "tray_position": self.tray_position,
            "from_strand_id": self.from_strand_id,
            "from_strand_end": self.from_strand_end,
            "to_strand_id": self.to_strand_id,
            "to_strand_end": self.to_strand_end,
            "splice_type": self.splice_type,
            "expected_loss_db": self.expected_loss_db,
            "notes": self.notes,
            "from_strand_colors": self.from_strand_colors.to_dict()
            if self.from_strand_colors
            else None,
            "to_strand_colors": self.to_strand_colors.to_dict()
            if self.to_strand_colors
            else None,
            "execution": self.execution.to_dict() if self.execution else None,
            "executed": self.executed,
        }


@dataclass(frozen=True)
class SplicePlanView:
    """One cut sheet with derived progress."""

    plan_id: uuid.UUID
    work_order_id: uuid.UUID
    work_order_public_id: str
    name: str
    status: FiberSplicePlanStatus
    notes: str | None
    issued_at: datetime | None
    cancelled_at: datetime | None
    items: tuple[SplicePlanItemView, ...]

    @property
    def executed_count(self) -> int:
        return sum(1 for item in self.items if item.executed)

    @property
    def unexecuted_count(self) -> int:
        return len(self.items) - self.executed_count

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "work_order_id": self.work_order_id,
            "work_order_public_id": self.work_order_public_id,
            "name": self.name,
            "status": self.status.value,
            "notes": self.notes,
            "issued_at": self.issued_at,
            "cancelled_at": self.cancelled_at,
            "item_count": len(self.items),
            "executed_count": self.executed_count,
            "unexecuted_count": self.unexecuted_count,
            "items": [item.to_dict() for item in self.items],
        }


@dataclass(frozen=True)
class SplicePlanDiff:
    """Planned-vs-as-built comparison for one work order's live plan."""

    plan_id: uuid.UUID
    executed_items: tuple[uuid.UUID, ...]
    pending_review_items: tuple[uuid.UUID, ...]
    unexecuted_items: tuple[uuid.UUID, ...]
    unplanned_change_requests: tuple[uuid.UUID, ...]

    @property
    def complete(self) -> bool:
        return not self.unexecuted_items and not self.unplanned_change_requests

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "executed_items": list(self.executed_items),
            "pending_review_items": list(self.pending_review_items),
            "unexecuted_items": list(self.unexecuted_items),
            "unplanned_change_requests": list(self.unplanned_change_requests),
            "complete": self.complete,
        }


def _get_plan(db: Session, plan_id: str) -> FiberSplicePlan:
    try:
        plan_uuid = coerce_uuid(str(plan_id))
    except ValueError as exc:
        raise _invalid("invalid_identifier", "plan_id must be a UUID") from exc
    plan = db.get(FiberSplicePlan, plan_uuid)
    if plan is None:
        raise _not_found("plan_not_found", "Splice plan not found")
    return plan


def _plannable_strand(db: Session, strand_id: uuid.UUID, label: str) -> FiberStrand:
    strand = db.get(FiberStrand, strand_id)
    if strand is None or not strand.is_active:
        raise _not_found("strand_not_found", f"{label} strand not found")
    if strand.status not in _PLANNABLE_STRAND_STATUSES:
        raise _invalid(
            "strand_not_plannable",
            (
                f"{label} strand is {strand.status.value}; only available or "
                "reserved strands can be planned"
            ),
        )
    return strand


def _strand_end(value: str, field_name: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in {"a", "b"}:
        raise _invalid("invalid_strand_end", f"{field_name} must be a or b")
    return normalized


def _pair(item: FiberSplicePlanItem) -> set[tuple[str, str]]:
    return {
        (str(item.from_strand_id), item.from_strand_end),
        (str(item.to_strand_id), item.to_strand_end),
    }


def create_plan(
    db: Session,
    *,
    context: CommandContext,
    work_order_id: str,
    name: str,
    notes: str | None = None,
    created_by_person_id: str | None = None,
) -> FiberSplicePlan:
    """Create a draft cut sheet for one work order (one live plan each)."""

    return execute_owner_command(
        db,
        definition=_definition(_LIFECYCLE_CONCERN, "create_plan"),
        context=context,
        operation=lambda: _create_plan(
            db,
            work_order_id=work_order_id,
            name=name,
            notes=notes,
            created_by_person_id=created_by_person_id,
        ),
    )


def _create_plan(
    db: Session,
    *,
    work_order_id: str,
    name: str,
    notes: str | None,
    created_by_person_id: str | None,
) -> FiberSplicePlan:
    work_order = (
        db.query(WorkOrder)
        .filter(WorkOrder.public_id == work_order_id)
        .filter(WorkOrder.is_active.is_(True))
        .one_or_none()
    )
    if work_order is None:
        raise _not_found("work_order_not_found", "Work order not found")
    cleaned_name = (name or "").strip()
    if not cleaned_name:
        raise _invalid("name_required", "A plan name is required")
    if live_plan_for_work_order(db, work_order.id) is not None:
        raise _conflict(
            "live_plan_exists",
            "This work order already has a live splice plan",
        )
    plan = FiberSplicePlan(
        work_order_id=work_order.id,
        name=cleaned_name,
        status=FiberSplicePlanStatus.draft.value,
        notes=notes,
        created_by_person_id=coerce_uuid(created_by_person_id)
        if created_by_person_id
        else None,
    )
    db.add(plan)
    try:
        db.flush()
    except IntegrityError as exc:
        raise _conflict(
            "live_plan_exists",
            "This work order already has a live splice plan",
        ) from exc
    return plan


def add_item(
    db: Session,
    *,
    context: CommandContext,
    plan_id: str,
    closure_id: str,
    from_strand_id: str,
    from_strand_end: str,
    to_strand_id: str,
    to_strand_end: str,
    splice_type: str,
    tray_id: str | None = None,
    tray_position: int | None = None,
    expected_loss_db: float | None = None,
    notes: str | None = None,
) -> FiberSplicePlanItem:
    """Append one planned splice to a draft plan."""

    return execute_owner_command(
        db,
        definition=_definition(_LIFECYCLE_CONCERN, "add_item"),
        context=context,
        operation=lambda: _add_item(
            db,
            plan_id=plan_id,
            closure_id=closure_id,
            from_strand_id=from_strand_id,
            from_strand_end=from_strand_end,
            to_strand_id=to_strand_id,
            to_strand_end=to_strand_end,
            splice_type=splice_type,
            tray_id=tray_id,
            tray_position=tray_position,
            expected_loss_db=expected_loss_db,
            notes=notes,
        ),
    )


def _add_item(
    db: Session,
    *,
    plan_id: str,
    closure_id: str,
    from_strand_id: str,
    from_strand_end: str,
    to_strand_id: str,
    to_strand_end: str,
    splice_type: str,
    tray_id: str | None,
    tray_position: int | None,
    expected_loss_db: float | None,
    notes: str | None,
) -> FiberSplicePlanItem:
    plan = _get_plan(db, plan_id)
    if plan.status != FiberSplicePlanStatus.draft.value:
        raise _invalid("plan_not_editable", "Only a draft plan can be edited")
    if len(plan.items) >= _MAX_ITEMS_PER_PLAN:
        raise _invalid("plan_full", "The plan has reached its item limit")

    try:
        closure_uuid = coerce_uuid(str(closure_id))
        from_uuid = coerce_uuid(str(from_strand_id))
        to_uuid = coerce_uuid(str(to_strand_id))
    except ValueError as exc:
        raise _invalid("invalid_identifier", "identifiers must be UUIDs") from exc
    from_end = _strand_end(from_strand_end, "from_strand_end")
    to_end = _strand_end(to_strand_end, "to_strand_end")
    if from_uuid == to_uuid:
        raise _invalid("self_splice", "A strand cannot be spliced to itself")
    cleaned_type = (splice_type or "").strip()
    if not cleaned_type:
        raise _invalid("splice_type_required", "splice_type is required")

    closure = db.get(FiberSpliceClosure, closure_uuid)
    if closure is None or not closure.is_active:
        raise _not_found("closure_not_found", "Splice closure not found")
    _plannable_strand(db, from_uuid, "from")
    _plannable_strand(db, to_uuid, "to")

    tray_uuid = None
    if tray_id:
        try:
            tray_uuid = coerce_uuid(str(tray_id))
        except ValueError as exc:
            raise _invalid("invalid_identifier", "tray_id must be a UUID") from exc
        tray = db.get(FiberSpliceTray, tray_uuid)
        if tray is None:
            raise _not_found("tray_not_found", "Splice tray not found")
        if tray.closure_id != closure.id:
            raise _invalid(
                "tray_closure_mismatch",
                "Splice tray does not belong to this closure",
            )

    pair = {(str(from_uuid), from_end), (str(to_uuid), to_end)}
    for existing in plan.items:
        if _pair(existing) == pair:
            raise _conflict(
                "duplicate_planned_pair",
                "This strand-end pair is already planned",
            )

    next_index = max((item.position_index for item in plan.items), default=0) + 1
    item = FiberSplicePlanItem(
        plan_id=plan.id,
        position_index=next_index,
        closure_id=closure.id,
        tray_id=tray_uuid,
        tray_position=tray_position,
        from_strand_id=from_uuid,
        from_strand_end=from_end,
        to_strand_id=to_uuid,
        to_strand_end=to_end,
        splice_type=cleaned_type,
        expected_loss_db=expected_loss_db,
        notes=notes,
    )
    db.add(item)
    db.flush()
    return item


def remove_item(
    db: Session, *, context: CommandContext, plan_id: str, item_id: str
) -> None:
    """Remove one planned splice from a draft plan."""

    execute_owner_command(
        db,
        definition=_definition(_LIFECYCLE_CONCERN, "remove_item"),
        context=context,
        operation=lambda: _remove_item(db, plan_id=plan_id, item_id=item_id),
    )


def _remove_item(db: Session, *, plan_id: str, item_id: str) -> None:
    plan = _get_plan(db, plan_id)
    if plan.status != FiberSplicePlanStatus.draft.value:
        raise _invalid("plan_not_editable", "Only a draft plan can be edited")
    try:
        item_uuid = coerce_uuid(str(item_id))
    except ValueError as exc:
        raise _invalid("invalid_identifier", "item_id must be a UUID") from exc
    item = db.get(FiberSplicePlanItem, item_uuid)
    if item is None or item.plan_id != plan.id:
        raise _not_found("item_not_found", "Plan item not found")
    db.delete(item)
    db.flush()


def issue_plan(
    db: Session, *, context: CommandContext, plan_id: str
) -> FiberSplicePlan:
    """Commit a draft plan to the field: it becomes executable evidence scope."""

    return execute_owner_command(
        db,
        definition=_definition(_LIFECYCLE_CONCERN, "issue_plan"),
        context=context,
        operation=lambda: _issue_plan(db, plan_id=plan_id),
    )


def _issue_plan(db: Session, *, plan_id: str) -> FiberSplicePlan:
    plan = _get_plan(db, plan_id)
    if plan.status != FiberSplicePlanStatus.draft.value:
        raise _invalid("plan_not_issuable", "Only a draft plan can be issued")
    if not plan.items:
        raise _invalid("plan_empty", "An empty plan cannot be issued")
    plan.status = FiberSplicePlanStatus.issued.value
    plan.issued_at = datetime.now(UTC)
    db.flush()
    emit_event(
        db,
        EventType.fiber_splice_plan_issued,
        {
            "plan_id": str(plan.id),
            "work_order_id": str(plan.work_order_id),
            "item_count": len(plan.items),
        },
    )
    return plan


def cancel_plan(
    db: Session, *, context: CommandContext, plan_id: str
) -> FiberSplicePlan:
    """Cancel a plan; remaining unexecuted items stop gating completion."""

    return execute_owner_command(
        db,
        definition=_definition(_LIFECYCLE_CONCERN, "cancel_plan"),
        context=context,
        operation=lambda: _cancel_plan(db, plan_id=plan_id),
    )


def _cancel_plan(db: Session, *, plan_id: str) -> FiberSplicePlan:
    plan = _get_plan(db, plan_id)
    if plan.status == FiberSplicePlanStatus.cancelled.value:
        return plan
    plan.status = FiberSplicePlanStatus.cancelled.value
    plan.cancelled_at = datetime.now(UTC)
    db.flush()
    emit_event(
        db,
        EventType.fiber_splice_plan_cancelled,
        {
            "plan_id": str(plan.id),
            "work_order_id": str(plan.work_order_id),
        },
    )
    return plan


def live_plan_for_work_order(
    db: Session, work_order_uuid: uuid.UUID
) -> FiberSplicePlan | None:
    return (
        db.query(FiberSplicePlan)
        .filter(FiberSplicePlan.work_order_id == work_order_uuid)
        .filter(FiberSplicePlan.status != FiberSplicePlanStatus.cancelled.value)
        .one_or_none()
    )


def _item_execution(
    db: Session, item: FiberSplicePlanItem
) -> SplicePlanItemExecution | None:
    if item.executed_change_request_id is None:
        return None
    request = db.get(FiberChangeRequest, item.executed_change_request_id)
    if request is None:
        return None
    return SplicePlanItemExecution(change_request_id=request.id, status=request.status)


def _item_view(db: Session, item: FiberSplicePlanItem) -> SplicePlanItemView:
    from_strand = db.get(FiberStrand, item.from_strand_id)
    to_strand = db.get(FiberStrand, item.to_strand_id)
    return SplicePlanItemView(
        item_id=item.id,
        position_index=item.position_index,
        closure_id=item.closure_id,
        tray_id=item.tray_id,
        tray_position=item.tray_position,
        from_strand_id=item.from_strand_id,
        from_strand_end=item.from_strand_end,
        to_strand_id=item.to_strand_id,
        to_strand_end=item.to_strand_end,
        splice_type=item.splice_type,
        expected_loss_db=item.expected_loss_db,
        notes=item.notes,
        from_strand_colors=derive_segment_strand_colors(
            from_strand.segment, from_strand.strand_number
        )
        if from_strand is not None
        else None,
        to_strand_colors=derive_segment_strand_colors(
            to_strand.segment, to_strand.strand_number
        )
        if to_strand is not None
        else None,
        execution=_item_execution(db, item),
    )


def plan_view(db: Session, plan: FiberSplicePlan) -> SplicePlanView:
    work_order = db.get(WorkOrder, plan.work_order_id)
    return SplicePlanView(
        plan_id=plan.id,
        work_order_id=plan.work_order_id,
        work_order_public_id=work_order.public_id if work_order else "",
        name=plan.name,
        status=FiberSplicePlanStatus(plan.status),
        notes=plan.notes,
        issued_at=plan.issued_at,
        cancelled_at=plan.cancelled_at,
        items=tuple(_item_view(db, item) for item in plan.items),
    )


def get_plan_view(db: Session, plan_id: str) -> SplicePlanView:
    return plan_view(db, _get_plan(db, plan_id))


def view_for_work_order(
    db: Session, work_order_uuid: uuid.UUID
) -> SplicePlanView | None:
    plan = live_plan_for_work_order(db, work_order_uuid)
    if plan is None:
        return None
    return plan_view(db, plan)


def match_item_for_execution(
    db: Session,
    *,
    work_order_uuid: uuid.UUID,
    closure_id: uuid.UUID,
    pair: set[tuple[str, str]],
) -> FiberSplicePlanItem | None:
    """Find the unexecuted issued-plan item matching an exact proposal pair."""

    plan = live_plan_for_work_order(db, work_order_uuid)
    if plan is None or plan.status != FiberSplicePlanStatus.issued.value:
        return None
    for item in plan.items:
        if item.closure_id != closure_id:
            continue
        if _pair(item) != pair:
            continue
        execution = _item_execution(db, item)
        if execution is None or execution.status == FiberChangeRequestStatus.rejected:
            return item
    return None


def resolve_item_for_execution(
    db: Session,
    *,
    plan_item_id: str,
    work_order_uuid: uuid.UUID,
    closure_id: uuid.UUID,
    pair: set[tuple[str, str]],
) -> FiberSplicePlanItem:
    """Validate an explicit plan item against the exact proposed splice."""

    try:
        item_uuid = coerce_uuid(str(plan_item_id))
    except ValueError as exc:
        raise _invalid("invalid_identifier", "plan_item_id must be a UUID") from exc
    item = db.get(FiberSplicePlanItem, item_uuid)
    if item is None:
        raise _not_found("item_not_found", "Plan item not found")
    plan = item.plan
    if plan.status != FiberSplicePlanStatus.issued.value:
        raise _invalid("plan_not_issued", "The plan is not issued for execution")
    if plan.work_order_id != work_order_uuid:
        raise _invalid(
            "plan_work_order_mismatch",
            "The plan item belongs to a different work order",
        )
    if item.closure_id != closure_id or _pair(item) != pair:
        raise _invalid(
            "plan_item_mismatch",
            (
                "The proposed splice does not match the cut sheet entry; record "
                "it without a plan item and flag the deviation for review"
            ),
        )
    execution = _item_execution(db, item)
    if execution is not None and execution.status != FiberChangeRequestStatus.rejected:
        raise _conflict(
            "item_already_executed",
            "This plan item already has an executing splice proposal",
        )
    return item


def record_execution(
    db: Session,
    *,
    context: CommandContext,
    item: FiberSplicePlanItem,
    change_request: FiberChangeRequest,
) -> None:
    """Link the executing change request to its plan item (sole writer)."""

    def _link() -> None:
        item.executed_change_request_id = change_request.id
        db.flush()
        emit_event(
            db,
            EventType.fiber_splice_plan_item_executed,
            {
                "plan_id": str(item.plan_id),
                "plan_item_id": str(item.id),
                "change_request_id": str(change_request.id),
            },
        )

    execute_owner_command(
        db,
        definition=_definition(_EXECUTION_CONCERN, "record_execution"),
        context=context,
        operation=_link,
    )


def diff_for_work_order(
    db: Session, work_order_uuid: uuid.UUID
) -> SplicePlanDiff | None:
    """Planned-vs-as-built: executed, pending review, unexecuted, unplanned."""

    plan = live_plan_for_work_order(db, work_order_uuid)
    if plan is None:
        return None
    executed: list[uuid.UUID] = []
    pending: list[uuid.UUID] = []
    unexecuted: list[uuid.UUID] = []
    linked_requests: set[uuid.UUID] = set()
    for item in plan.items:
        execution = _item_execution(db, item)
        if execution is not None:
            linked_requests.add(execution.change_request_id)
        if execution is None or execution.status == FiberChangeRequestStatus.rejected:
            unexecuted.append(item.id)
        elif execution.status == FiberChangeRequestStatus.pending:
            pending.append(item.id)
            executed.append(item.id)
        else:
            executed.append(item.id)
    unplanned = [
        request.id
        for request in db.query(FiberChangeRequest)
        .filter(FiberChangeRequest.asset_type == "fiber_splice")
        .filter(
            FiberChangeRequest.payload["work_order_id"].as_string()
            == str(work_order_uuid)
        )
        .all()
        if request.id not in linked_requests
        and request.status != FiberChangeRequestStatus.rejected
    ]
    return SplicePlanDiff(
        plan_id=plan.id,
        executed_items=tuple(executed),
        pending_review_items=tuple(pending),
        unexecuted_items=tuple(unexecuted),
        unplanned_change_requests=tuple(unplanned),
    )
