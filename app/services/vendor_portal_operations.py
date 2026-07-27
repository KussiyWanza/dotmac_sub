"""Vendor projects, quotes and as-built workflow native to Sub."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import TypeVar
from uuid import UUID

from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload, selectinload

from app.models.domain_settings import SettingDomain
from app.models.vendor_routes import (
    AsBuiltRoute,
    AsBuiltRouteStatus,
    InstallationProject,
    InstallationProjectStatus,
    ProjectQuote,
    ProjectQuoteStatus,
    ProposedRouteRevision,
    ProposedRouteRevisionStatus,
)
from app.models.work_order import WorkOrder
from app.schemas.status_presentation import StatusPresentation
from app.schemas.vendor_portal import (
    VendorAsBuiltCreate,
    VendorQuoteCreate,
    VendorQuoteLineCreate,
    VendorQuoteLineUpdate,
    VendorRouteRevisionCreate,
)
from app.services.common import coerce_uuid
from app.services.domain_errors import DomainError
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.settings_spec import resolve_value
from app.services.status_presentation import (
    proposed_route_revision_status_presentation,
)
from app.services.ui_contracts import Action
from app.services.vendor_portal_errors import (
    VendorPortalOperationError,
    VendorProjectLifecycleError,
)

_EDITABLE_QUOTES = {
    ProjectQuoteStatus.draft.value,
    ProjectQuoteStatus.revision_requested.value,
}
# Awarding a project (``approved``) and everything after it closes quoting:
# post-award change is a variation, not another bid. ``assigned`` is the
# directed-work intake state: the vendor was named without a bidding round and
# still has to quote.
_QUOTE_CREATION_STATUSES = {
    InstallationProjectStatus.draft.value,
    InstallationProjectStatus.assigned.value,
    InstallationProjectStatus.open_for_bidding.value,
    InstallationProjectStatus.quoted.value,
}
ResultT = TypeVar("ResultT")


class VendorProjectWorkspaceError(DomainError):
    """Stable failures from vendor workspace policy and record boundaries."""


def _error(suffix: str, message: str) -> VendorProjectWorkspaceError:
    return VendorProjectWorkspaceError(
        code=f"operations.vendor_project_workspace.{suffix}",
        message=message,
    )


def _definition(name: str) -> OwnerCommandDefinition:
    return OwnerCommandDefinition(
        owner="operations.vendor_project_workspace",
        concern="vendor project workspace mutation coordination",
        name=name,
    )


def _execute(
    db: Session,
    *,
    context: CommandContext,
    name: str,
    operation: Callable[[], ResultT],
) -> ResultT:
    return execute_owner_command(
        db,
        definition=_definition(name),
        context=context,
        operation=operation,
    )


@dataclass(frozen=True, slots=True)
class CreateVendorQuoteCommand:
    context: CommandContext
    payload: VendorQuoteCreate
    vendor_id: str
    user_id: str
    resolved_currency: str | None = None
    quote_validity_days: int | None = None


@dataclass(frozen=True, slots=True)
class AddVendorQuoteLineCommand:
    context: CommandContext
    quote_id: str
    payload: VendorQuoteLineCreate
    vendor_id: str


@dataclass(frozen=True, slots=True)
class UpdateVendorQuoteLineCommand:
    context: CommandContext
    quote_id: str
    line_id: str
    payload: VendorQuoteLineUpdate
    vendor_id: str


@dataclass(frozen=True, slots=True)
class DeleteVendorQuoteLineCommand:
    context: CommandContext
    quote_id: str
    line_id: str
    vendor_id: str


@dataclass(frozen=True, slots=True)
class ReviewVendorQuoteCommand:
    context: CommandContext
    quote_id: str
    reviewer_id: str
    approve: bool
    notes: str | None


@dataclass(frozen=True, slots=True)
class CreateVendorRouteRevisionCommand:
    context: CommandContext
    quote_id: str
    payload: VendorRouteRevisionCreate
    vendor_id: str


@dataclass(frozen=True, slots=True)
class SubmitVendorRouteRevisionCommand:
    context: CommandContext
    revision_id: str
    vendor_id: str
    user_id: str


@dataclass(frozen=True, slots=True)
class VendorRouteRevisionItem:
    id: UUID
    revision_number: int
    status: StatusPresentation
    length_meters: float | None
    length_label: str
    review_notes: str | None
    submit_action: Action


@dataclass(frozen=True, slots=True)
class VendorRouteAuthoringProjection:
    create_action: Action
    revisions: tuple[VendorRouteRevisionItem, ...]


@dataclass(frozen=True, slots=True)
class StageVendorQuoteSubmission:
    context: CommandContext
    quote_id: str
    vendor_id: str


@dataclass(frozen=True, slots=True)
class StageVendorAsBuiltSubmission:
    context: CommandContext
    payload: VendorAsBuiltCreate
    vendor_id: str
    user_id: str


def _lifecycle_project(
    db: Session, project_id: str, *, for_update: bool = False
) -> InstallationProject:
    query = db.query(InstallationProject).filter(
        InstallationProject.id == coerce_uuid(project_id)
    )
    if for_update:
        query = query.with_for_update(of=InstallationProject)
    row = query.one_or_none()
    if row is None or not row.is_active:
        raise VendorProjectLifecycleError(
            "not_found", "Installation project not found", kind="not_found"
        )
    return row


def _now() -> datetime:
    return datetime.now(UTC)


def _money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _as_utc(value: datetime | None) -> datetime | None:
    """Bidding-window columns are ``timezone=True``, but SQLite hands back naive
    values. Compare everything in UTC rather than raising on a mixed pair."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _quote_creation_eligibility(
    project: InstallationProject,
    vendor_id: str,
    *,
    now: datetime,
) -> tuple[bool, str | None]:
    """Whether this vendor may open a *new* quote on this installation project.

    Visibility is the rule: a vendor may quote work directed at them, or work
    genuinely published for bidding — nothing else. The marketplace read
    (``list_projects(available=True)``) already applied this shape as a query
    filter; stating it here makes it a decision the command owner enforces
    under lock instead of an assumption the listing happened to imply.
    """
    assigned_vendor_id = project.assigned_vendor_id
    if assigned_vendor_id is not None and str(assigned_vendor_id) != str(vendor_id):
        return False, "Project is assigned to another vendor."
    if project.status not in _QUOTE_CREATION_STATUSES:
        return False, "Project is no longer accepting quotes."
    if assigned_vendor_id is not None:
        # Directed work: the assignment itself is the invitation, so it does
        # not depend on a bidding window.
        return True, None
    if project.status != InstallationProjectStatus.open_for_bidding.value and (
        project.status != InstallationProjectStatus.quoted.value
    ):
        return False, "Project is not open for bidding."
    opens_at = _as_utc(project.bidding_open_at)
    closes_at = _as_utc(project.bidding_close_at)
    if opens_at is None or closes_at is None:
        return False, "Project has no open bidding window."
    if now < opens_at:
        return False, "Bidding has not opened yet."
    if now > closes_at:
        return False, "Bidding window has closed."
    return True, None


