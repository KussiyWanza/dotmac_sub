"""Transaction-neutral owner for installation scope creation.

Work reaches a vendor through three origins, all landing on the same
``InstallationProject`` root so the award -> quote -> as-built -> PO -> payment
chain downstream has exactly one shape:

* **Sale** — ``ensure_for_project`` scopes an installation sold to a
  subscriber. ``sales.fulfillment`` owns that trigger.
* **Infrastructure maintenance** — ``ensure_for_project`` also scopes a
  vendor-enabled, infrastructure-linked project without requiring a subscriber.
  ``operations.project_lifecycle`` owns that trigger.
* **Buildout** — ``ensure_for_buildout`` scopes plant we decided to build.
  There is no subscriber, quote, or sales order; the ``BuildoutProject`` is the
  reason the work exists.

``InstallationProject.subscriber_id`` is nullable and ``buildout_project_id``
already FKs to ``buildout_projects``, so this is one owner with two entry
points, not a second scope model.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.project import Project
from app.models.qualification import BuildoutProject
from app.models.vendor_routes import InstallationProject, InstallationProjectStatus
from app.services import projects as projects_service
from app.services.domain_errors import DomainError
from app.services.events import EventType, emit_event


class InstallationScopeError(DomainError):
    def __init__(self, code: str, message: str, *, kind: str = "conflict") -> None:
        super().__init__(code=code, message=message)
        self.kind = kind


@dataclass(frozen=True, slots=True)
class EnsureProjectScope:
    project_id: UUID
    subscriber_id: UUID | None
    actor_id: str
    created_by_person_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ProjectScopeOutcome:
    installation_project_id: UUID
    project_id: UUID
    created: bool


def ensure_for_project(
    db: Session, *, command: EnsureProjectScope
) -> ProjectScopeOutcome:
    project_id = command.project_id
    subscriber_id = command.subscriber_id
    actor_id = command.actor_id
    created_by_person_id = command.created_by_person_id
    actor = str(actor_id or "").strip()
    if not actor:
        raise InstallationScopeError(
            "actor_required", "Installation-scope actor is required", kind="invalid"
        )
    project = db.scalar(
        select(Project).where(Project.id == project_id).with_for_update()
    )
    if project is None or not project.is_active:
        raise InstallationScopeError(
            "project_not_found", "Project not found", kind="not_found"
        )
    if project.subscriber_id != subscriber_id:
        raise InstallationScopeError(
            "subscriber_mismatch", "Project and installation Subscriber differ"
        )
    existing = db.scalars(
        select(InstallationProject)
        .where(InstallationProject.project_id == project_id)
        .with_for_update()
    ).one_or_none()
    if existing is not None:
        if existing.subscriber_id != subscriber_id or not existing.is_active:
            raise InstallationScopeError(
                "existing_scope_mismatch",
                "Installation project conflicts with the Project Subscriber",
            )
        return ProjectScopeOutcome(existing.id, project.id, False)
    if subscriber_id is None:
        template = project.project_template
        if (
            project.infrastructure is None
            or template is None
            or not template.creates_vendor_assignment_scope
        ):
            raise InstallationScopeError(
                "scope_required",
                "Select infrastructure and a vendor-enabled template before creating a vendor scope.",
                kind="invalid",
            )
    installation = InstallationProject(
        project_id=project.id,
        subscriber_id=subscriber_id,
        status=InstallationProjectStatus.draft.value,
        created_by_person_id=created_by_person_id,
        notes="Created by operations.installation_scope from the native project scope",
    )
    db.add(installation)
    db.flush()
    emit_event(
        db,
        EventType.installation_scope_created,
        {
            "installation_project_id": str(installation.id),
            "project_id": str(project.id),
            "subscriber_id": str(subscriber_id) if subscriber_id else None,
        },
        actor=actor,
        subscriber_id=subscriber_id,
    )
    return ProjectScopeOutcome(installation.id, project.id, True)


DEFAULT_BUILDOUT_PROJECT_TYPE = "fiber_optics_installation"


def ensure_for_buildout(
    db: Session,
    *,
    buildout_project_id: UUID,
    actor_id: str,
    name: str | None = None,
    project_type: str = DEFAULT_BUILDOUT_PROJECT_TYPE,
    created_by_person_id: UUID | None = None,
) -> InstallationProject:
    """Idempotently scope one BuildoutProject as vendor-executable work.

    This is the entry point sales does not provide: plant we build ourselves,
    with no subscriber behind it. It mints the native project root through
    ``operations.project_lifecycle`` and roots one installation project on it,
    so every downstream vendor decision — bidding, quoting, as-built evidence,
    PO, verification — runs the identical path a sold installation runs.
    """

    actor = str(actor_id or "").strip()
    if not actor:
        raise InstallationScopeError(
            "actor_required", "Installation-scope actor is required", kind="invalid"
        )
    buildout = db.get(BuildoutProject, buildout_project_id)
    if buildout is None:
        raise InstallationScopeError(
            "buildout_project_not_found",
            "Buildout project not found",
            kind="not_found",
        )
    existing = db.scalars(
        select(InstallationProject).where(
            InstallationProject.buildout_project_id == buildout_project_id
        )
    ).one_or_none()
    if existing is not None:
        return existing
    project = projects_service.prepare_buildout_project(
        db,
        buildout_project_id=buildout_project_id,
        name=name or f"Buildout — {buildout_project_id}",
        project_type=project_type,
        actor_id=actor,
    )
    installation = InstallationProject(
        project_id=project.id,
        buildout_project_id=buildout_project_id,
        subscriber_id=None,
        status=InstallationProjectStatus.draft.value,
        created_by_person_id=created_by_person_id,
        notes="Created by operations.installation_scope from a buildout project",
    )
    db.add(installation)
    db.flush()
    emit_event(
        db,
        EventType.installation_scope_created,
        {
            "installation_project_id": str(installation.id),
            "project_id": str(project.id),
            "buildout_project_id": str(buildout_project_id),
            "subscriber_id": None,
        },
        actor=actor,
    )
    return installation
