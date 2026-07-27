"""Canonical participant for vendor installation-project lifecycle state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy.orm import Session

from app.models.vendor_routes import (
    InstallationProject,
    InstallationProjectLifecycleEvent,
    InstallationProjectStatus,
    Vendor,
    VendorAssignmentType,
)
from app.services.common import coerce_uuid
from app.services.events import EventType, emit_event
from app.services.vendor_portal_errors import VendorProjectLifecycleError

VendorProjectAction = Literal["start", "complete"]


def _error(suffix: str, message: str) -> VendorProjectLifecycleError:
    return VendorProjectLifecycleError(
        f"operations.vendor_project_lifecycle.{suffix}",
        message,
    )


@dataclass(frozen=True, slots=True)
class PreviewVendorProjectLifecycle:
    project_id: str
    vendor_id: str
    action: str


@dataclass(frozen=True, slots=True)
class StageVendorProjectTransition:
    project_id: str
    vendor_id: str
    action: str
    actor_id: str
    actor_type: str


@dataclass(frozen=True, slots=True)
class StageStaffVendorProjectTransition:
    project_id: str
    action: str
    actor_id: str
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class StagePublishForBidding:
    project_id: str
    actor_id: str
    bidding_open_at: datetime
    bidding_close_at: datetime


@dataclass(frozen=True, slots=True)
class StageAssignVendorDirectly:
    project_id: str
    vendor_id: str
    actor_id: str
    reason: str | None = None


def _project(
    db: Session,
    project_id: str,
    *,
    for_update: bool,
) -> InstallationProject:
    query = db.query(InstallationProject).filter(
        InstallationProject.id == coerce_uuid(project_id)
    )
    if for_update:
        query = query.with_for_update(of=InstallationProject)
    row = query.one_or_none()
    if row is None or not row.is_active:
        raise _error("not_found", "Installation project not found.")
    return row


def _transition(action: str) -> tuple[str, str, str, str]:
    transitions = {
        "start": (
            InstallationProjectStatus.approved.value,
            InstallationProjectStatus.in_progress.value,
            "Start field work",
            "Records that the assigned vendor has begun field work",
        ),
        "complete": (
            InstallationProjectStatus.in_progress.value,
            InstallationProjectStatus.completed.value,
            "Mark field work complete",
            "Records vendor completion for Dotmac review and verification",
        ),
    }
    try:
        return transitions[action]
    except KeyError as exc:
        raise _error("unsupported_action", "Unsupported lifecycle action.") from exc


def preview_project_lifecycle(
    db: Session,
    query: PreviewVendorProjectLifecycle,
    *,
    for_update: bool = False,
) -> dict:
    """Return the owner-validated impact and stale-check state."""

    project = _project(db, query.project_id, for_update=for_update)
    if project.assigned_vendor_id != coerce_uuid(query.vendor_id):
        raise _error("not_assigned", "Project is not assigned to this vendor.")
    expected, target, title, summary = _transition(query.action)
    if project.status != expected:
        label = "approved" if query.action == "start" else "in-progress"
        verb = "started" if query.action == "start" else "completed"
        raise _error(
            "invalid_transition",
            f"Only an {label} project can be {verb}.",
        )
    native_project = project.project
    return {
        "submission_type": f"project_{query.action}",
        "project_id": str(project.id),
        "target_id": str(project.id),
        "title": title,
        "summary": summary,
        "details": [
            ("Project", getattr(native_project, "name", None) or str(project.id)),
            ("Current state", expected.replace("_", " ").title()),
            ("Result", target.replace("_", " ").title()),
            ("Affected", "1 installation project"),
        ],
        "state": {
            "project_id": str(project.id),
            "vendor_id": str(project.assigned_vendor_id),
            "from_status": project.status,
            "to_status": target,
            "updated_at": project.updated_at,
        },
    }


def stage_project_transition(
    db: Session,
    command: StageVendorProjectTransition,
) -> dict:
    """Stage one locked transition and its evidence in the caller transaction."""

    if not command.actor_id.strip() or not command.actor_type.strip():
        raise _error("actor_required", "Lifecycle transition actor is required.")
    preview = preview_project_lifecycle(
        db,
        PreviewVendorProjectLifecycle(
            project_id=command.project_id,
            vendor_id=command.vendor_id,
            action=command.action,
        ),
        for_update=True,
    )
    project = _project(db, command.project_id, for_update=True)
    previous = str(preview["state"]["from_status"])
    target = str(preview["state"]["to_status"])
    event_type = (
        EventType.vendor_project_started
        if command.action == "start"
        else EventType.vendor_project_completed
    )
    project.status = target
    domain_event = emit_event(
        db,
        event_type,
        {
            "schema_version": 1,
            "project_id": str(project.id),
            "native_project_id": str(project.project_id),
            "vendor_id": str(project.assigned_vendor_id),
            "from_status": previous,
            "to_status": target,
            "actor_type": command.actor_type,
            "actor_id": command.actor_id,
        },
        actor=command.actor_id,
        subscriber_id=project.subscriber_id,
        account_id=project.subscriber_id,
    )
    evidence = InstallationProjectLifecycleEvent(
        event_id=domain_event.event_id,
        project_id=project.id,
        vendor_id=project.assigned_vendor_id,
        event_type=domain_event.event_type.value,
        from_status=previous,
        to_status=target,
        actor_type=command.actor_type,
        actor_id=command.actor_id,
        occurred_at=domain_event.occurred_at,
    )
    db.add(evidence)
    db.flush()
    return {
        "id": project.id,
        "status": project.status,
        "lifecycle_event_id": str(evidence.id),
        "domain_event_id": str(domain_event.event_id),
        "transitioned_at": domain_event.occurred_at,
    }


def stage_staff_project_transition(
    db: Session,
    command: StageStaffVendorProjectTransition,
) -> dict:
    """Stage one locked staff verification/rework decision and its evidence."""

    normalized_actor = command.actor_id.strip()
    if not normalized_actor:
        raise VendorProjectLifecycleError(
            "actor_required",
            "Lifecycle transition actor is required",
            kind="invalid",
        )
    from app.services.vendor_portal_operations import (
        VendorPortalOperations,
        _serialize_project,
    )

    preview = VendorPortalOperations.preview_staff_project_lifecycle(
        db,
        command.project_id,
        action=command.action,
        reason=command.reason,
        for_update=True,
    )
    project = _project(db, command.project_id, for_update=True)
    previous = str(preview["state"]["from_status"])
    target = str(preview["state"]["to_status"])
    normalized_reason = preview["state"]["reason"]
    event_type = (
        EventType.vendor_project_verified
        if command.action == "verify"
        else EventType.vendor_project_rework_requested
    )
    project.status = target
    domain_event = emit_event(
        db,
        event_type,
        {
            "schema_version": 1,
            "project_id": str(project.id),
            "native_project_id": str(project.project_id),
            "vendor_id": str(project.assigned_vendor_id),
            "from_status": previous,
            "to_status": target,
            "actor_type": "staff_user",
            "actor_id": normalized_actor,
            "reason": normalized_reason,
            "verification_evidence": preview["state"]["verification_evidence"],
        },
        actor=normalized_actor,
        subscriber_id=project.subscriber_id,
        account_id=project.subscriber_id,
    )
    evidence = InstallationProjectLifecycleEvent(
        event_id=domain_event.event_id,
        project_id=project.id,
        vendor_id=project.assigned_vendor_id,
        event_type=domain_event.event_type.value,
        from_status=previous,
        to_status=target,
        actor_type="staff_user",
        actor_id=normalized_actor,
        reason=normalized_reason,
        decision_context=(
            {"verification_evidence": preview["state"]["verification_evidence"]}
            if command.action == "verify"
            else None
        ),
        occurred_at=domain_event.occurred_at,
    )
    db.add(evidence)
    db.flush()
    result = _serialize_project(project)
    result["lifecycle_event_id"] = str(evidence.id)
    result["domain_event_id"] = str(domain_event.event_id)
    result["transitioned_at"] = domain_event.occurred_at
    return result


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _stage_intake_transition(
    db: Session,
    *,
    project: InstallationProject,
    target: str,
    event_type: EventType,
    actor_id: str,
    reason: str | None,
    decision_context: dict,
) -> dict:
    """Shared tail for the two ways a draft project becomes vendor-visible."""

    from app.services.vendor_portal_operations import _serialize_project

    previous = project.status
    project.status = target
    domain_event = emit_event(
        db,
        event_type,
        {
            "schema_version": 1,
            "project_id": str(project.id),
            "native_project_id": str(project.project_id),
            "vendor_id": (
                str(project.assigned_vendor_id) if project.assigned_vendor_id else None
            ),
            "from_status": previous,
            "to_status": target,
            "actor_type": "staff_user",
            "actor_id": actor_id,
            "reason": reason,
            **decision_context,
        },
        actor=actor_id,
        subscriber_id=project.subscriber_id,
        account_id=project.subscriber_id,
    )
    evidence = InstallationProjectLifecycleEvent(
        event_id=domain_event.event_id,
        project_id=project.id,
        vendor_id=project.assigned_vendor_id,
        event_type=domain_event.event_type.value,
        from_status=previous,
        to_status=target,
        actor_type="staff_user",
        actor_id=actor_id,
        reason=reason,
        decision_context=decision_context or None,
        occurred_at=domain_event.occurred_at,
    )
    db.add(evidence)
    db.flush()
    result = _serialize_project(project)
    result["lifecycle_event_id"] = str(evidence.id)
    result["domain_event_id"] = str(domain_event.event_id)
    result["transitioned_at"] = domain_event.occurred_at
    return result


def stage_publish_for_bidding(
    db: Session,
    command: StagePublishForBidding,
) -> dict:
    """Open a drafted installation project to competitive vendor bidding.

    Publishing is the decision that makes work visible to vendors who were
    never assigned it, so the window is required rather than optional: the
    marketplace read and the quote-creation policy both refuse a project whose
    window is absent, and a project nobody can quote is not published.
    """

    actor = str(command.actor_id or "").strip()
    if not actor:
        raise _error("actor_required", "Lifecycle transition actor is required.")
    project = _project(db, command.project_id, for_update=True)
    if project.status != InstallationProjectStatus.draft.value:
        raise _error(
            "invalid_transition",
            "Only a draft project can be published for bidding.",
        )
    if project.assigned_vendor_id is not None:
        raise _error(
            "already_assigned",
            "A directly assigned project cannot be opened for bidding.",
        )
    opens_at = _as_utc(command.bidding_open_at)
    closes_at = _as_utc(command.bidding_close_at)
    if closes_at <= opens_at:
        raise _error(
            "invalid_bidding_window",
            "Bidding must close after it opens.",
        )
    project.bidding_open_at = opens_at
    project.bidding_close_at = closes_at
    project.assignment_type = VendorAssignmentType.bidding.value
    return _stage_intake_transition(
        db,
        project=project,
        target=InstallationProjectStatus.open_for_bidding.value,
        event_type=EventType.vendor_project_published,
        actor_id=actor,
        reason=None,
        decision_context={
            "assignment_type": VendorAssignmentType.bidding.value,
            "bidding_open_at": opens_at.isoformat(),
            "bidding_close_at": closes_at.isoformat(),
        },
    )


def stage_assign_vendor_directly(
    db: Session,
    command: StageAssignVendorDirectly,
) -> dict:
    """Direct one drafted installation project at one vendor without bidding.

    The assignment is the invitation, so no window applies. The vendor still
    quotes and the quote still needs staff approval — this decides *who may
    quote*, never what the work is worth.
    """

    actor = str(command.actor_id or "").strip()
    if not actor:
        raise _error("actor_required", "Lifecycle transition actor is required.")
    project = _project(db, command.project_id, for_update=True)
    if project.status != InstallationProjectStatus.draft.value:
        raise _error(
            "invalid_transition",
            "Only a draft project can be assigned to a vendor.",
        )
    vendor_uuid = coerce_uuid(command.vendor_id)
    vendor = db.get(Vendor, vendor_uuid)
    if vendor is None or not vendor.is_active:
        raise _error("vendor_not_found", "Vendor not found or inactive.")
    project.assigned_vendor_id = vendor_uuid
    project.assignment_type = VendorAssignmentType.direct.value
    return _stage_intake_transition(
        db,
        project=project,
        target=InstallationProjectStatus.assigned.value,
        event_type=EventType.vendor_project_assigned,
        actor_id=actor,
        reason=(command.reason or "").strip() or None,
        decision_context={
            "assignment_type": VendorAssignmentType.direct.value,
            "vendor_name": vendor.name,
        },
    )