def _as_built_submission_eligibility(
    project: InstallationProject,
) -> tuple[bool, str | None]:
    if project.status == InstallationProjectStatus.verified.value:
        return False, "Verified work cannot receive another as-built submission"
    if project.status not in {
        InstallationProjectStatus.in_progress.value,
        InstallationProjectStatus.completed.value,
    }:
        return False, "As-built evidence is available after field work starts"
    submissions = sorted(
        getattr(project, "as_built_routes", ()),
        key=lambda item: (item.submitted_at or item.created_at, str(item.id)),
    )
    if not submissions:
        return True, None
    latest = submissions[-1]
    if latest.status in {
        AsBuiltRouteStatus.submitted.value,
        AsBuiltRouteStatus.under_review.value,
    }:
        return False, "The latest as-built submission is awaiting staff review"
    if (
        latest.status == AsBuiltRouteStatus.accepted.value
        and project.status != InstallationProjectStatus.in_progress.value
    ):
        return False, "The latest as-built submission has already been accepted"
    return True, None


def _verification_evidence_policy(
    db: Session,
    project: InstallationProject,
    *,
    for_update: bool = False,
) -> dict:
    """Resolve work-order policy and the latest project-level evidence."""

    work_order_query = (
        db.query(WorkOrder)
        .filter(
            WorkOrder.project_id == project.project_id,
            WorkOrder.is_active.is_(True),
        )
        .order_by(WorkOrder.id.asc())
    )
    if for_update:
        work_order_query = work_order_query.with_for_update(of=WorkOrder)
    work_orders = work_order_query.all()

    evidence_query = (
        db.query(AsBuiltRoute)
        .filter(AsBuiltRoute.project_id == project.id)
        .order_by(
            AsBuiltRoute.version.desc(),
            AsBuiltRoute.submitted_at.desc(),
            AsBuiltRoute.created_at.desc(),
            AsBuiltRoute.id.desc(),
        )
    )
    if for_update:
        evidence_query = evidence_query.with_for_update(of=AsBuiltRoute)
    latest = evidence_query.first()

    required = (
        any(row.requires_as_built_evidence for row in work_orders)
        if work_orders
        else True
    )
    accepted = latest is not None and latest.status == AsBuiltRouteStatus.accepted.value
    eligible = not required or accepted
    if eligible:
        blocked_reason = None
    elif latest is None:
        blocked_reason = "Accepted as-built evidence is required before verification"
    else:
        blocked_reason = (
            "The latest as-built evidence must be accepted before verification "
            f"(currently {latest.status.replace('_', ' ')})"
        )

    return {
        "required": required,
        "eligible": eligible,
        "reason": blocked_reason,
        "source": "work_order" if work_orders else "default_enabled",
        "work_orders": [
            {
                "id": str(row.id),
                "public_id": row.public_id,
                "requires_as_built_evidence": row.requires_as_built_evidence,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }
            for row in work_orders
        ],
        "latest_as_built": (
            {
                "id": str(latest.id),
                "version": latest.version,
                "status": latest.status,
                "updated_at": (
                    latest.updated_at.isoformat() if latest.updated_at else None
                ),
            }
            if latest is not None
            else None
        ),
    }


def _project(
    db: Session, project_id: str, *, for_update: bool = False
) -> InstallationProject:
    query = db.query(InstallationProject).filter(
        InstallationProject.id == coerce_uuid(project_id)
    )
    if for_update:
        query = query.with_for_update(of=InstallationProject)
    row = query.one_or_none()
    if row is None or not row.is_active:
        raise _error("project_not_found", "Installation project not found.")
    return row


def _quote(
    db: Session,
    quote_id: str,
    vendor_id: str | None = None,
    *,
    for_update: bool = False,
) -> ProjectQuote:
    query = (
        db.query(ProjectQuote)
        .options(
            selectinload(ProjectQuote.line_items),
            joinedload(ProjectQuote.vendor),
            joinedload(ProjectQuote.project).joinedload(InstallationProject.project),
            selectinload(ProjectQuote.route_revisions).selectinload(
                ProposedRouteRevision.review_events
            ),
        )
        .filter(ProjectQuote.id == coerce_uuid(quote_id))
        .filter(ProjectQuote.is_active.is_(True))
    )
    if for_update:
        query = query.with_for_update(of=ProjectQuote)
    row = query.one_or_none()
    if row is None or (vendor_id and str(row.vendor_id) != str(vendor_id)):
        raise _error("quote_not_found", "Project quote not found.")
    return row


def _as_built(
    db: Session, as_built_id: str, *, for_update: bool = False
) -> AsBuiltRoute:
    query = (
        db.query(AsBuiltRoute)
        .options(
            joinedload(AsBuiltRoute.project).joinedload(
                InstallationProject.assigned_vendor
            ),
            joinedload(AsBuiltRoute.project).joinedload(InstallationProject.project),
            selectinload(AsBuiltRoute.line_items),
            selectinload(AsBuiltRoute.review_events),
        )
        .filter(AsBuiltRoute.id == coerce_uuid(as_built_id))
    )
    if for_update:
        query = query.with_for_update(of=AsBuiltRoute)
    row = query.one_or_none()
    if row is None:
        raise VendorPortalOperationError(
            "as_built_not_found", "As-built evidence not found", kind="not_found"
        )
    return row


def _route_revision(
    db: Session,
    revision_id: str,
    *,
    for_update: bool = False,
) -> ProposedRouteRevision:
    query = (
        db.query(ProposedRouteRevision)
        .options(
            joinedload(ProposedRouteRevision.quote).joinedload(ProjectQuote.vendor),
            joinedload(ProposedRouteRevision.quote)
            .joinedload(ProjectQuote.project)
            .joinedload(InstallationProject.project),
            selectinload(ProposedRouteRevision.review_events),
        )
        .filter(ProposedRouteRevision.id == coerce_uuid(revision_id))
    )
    if for_update:
        query = query.with_for_update(of=ProposedRouteRevision)
    row = query.one_or_none()
    if row is None:
        raise VendorPortalOperationError(
            "route_revision_not_found",
            "Proposed route revision not found",
            kind="not_found",
        )
    return row


def _serialize_project(
    row: InstallationProject, viewer_vendor_id: str | None = None
) -> dict:
    project = row.project
    is_mine = (
        viewer_vendor_id is not None
        and row.assigned_vendor_id is not None
        and str(row.assigned_vendor_id) == str(viewer_vendor_id)
    )
    lifecycle_action = None
    if is_mine and row.status == InstallationProjectStatus.approved.value:
        lifecycle_action = Action(
            key="start",
            label="Start work",
            allowed=True,
            preview_url=f"/vendor/projects/{row.id}/start",
            affected=1,
            requires_confirmation=True,
        )
    elif is_mine and row.status == InstallationProjectStatus.in_progress.value:
        lifecycle_action = Action(
            key="complete",
            label="Mark complete",
            allowed=True,
            preview_url=f"/vendor/projects/{row.id}/complete",
            affected=1,
            requires_confirmation=True,
        )
    as_built_allowed, as_built_reason = _as_built_submission_eligibility(row)
    as_built_allowed = is_mine and as_built_allowed
    if not is_mine:
        as_built_reason = "As-built submission is available after award"
    as_built_action = Action(
        key="submit_as_built",
        label="Review and submit as-built",
        allowed=as_built_allowed,
        reason=as_built_reason,
    )
    return {
        "id": row.id,
        "project_id": row.project_id,
        "project_code": getattr(project, "code", None),
        "project_name": getattr(project, "name", None),
        "subscriber_id": row.subscriber_id,
        "assigned_vendor_id": row.assigned_vendor_id,
        "assignment_type": row.assignment_type,
        "status": row.status,
        "bidding_open_at": row.bidding_open_at,
        "bidding_close_at": row.bidding_close_at,
        "approved_quote_id": row.approved_quote_id,
        "procurement_order_reference": row.procurement_order_reference,
        "notes": row.notes,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "lifecycle_action": lifecycle_action,
        "as_built_action": as_built_action,
        "lifecycle_events": [
            {
                "id": event.id,
                "event_type": event.event_type,
                "from_status": event.from_status,
                "to_status": event.to_status,
                "actor_type": event.actor_type,
                "actor_id": event.actor_id,
                "reason": event.reason,
                "decision_context": event.decision_context,
                "occurred_at": event.occurred_at,
            }
            for event in sorted(
                getattr(row, "lifecycle_events", ()),
                key=lambda item: (item.occurred_at, str(item.id)),
            )
        ],
        "as_built_submissions": [
            {
                "id": item.id,
                "status": item.status,
                "version": item.version,
                "submitted_at": item.submitted_at,
                "reviewed_at": item.reviewed_at,
                "review_notes": item.review_notes,
                "actual_length_meters": item.actual_length_meters,
                "line_item_count": len(item.line_items),
            }
            for item in sorted(
                getattr(row, "as_built_routes", ()),
                key=lambda item: (
                    item.submitted_at or item.created_at,
                    str(item.id),
                ),
            )
        ],
    }


def _serialize_quote(row: ProjectQuote) -> dict:
    editable = row.status in _EDITABLE_QUOTES
    route_revisions = tuple(
        VendorRouteRevisionItem(
            id=revision.id,
            revision_number=revision.revision_number,
            status=proposed_route_revision_status_presentation(revision.status),
            length_meters=revision.length_meters,
            length_label=(
                f"{revision.length_meters:,.1f} m"
                if revision.length_meters is not None
                else "Length unavailable"
            ),
            review_notes=revision.review_notes,
            submit_action=Action(
                key="submit_route_revision",
                label="Submit for review",
                allowed=revision.status == ProposedRouteRevisionStatus.draft.value,
                reason=(
                    None
                    if revision.status == ProposedRouteRevisionStatus.draft.value
                    else (
                        "Only a draft route revision can be submitted "
                        f"(currently {revision.status.replace('_', ' ')})"
                    )
                ),
                affected=1,
            ),
        )
        for revision in sorted(
            getattr(row, "route_revisions", ()),
            key=lambda item: (item.revision_number, str(item.id)),
            reverse=True,
        )
    )
    reviewable = row.status in {
        ProjectQuoteStatus.submitted.value,
        ProjectQuoteStatus.under_review.value,
    }
    project = getattr(row, "project", None)
    native_project = getattr(project, "project", None)
    vendor = getattr(row, "vendor", None)
    return {
        "id": row.id,
        "project_id": row.project_id,
        "project_name": getattr(native_project, "name", None),
        "project_code": getattr(native_project, "code", None),
        "vendor_id": row.vendor_id,
        "vendor_name": getattr(vendor, "name", None),
        "status": row.status,
        # Editability is projected from the same set the mutation paths enforce;
        # the template consumes allowed/reason and never re-derives status rules.
        "edit_action": Action(
            key="edit",
            label="Edit quote",
            allowed=editable,
            reason=None
            if editable
            else f"A {row.status.replace('_', ' ')} quote cannot be edited",
        ),
        "currency": row.currency,
        "subtotal": row.subtotal,
        "vat_rate_percent": row.vat_rate_percent,
        "tax_total": row.tax_total,
        "total": row.total,
        "valid_from": row.valid_from,
        "valid_until": row.valid_until,
        "submitted_at": row.submitted_at,
        "reviewed_at": row.reviewed_at,
        "review_notes": row.review_notes,
        "line_items": [item for item in row.line_items if item.is_active],
        "route_authoring": VendorRouteAuthoringProjection(
            create_action=Action(
                key="create_route_revision",
                label="Save route draft",
                allowed=True,
                affected=1,
            ),
            revisions=route_revisions,
        ),
        "route_revisions": [
            _serialize_route_revision_review(revision)
            for revision in sorted(
                getattr(row, "route_revisions", ()),
                key=lambda item: (item.revision_number, str(item.id)),
                reverse=True,
            )
        ],
        "approve_action": Action(
            key="approve_quote",
            label="Approve quote",
            allowed=reviewable,
            reason=(
                None
                if reviewable
                else f"A {row.status.replace('_', ' ')} quote is not reviewable"
            ),
            affected=1,
        ),
        "revision_action": Action(
            key="request_quote_revision",
            label="Request revision",
            allowed=reviewable,
            reason=(
                None
                if reviewable
                else f"A {row.status.replace('_', ' ')} quote is not reviewable"
            ),
            affected=1,
        ),
        "approve_url": f"/admin/vendors/operations/quotes/{row.id}/approve",
        "revision_url": (f"/admin/vendors/operations/quotes/{row.id}/request-revision"),
    }


def _serialize_as_built_review(row: AsBuiltRoute) -> dict:
    project = row.project
    vendor = project.assigned_vendor
    status_reviewable = row.status in {
        AsBuiltRouteStatus.submitted.value,
        AsBuiltRouteStatus.under_review.value,
    }
    reviewable = status_reviewable and project.assigned_vendor_id is not None
    blocked_reason = None
    if not status_reviewable:
        blocked_reason = f"Evidence is already {row.status}"
    elif project.assigned_vendor_id is None:
        blocked_reason = "Evidence has no assigned vendor"
    return {
        "id": row.id,
        "project_id": row.project_id,
        "project_name": getattr(project.project, "name", None),
        "project_code": getattr(project.project, "code", None),
        "vendor_id": project.assigned_vendor_id,
        "vendor_name": getattr(vendor, "name", None),
        "status": row.status,
        "version": row.version,
        "submitted_at": row.submitted_at,
        "submitted_by_person_id": row.submitted_by_person_id,
        "reviewed_at": row.reviewed_at,
        "review_notes": row.review_notes,
        "has_geometry": row.route_geom is not None,
        "actual_length_meters": row.actual_length_meters,
        "variation_type": row.variation_type,
        "variation_reason": row.variation_reason,
        "work_order_ref": row.work_order_ref,
        "line_items": [item for item in row.line_items if item.is_active],
        "review_events": [
            {
                "id": event.id,
                "event_type": event.event_type,
                "from_status": event.from_status,
                "to_status": event.to_status,
                "actor_type": event.actor_type,
                "actor_id": event.actor_id,
                "reason": event.reason,
                "occurred_at": event.occurred_at,
            }
            for event in row.review_events
        ],
        "accept_action": Action(
            key="accept_as_built",
            label="Accept evidence",
            allowed=reviewable,
            reason=blocked_reason,
            permission="inventory:write",
            preview_url=(f"/admin/vendors/operations/as-built/{row.id}/accept/preview"),
            affected=1,
            requires_confirmation=True,
        ),
        "reject_action": Action(
            key="reject_as_built",
            label="Reject evidence",
            allowed=reviewable,
            reason=blocked_reason,
            permission="inventory:write",
            preview_url=(f"/admin/vendors/operations/as-built/{row.id}/reject/preview"),
            affected=1,
            requires_confirmation=True,
        ),
    }


def _serialize_route_revision_review(row: ProposedRouteRevision) -> dict:
    quote = row.quote
    project = quote.project
    vendor = quote.vendor
    reviewable = row.status == ProposedRouteRevisionStatus.submitted.value
    blocked_reason = None if reviewable else f"Route revision is already {row.status}"
    detail_url = f"/admin/vendors/routes/{project.id}?revision_id={row.id}"
    return {
        "id": row.id,
        "quote_id": row.quote_id,
        "project_id": project.id,
        "project_name": getattr(project.project, "name", None),
        "project_code": getattr(project.project, "code", None),
        "vendor_id": quote.vendor_id,
        "vendor_name": getattr(vendor, "name", None),
        "revision_number": row.revision_number,
        "status": row.status,
        "length_meters": row.length_meters,
        "submitted_at": row.submitted_at,
        "submitted_by_person_id": row.submitted_by_person_id,
        "reviewed_at": row.reviewed_at,
        "reviewed_by_person_id": row.reviewed_by_person_id,
        "review_notes": row.review_notes,
        "has_geometry": row.route_geom is not None,
        "review_events": [
            {
                "id": event.id,
                "event_type": event.event_type,
                "from_status": event.from_status,
                "to_status": event.to_status,
                "actor_type": event.actor_type,
                "actor_id": event.actor_id,
                "reason": event.reason,
                "occurred_at": event.occurred_at,
            }
            for event in row.review_events
        ],
        "detail_url": detail_url,
        "accept_action": Action(
            key="accept_route_revision",
            label="Accept route",
            allowed=reviewable,
            reason=blocked_reason,
            permission="inventory:write",
            preview_url=(f"/admin/vendors/operations/routes/{row.id}/accept/preview"),
            affected=1,
            requires_confirmation=True,
        ),
        "reject_action": Action(
            key="reject_route_revision",
            label="Reject route",
            allowed=reviewable,
            reason=blocked_reason,
            permission="inventory:write",
            preview_url=(f"/admin/vendors/operations/routes/{row.id}/reject/preview"),
            affected=1,
            requires_confirmation=True,
        ),
    }


class VendorPortalOperations:
    @staticmethod
    def list_reviewable_route_revisions(
        db: Session,
        *,
        limit: int = 200,
    ) -> list[dict]:
        rows = (
            db.query(ProposedRouteRevision)
            .join(ProjectQuote, ProposedRouteRevision.quote_id == ProjectQuote.id)
            .join(
                InstallationProject,
                ProjectQuote.project_id == InstallationProject.id,
            )
            .options(
                joinedload(ProposedRouteRevision.quote).joinedload(ProjectQuote.vendor),
                joinedload(ProposedRouteRevision.quote)
                .joinedload(ProjectQuote.project)
                .joinedload(InstallationProject.project),
                selectinload(ProposedRouteRevision.review_events),
            )
            .filter(
                InstallationProject.is_active.is_(True),
                ProjectQuote.is_active.is_(True),
                ProposedRouteRevision.status
                == ProposedRouteRevisionStatus.submitted.value,
            )
            .order_by(
                ProposedRouteRevision.submitted_at.asc(),
                ProposedRouteRevision.id.asc(),
            )
            .limit(max(1, min(limit, 500)))
            .all()
        )
        return [_serialize_route_revision_review(row) for row in rows]

    @staticmethod
    def list_route_revisions_for_project(
        db: Session,
        project_id: str,
    ) -> list[dict]:
        rows = (
            db.query(ProposedRouteRevision)
            .join(ProjectQuote, ProposedRouteRevision.quote_id == ProjectQuote.id)
            .options(
                joinedload(ProposedRouteRevision.quote).joinedload(ProjectQuote.vendor),
                joinedload(ProposedRouteRevision.quote)
                .joinedload(ProjectQuote.project)
                .joinedload(InstallationProject.project),
                selectinload(ProposedRouteRevision.review_events),
            )
            .filter(
                ProjectQuote.project_id == coerce_uuid(project_id),
                ProjectQuote.is_active.is_(True),
            )
            .order_by(
                ProposedRouteRevision.revision_number.desc(),
                ProposedRouteRevision.id.desc(),
            )
            .all()
        )
        return [_serialize_route_revision_review(row) for row in rows]

    @staticmethod
    def list_reviewable_as_builts(db: Session, *, limit: int = 200) -> list[dict]:
        rows = (
            db.query(AsBuiltRoute)
            .join(
                InstallationProject,
                AsBuiltRoute.project_id == InstallationProject.id,
            )
            .options(
                joinedload(AsBuiltRoute.project).joinedload(
                    InstallationProject.assigned_vendor
                ),
                joinedload(AsBuiltRoute.project).joinedload(
                    InstallationProject.project
                ),
                selectinload(AsBuiltRoute.line_items),
                selectinload(AsBuiltRoute.review_events),
            )
            .filter(
                InstallationProject.is_active.is_(True),
                AsBuiltRoute.status.in_(
                    (
                        AsBuiltRouteStatus.submitted.value,
                        AsBuiltRouteStatus.under_review.value,
                    )
                ),
            )
            .order_by(AsBuiltRoute.submitted_at.asc(), AsBuiltRoute.id.asc())
            .limit(max(1, min(limit, 500)))
            .all()
        )
        return [_serialize_as_built_review(row) for row in rows]

    @staticmethod
    def get_as_built_review(db: Session, as_built_id: str) -> dict:
        return _serialize_as_built_review(_as_built(db, as_built_id))

    @staticmethod
    def list_reviewable_projects(db: Session, *, limit: int = 200) -> list[dict]:
        """Project the exact staff cohort awaiting acceptance or rework."""

        rows = (
            db.query(InstallationProject)
            .options(
                joinedload(InstallationProject.project),
                joinedload(InstallationProject.assigned_vendor),
                selectinload(InstallationProject.lifecycle_events),
            )
            .filter(
                InstallationProject.status == InstallationProjectStatus.completed.value,
                InstallationProject.is_active.is_(True),
            )
            .order_by(
                InstallationProject.updated_at.asc(),
                InstallationProject.id.asc(),
            )
            .limit(max(1, min(limit, 500)))
            .all()
        )
        projected = []
        for row in rows:
            evidence_policy = _verification_evidence_policy(db, row)
            projected.append(
                {
                    **_serialize_project(row),
                    "vendor_name": getattr(row.assigned_vendor, "name", None),
                    "verification_evidence": evidence_policy,
                    "verify_action": Action(
                        key="verify",
                        label="Verify completed work",
                        allowed=bool(evidence_policy["eligible"]),
                        reason=evidence_policy["reason"],
                        permission="inventory:write",
                        preview_url=(
                            f"/admin/vendors/operations/projects/{row.id}/verify/preview"
                        ),
                        affected=1,
                        requires_confirmation=True,
                    ),
                    "rework_action": Action(
                        key="rework",
                        label="Request rework",
                        allowed=True,
                        permission="inventory:write",
                        preview_url=(
                            f"/admin/vendors/operations/projects/{row.id}/rework/preview"
                        ),
                        affected=1,
                        requires_confirmation=True,
                    ),
                }
            )
        return projected

    @staticmethod
    def list_reviewable_quotes(db: Session, *, limit: int = 200) -> list[ProjectQuote]:
        return (
            db.query(ProjectQuote)
            .options(selectinload(ProjectQuote.line_items))
            .filter(
                ProjectQuote.status.in_(
                    (
                        ProjectQuoteStatus.submitted.value,
                        ProjectQuoteStatus.under_review.value,
                    )
                )
            )
            .filter(ProjectQuote.is_active.is_(True))
            .order_by(ProjectQuote.submitted_at.asc())
            .limit(max(1, min(limit, 500)))
            .all()
        )

    @staticmethod
    def latest_quote_for_project(
        db: Session, project_id: str, vendor_id: str
    ) -> dict | None:
        row = (
            db.query(ProjectQuote)
            .filter(ProjectQuote.project_id == coerce_uuid(project_id))
            .filter(ProjectQuote.vendor_id == coerce_uuid(vendor_id))
            .filter(ProjectQuote.is_active.is_(True))
            .order_by(ProjectQuote.created_at.desc())
            .first()
        )
        return _serialize_quote(_quote(db, str(row.id), vendor_id)) if row else None

    @staticmethod
    def list_projects(
        db: Session, vendor_id: str, *, available: bool, limit: int, offset: int
    ) -> list[dict]:
        query = db.query(InstallationProject).options(
            joinedload(InstallationProject.project),
            selectinload(InstallationProject.lifecycle_events),
            selectinload(InstallationProject.as_built_routes).selectinload(
                AsBuiltRoute.line_items
            ),
        )
        if available:
            now = _now()
            query = query.filter(
                InstallationProject.status
                == InstallationProjectStatus.open_for_bidding.value,
                InstallationProject.bidding_open_at <= now,
                InstallationProject.bidding_close_at >= now,
            )
        else:
            query = query.outerjoin(
                ProjectQuote, ProjectQuote.project_id == InstallationProject.id
            ).filter(
                or_(
                    InstallationProject.assigned_vendor_id == coerce_uuid(vendor_id),
                    ProjectQuote.vendor_id == coerce_uuid(vendor_id),
                )
            )
        rows = (
            query.filter(InstallationProject.is_active.is_(True))
            .order_by(InstallationProject.updated_at.desc())
            .distinct()
            .offset(offset)
            .limit(limit)
            .all()
        )
        return [_serialize_project(row, viewer_vendor_id=vendor_id) for row in rows]

    @staticmethod
    def create_quote(
        db: Session,
        command: CreateVendorQuoteCommand,
    ) -> dict:
        def operation() -> dict:
            from app.services import vendor_project_records

            currency = command.payload.currency or str(
                resolve_value(db, SettingDomain.billing, "default_currency")
            )
            validity_days = int(
                resolve_value(
                    db,
                    SettingDomain.projects,
                    "vendor_quote_validity_days",
                )
            )
            return vendor_project_records.stage_create_quote(
                db,
                replace(
                    command,
                    resolved_currency=currency.upper(),
                    quote_validity_days=validity_days,
                ),
            )

        return _execute(
            db,
            context=command.context,
            name="create_vendor_quote",
            operation=operation,
        )

    @staticmethod
    def get_quote(db: Session, quote_id: str, vendor_id: str) -> dict:
        return _serialize_quote(_quote(db, quote_id, vendor_id))

    @staticmethod
    def get_quote_for_review(db: Session, quote_id: str) -> dict:
        return _serialize_quote(_quote(db, quote_id))

    @staticmethod
    def preview_quote_submission(
        db: Session,
        quote_id: str,
        vendor_id: str,
        *,
        for_update: bool = False,
    ) -> dict:
        """Own the read-only impact snapshot for a quote submission."""
        quote = _quote(db, quote_id, vendor_id, for_update=for_update)
        project = (
            _project(db, str(quote.project_id), for_update=True)
            if for_update
            else quote.project
        )
        if quote.status not in _EDITABLE_QUOTES:
            raise _error("quote_not_submittable", "Quote is not submittable.")
        active = [item for item in quote.line_items if item.is_active]
        if not active:
            raise _error("quote_line_required", "Quote requires at least one line.")
        subtotal = sum((_money(item.amount) for item in active), Decimal("0.00"))
        tax_total = _money(
            subtotal * Decimal(str(quote.vat_rate_percent or 0)) / Decimal("100")
        )
        total = _money(subtotal + tax_total)
        return {
            "submission_type": "quote",
            "project_id": str(quote.project_id),
            "target_id": str(quote.id),
            "title": "Submit quote for review",
            "summary": (
                f"{len(active)} line item{'s' if len(active) != 1 else ''}; "
                f"{quote.currency} {total:,.2f} total"
            ),
            "details": [
                ("Line items", str(len(active))),
                ("Subtotal", f"{quote.currency} {subtotal:,.2f}"),
                ("Tax", f"{quote.currency} {tax_total:,.2f}"),
                ("Total", f"{quote.currency} {total:,.2f}"),
                ("Result", "Quote becomes read-only and enters staff review"),
            ],
            "state": {
                "quote_id": str(quote.id),
                "project_id": str(quote.project_id),
                "project_status": project.status,
                "project_updated_at": project.updated_at,
                "status": quote.status,
                "currency": quote.currency,
                "vat_rate_percent": quote.vat_rate_percent,
                "updated_at": quote.updated_at,
                "lines": [
                    {
                        "id": str(item.id),
                        "description": item.description,
                        "quantity": item.quantity,
                        "unit_price": item.unit_price,
                        "amount": item.amount,
                        "updated_at": item.updated_at,
                    }
                    for item in sorted(active, key=lambda row: str(row.id))
                ],
            },
        }

    @staticmethod
    def preview_as_built_submission(
        db: Session,
        payload: VendorAsBuiltCreate,
        vendor_id: str,
        *,
        for_update: bool = False,
    ) -> dict:
        """Own the read-only impact snapshot for an as-built submission."""
        project = _project(db, str(payload.project_id), for_update=for_update)
        if str(project.assigned_vendor_id) != str(vendor_id):
            raise _error(
                "project_not_assigned",
                "Project is assigned to another vendor.",
            )
        allowed, reason = _as_built_submission_eligibility(project)
        if not allowed:
            raise _error("as_built_submission_not_allowed", str(reason))
        if not payload.geojson and not payload.line_items:
            raise _error(
                "as_built_evidence_required",
                "Provide a route or line items.",
            )
        if payload.geojson:
            if payload.geojson.get("type") != "LineString" or not isinstance(
                payload.geojson.get("coordinates"), list
            ):
                raise _error(
                    "invalid_as_built_route",
                    "As-built route must be a GeoJSON LineString.",
                )
            if len(payload.geojson["coordinates"]) < 2:
                raise _error(
                    "invalid_as_built_route",
                    "As-built route requires at least two coordinates.",
                )
        length_label = (
            f"{payload.actual_length_meters:,.1f} m"
            if payload.actual_length_meters is not None
            else "Not supplied"
        )
        return {
            "submission_type": "as_built",
            "project_id": str(project.id),
            "target_id": None,
            "title": "Submit as-built route",
            "summary": "Creates the immutable route evidence staff will review",
            "details": [
                ("Route type", "GeoJSON LineString" if payload.geojson else "None"),
                ("Actual length", length_label),
                ("Variation reason", payload.variation_reason or "None"),
                ("Result", "A submitted as-built record is created for review"),
            ],
            "state": {
                "project_id": str(project.id),
                "project_status": project.status,
                "project_updated_at": project.updated_at,
                "assigned_vendor_id": str(project.assigned_vendor_id),
                "payload": payload.model_dump(mode="json"),
            },
            "payload": payload.model_dump(mode="json"),
        }

    @staticmethod
    def add_quote_line(
        db: Session,
        command: AddVendorQuoteLineCommand,
    ) -> dict:
        def operation() -> dict:
            from app.services import vendor_project_records

            return vendor_project_records.stage_add_quote_line(db, command)

        return _execute(
            db,
            context=command.context,
            name="add_vendor_quote_line",
            operation=operation,
        )

    @staticmethod
    def update_quote_line(
        db: Session,
        command: UpdateVendorQuoteLineCommand,
    ) -> dict:
        def operation() -> dict:
            from app.services import vendor_project_records

            return vendor_project_records.stage_update_quote_line(db, command)

        return _execute(
            db,
            context=command.context,
            name="update_vendor_quote_line",
            operation=operation,
        )

    @staticmethod
    def delete_quote_line(
        db: Session,
        command: DeleteVendorQuoteLineCommand,
    ) -> dict:
        def operation() -> dict:
            from app.services import vendor_project_records

            return vendor_project_records.stage_delete_quote_line(db, command)

        return _execute(
            db,
            context=command.context,
            name="delete_vendor_quote_line",
            operation=operation,
        )

    @staticmethod
    def stage_quote_submission(
        db: Session,
        command: StageVendorQuoteSubmission,
    ) -> dict:
        """Stage quote submission in the signed-confirmation transaction."""

        from app.services import vendor_project_records

        return vendor_project_records.stage_quote_submission(db, command)

    @staticmethod
    def review_quote(
        db: Session,
        command: ReviewVendorQuoteCommand,
    ) -> dict:
        def operation() -> dict:
            from app.services import vendor_project_records

            return vendor_project_records.stage_review_quote(db, command)

        return _execute(
            db,
            context=command.context,
            name="review_vendor_quote",
            operation=operation,
        )

    @staticmethod
    def preview_staff_project_lifecycle(
        db: Session,
        project_id: str,
        *,
        action: str,
        reason: str | None = None,
        for_update: bool = False,
    ) -> dict:
        """Own staff acceptance/rework eligibility and its impact snapshot."""

        project = _lifecycle_project(db, project_id, for_update=for_update)
        if project.assigned_vendor_id is None:
            raise VendorProjectLifecycleError(
                "vendor_assignment_required",
                "Completed work must have an assigned vendor before review",
            )
        normalized_reason = str(reason or "").strip() or None
        if normalized_reason and len(normalized_reason) > 2000:
            raise VendorProjectLifecycleError(
                "reason_too_long",
                "Review reason must be 2,000 characters or fewer",
                kind="invalid",
            )
        transitions = {
            "verify": (
                InstallationProjectStatus.verified.value,
                "Verify completed work",
                "Accepts the vendor completion as operationally verified",
            ),
            "rework": (
                InstallationProjectStatus.in_progress.value,
                "Request vendor rework",
                "Returns the project to field work with a required reason",
            ),
        }
        if action not in transitions:
            raise VendorProjectLifecycleError(
                "unsupported_action", "Unsupported lifecycle action", kind="invalid"
            )
        if project.status != InstallationProjectStatus.completed.value:
            raise VendorProjectLifecycleError(
                "invalid_transition",
                "Only a completed project can be verified or returned for rework",
            )
        if action == "rework" and normalized_reason is None:
            raise VendorProjectLifecycleError(
                "reason_required", "A rework reason is required", kind="invalid"
            )
        verification_evidence = (
            _verification_evidence_policy(db, project, for_update=for_update)
            if action == "verify"
            else None
        )
        if verification_evidence and not verification_evidence["eligible"]:
            raise VendorProjectLifecycleError(
                "as_built_evidence_required",
                str(verification_evidence["reason"]),
            )
        target, title, summary = transitions[action]
        native_project = project.project
        return {
            "review_type": f"project_{action}",
            "project_id": str(project.id),
            "title": title,
            "summary": summary,
            "details": [
                ("Project", getattr(native_project, "name", None) or str(project.id)),
                ("Current state", "Completed"),
                ("Result", target.replace("_", " ").title()),
                ("Reason", normalized_reason or "No additional note"),
                (
                    "As-built evidence",
                    (
                        "Required and latest submission accepted"
                        if verification_evidence and verification_evidence["required"]
                        else "Not required by linked work orders"
                        if verification_evidence
                        else "Not evaluated for rework"
                    ),
                ),
                (
                    "Financial effect",
                    "None; invoice approval and ERP payment remain separate",
                ),
            ],
            "state": {
                "project_id": str(project.id),
                "vendor_id": str(project.assigned_vendor_id),
                "from_status": project.status,
                "to_status": target,
                "reason": normalized_reason,
                "updated_at": project.updated_at,
                "verification_evidence": verification_evidence,
            },
        }

    @staticmethod
    def transition_staff_project(
        db: Session,
        project_id: str,
        *,
        action: str,
        actor_id: str,
        reason: str | None = None,
    ) -> dict:
        """Delegate the staff decision write to the lifecycle record owner."""

        from app.services.vendor_project_lifecycle import (
            StageStaffVendorProjectTransition,
            stage_staff_project_transition,
        )

        return stage_staff_project_transition(
            db,
            StageStaffVendorProjectTransition(
                project_id=project_id,
                action=action,
                actor_id=actor_id,
                reason=reason,
            ),
        )

    @staticmethod
    def preview_as_built_review(
        db: Session,
        as_built_id: str,
        *,
        action: str,
        reason: str | None = None,
        for_update: bool = False,
    ) -> dict:
        """Own accept/reject eligibility and the exact evidence preview."""

        row = _as_built(db, as_built_id, for_update=for_update)
        if row.project.assigned_vendor_id is None:
            raise VendorPortalOperationError(
                "vendor_assignment_required",
                "As-built evidence must belong to an assigned vendor",
            )
        if row.status not in {
            AsBuiltRouteStatus.submitted.value,
            AsBuiltRouteStatus.under_review.value,
        }:
            raise VendorPortalOperationError(
                "as_built_not_reviewable",
                "Only submitted as-built evidence can be reviewed",
            )
        normalized_reason = str(reason or "").strip() or None
        if normalized_reason and len(normalized_reason) > 2000:
            raise VendorPortalOperationError(
                "reason_too_long",
                "Review reason must be 2,000 characters or fewer",
                kind="invalid",
            )
        transitions = {
            "accept": (
                AsBuiltRouteStatus.accepted.value,
                "Accept as-built evidence",
                "Records that staff accepted this submitted evidence",
            ),
            "reject": (
                AsBuiltRouteStatus.rejected.value,
                "Reject as-built evidence",
                "Records why this evidence is insufficient",
            ),
        }
        if action not in transitions:
            raise VendorPortalOperationError(
                "unsupported_action",
                "Unsupported as-built review action",
                kind="invalid",
            )
        if action == "reject" and normalized_reason is None:
            raise VendorPortalOperationError(
                "reason_required", "A rejection reason is required", kind="invalid"
            )
        target, title, summary = transitions[action]
        project = row.project
        return {
            "review_type": f"as_built_{action}",
            "as_built_id": str(row.id),
            "project_id": str(row.project_id),
            "title": title,
            "summary": summary,
            "details": [
                (
                    "Project",
                    getattr(project.project, "name", None) or str(project.id),
                ),
                ("Vendor", getattr(project.assigned_vendor, "name", None) or "—"),
                ("Current evidence state", row.status.replace("_", " ").title()),
                ("Result", target.title()),
                (
                    "Route geometry",
                    "Provided" if row.route_geom is not None else "None",
                ),
                (
                    "Line items",
                    str(len([item for item in row.line_items if item.is_active])),
                ),
                ("Review reason", normalized_reason or "No additional note"),
                (
                    "Project effect",
                    "None; project verification or rework remains a separate decision",
                ),
                (
                    "Financial effect",
                    "None; invoice approval and ERP payment remain separate",
                ),
            ],
            "state": {
                "as_built_id": str(row.id),
                "project_id": str(row.project_id),
                "vendor_id": str(project.assigned_vendor_id),
                "from_status": row.status,
                "to_status": target,
                "reason": normalized_reason,
                "updated_at": row.updated_at,
                "actual_length_meters": row.actual_length_meters,
                "variation_type": row.variation_type,
                "variation_reason": row.variation_reason,
                "work_order_ref": row.work_order_ref,
                "has_geometry": row.route_geom is not None,
                "line_items": [
                    {
                        "id": str(item.id),
                        "description": item.description,
                        "quantity": item.quantity,
                        "unit_price": item.unit_price,
                        "amount": item.amount,
                        "is_active": item.is_active,
                        "updated_at": item.updated_at,
                    }
                    for item in sorted(row.line_items, key=lambda item: str(item.id))
                ],
            },
        }

    @staticmethod
    def preview_route_revision_review(
        db: Session,
        revision_id: str,
        *,
        action: str,
        reason: str | None = None,
        for_update: bool = False,
    ) -> dict:
        """Own accept/reject eligibility and the exact route preview."""

        row = _route_revision(db, revision_id, for_update=for_update)
        if row.status != ProposedRouteRevisionStatus.submitted.value:
            raise VendorPortalOperationError(
                "route_revision_not_reviewable",
                "Only submitted proposed routes can be reviewed",
            )
        normalized_reason = str(reason or "").strip() or None
        if normalized_reason and len(normalized_reason) > 2000:
            raise VendorPortalOperationError(
                "reason_too_long",
                "Review reason must be 2,000 characters or fewer",
                kind="invalid",
            )
        transitions = {
            "accept": (
                ProposedRouteRevisionStatus.accepted.value,
                "Accept proposed route",
                "Records that staff accepted this proposed route",
            ),
            "reject": (
                ProposedRouteRevisionStatus.rejected.value,
                "Reject proposed route",
                "Records why this proposed route must be revised",
            ),
        }
        if action not in transitions:
            raise VendorPortalOperationError(
                "unsupported_action",
                "Unsupported proposed-route review action",
                kind="invalid",
            )
        if action == "reject" and normalized_reason is None:
            raise VendorPortalOperationError(
                "reason_required",
                "A rejection reason is required",
                kind="invalid",
            )
        target, title, summary = transitions[action]
        quote = row.quote
        project = quote.project
        return {
            "review_type": f"route_revision_{action}",
            "revision_id": str(row.id),
            "quote_id": str(row.quote_id),
            "project_id": str(project.id),
            "title": title,
            "summary": summary,
            "details": [
                (
                    "Project",
                    getattr(project.project, "name", None) or str(project.id),
                ),
                ("Vendor", getattr(quote.vendor, "name", None) or "—"),
                ("Revision", str(row.revision_number)),
                ("Current route state", row.status.replace("_", " ").title()),
                ("Result", target.title()),
                (
                    "Route geometry",
                    "Provided" if row.route_geom is not None else "None",
                ),
                (
                    "Estimated length",
                    (
                        f"{row.length_meters:,.1f} m"
                        if row.length_meters is not None
                        else "Not provided"
                    ),
                ),
                ("Review reason", normalized_reason or "No additional note"),
                (
                    "Quote effect",
                    "None; quote approval remains a separate decision",
                ),
                (
                    "Project effect",
                    "None; installation approval and work state remain separate",
                ),
            ],
            "state": {
                "revision_id": str(row.id),
                "quote_id": str(row.quote_id),
                "project_id": str(project.id),
                "vendor_id": str(quote.vendor_id),
                "revision_number": row.revision_number,
                "from_status": row.status,
                "to_status": target,
                "reason": normalized_reason,
                "submitted_at": row.submitted_at,
                "length_meters": row.length_meters,
                "route_geometry": (
                    str(row.route_geom) if row.route_geom is not None else None
                ),
            },
        }

    @staticmethod
    def transition_route_revision_review(
        db: Session,
        revision_id: str,
        *,
        action: str,
        actor_id: str,
        reason: str | None = None,
    ) -> dict:
        """Delegate the staff route decision to the record owner."""

        from app.services.vendor_project_records import (
            StageVendorRouteRevisionReview,
            stage_route_revision_review,
        )

        return stage_route_revision_review(
            db,
            StageVendorRouteRevisionReview(
                revision_id=revision_id,
                action=action,
                actor_id=actor_id,
                reason=reason,
            ),
        )

    @staticmethod
    def transition_as_built_review(
        db: Session,
        as_built_id: str,
        *,
        action: str,
        actor_id: str,
        reason: str | None = None,
    ) -> dict:
        """Delegate the staff evidence write to the record owner."""

        from app.services.vendor_project_records import (
            StageVendorAsBuiltReview,
            stage_as_built_review,
        )

        return stage_as_built_review(
            db,
            StageVendorAsBuiltReview(
                as_built_id=as_built_id,
                action=action,
                actor_id=actor_id,
                reason=reason,
            ),
        )

    @staticmethod
    def create_route_revision(
        db: Session,
        command: CreateVendorRouteRevisionCommand,
    ) -> dict:
        def operation() -> dict:
            from app.services import vendor_project_records

            return vendor_project_records.stage_create_route_revision(db, command)

        return _execute(
            db,
            context=command.context,
            name="create_vendor_route_revision",
            operation=operation,
        )

    @staticmethod
    def submit_route_revision(
        db: Session,
        command: SubmitVendorRouteRevisionCommand,
    ) -> dict:
        def operation() -> dict:
            from app.services import vendor_project_records

            return vendor_project_records.stage_submit_route_revision(db, command)

        return _execute(
            db,
            context=command.context,
            name="submit_vendor_route_revision",
            operation=operation,
        )

    @staticmethod
    def stage_as_built_submission(
        db: Session,
        command: StageVendorAsBuiltSubmission,
    ) -> dict:
        """Stage as-built evidence in the signed-confirmation transaction."""

        from app.services import vendor_project_records

        return vendor_project_records.stage_as_built_submission(db, command)


vendor_portal_operations = VendorPortalOperations()
