"""Vendor work lifecycle (start/complete) is owned by Sub, not the template.

The awarded vendor may move an approved project to in_progress and an
in_progress project to completed. The owner projects an Action and atomically
records actor/time/event evidence for every transition.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.event_store import EventStore
from app.models.project import Project
from app.models.vendor_routes import (
    InstallationProject,
    InstallationProjectLifecycleEvent,
    InstallationProjectLifecycleEventImmutableError,
    InstallationProjectStatus,
    Vendor,
)
from app.services.sot_registry.registry import service_relationship
from app.services.ui_contracts import Action
from app.services.vendor_portal_operations import _serialize_project
from app.services.vendor_project_lifecycle import (
    StageVendorProjectTransition,
    VendorProjectLifecycleError,
    stage_project_transition,
)


def _project_row(status: str, assigned_vendor_id: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        id="p1",
        project_id="prj1",
        project=SimpleNamespace(code="PRJ-1", name="Fiber install"),
        subscriber_id=None,
        assigned_vendor_id=assigned_vendor_id,
        assignment_type=None,
        status=status,
        bidding_open_at=None,
        bidding_close_at=None,
        approved_quote_id=None,
        procurement_system=None,
        procurement_order_reference=None,
        procurement_delivery_status=None,
        procurement_delivery_error=None,
        procurement_delivered_at=None,
        notes=None,
        created_at=None,
        updated_at=None,
    )


def test_start_action_only_when_approved_and_owned_by_viewer():
    approved = _project_row(InstallationProjectStatus.approved.value, "v1")
    action = _serialize_project(approved, viewer_vendor_id="v1")["lifecycle_action"]
    assert isinstance(action, Action)
    assert action.key == "start"
    assert action.requires_confirmation is True
    # Not the viewing vendor's project.
    assert (
        _serialize_project(approved, viewer_vendor_id="v2")["lifecycle_action"] is None
    )
    # No viewer context (e.g. an admin listing) never offers the action.
    assert _serialize_project(approved)["lifecycle_action"] is None
    # Wrong source status.
    quoted = _project_row(InstallationProjectStatus.quoted.value, "v1")
    assert _serialize_project(quoted, viewer_vendor_id="v1")["lifecycle_action"] is None


def test_complete_action_only_when_in_progress_and_owned_by_viewer():
    in_progress = _project_row(InstallationProjectStatus.in_progress.value, "v1")
    action = _serialize_project(in_progress, viewer_vendor_id="v1")["lifecycle_action"]
    assert isinstance(action, Action)
    assert action.key == "complete"
    assert (
        _serialize_project(in_progress, viewer_vendor_id="v2")["lifecycle_action"]
        is None
    )
    approved = _project_row(InstallationProjectStatus.approved.value, "v1")
    assert (
        _serialize_project(approved, viewer_vendor_id="v1")["lifecycle_action"].key
        == "start"
    )


def _install(db, status: str) -> tuple[InstallationProject, Vendor]:
    project = Project(name="Lifecycle fiber install")
    vendor = Vendor(
        name="Native Vendor",
        code=f"NV-{uuid4().hex[:6]}",
        supplier_reference=str(uuid4()),
    )
    db.add_all([project, vendor])
    db.flush()
    install = InstallationProject(
        project_id=project.id, assigned_vendor_id=vendor.id, status=status
    )
    db.add(install)
    db.commit()
    return install, vendor


def test_start_project_moves_approved_to_in_progress(db_session):
    install, vendor = _install(db_session, InstallationProjectStatus.approved.value)
    result = stage_project_transition(
        db_session,
        StageVendorProjectTransition(
            project_id=str(install.id),
            vendor_id=str(vendor.id),
            action="start",
            actor_id="vendor-user-1",
            actor_type="vendor_user",
        ),
    )
    assert result["status"] == InstallationProjectStatus.in_progress.value
    db_session.refresh(install)
    assert install.status == InstallationProjectStatus.in_progress.value
    assert (
        _serialize_project(install, viewer_vendor_id=str(vendor.id))[
            "lifecycle_action"
        ].key
        == "complete"
    )
    evidence = db_session.query(InstallationProjectLifecycleEvent).one()
    outbox = db_session.query(EventStore).one()
    assert evidence.event_id == outbox.event_id
    assert evidence.event_type == "vendor_project.started"
    assert evidence.actor_id == "vendor-user-1"
    assert evidence.from_status == InstallationProjectStatus.approved.value
    assert evidence.to_status == InstallationProjectStatus.in_progress.value
    assert evidence.occurred_at is not None
    assert outbox.actor == "vendor-user-1"


def test_complete_project_moves_in_progress_to_completed(db_session):
    install, vendor = _install(db_session, InstallationProjectStatus.in_progress.value)
    result = stage_project_transition(
        db_session,
        StageVendorProjectTransition(
            project_id=str(install.id),
            vendor_id=str(vendor.id),
            action="complete",
            actor_id="vendor-user-2",
            actor_type="vendor_user",
        ),
    )
    assert result["status"] == InstallationProjectStatus.completed.value
    db_session.refresh(install)
    assert (
        _serialize_project(install, viewer_vendor_id=str(vendor.id))["lifecycle_action"]
        is None
    )
    evidence = db_session.query(InstallationProjectLifecycleEvent).one()
    assert evidence.event_type == "vendor_project.completed"
    assert evidence.actor_id == "vendor-user-2"


def test_start_rejects_a_vendor_who_does_not_own_the_project(db_session):
    install, _vendor = _install(db_session, InstallationProjectStatus.approved.value)
    with pytest.raises(VendorProjectLifecycleError) as exc:
        stage_project_transition(
            db_session,
            StageVendorProjectTransition(
                project_id=str(install.id),
                vendor_id=str(uuid4()),
                action="start",
                actor_id="vendor-user-1",
                actor_type="vendor_user",
            ),
        )
    assert exc.value.code.endswith(".not_assigned")


def test_start_rejects_a_project_that_is_not_approved(db_session):
    install, vendor = _install(db_session, InstallationProjectStatus.quoted.value)
    with pytest.raises(VendorProjectLifecycleError) as exc:
        stage_project_transition(
            db_session,
            StageVendorProjectTransition(
                project_id=str(install.id),
                vendor_id=str(vendor.id),
                action="start",
                actor_id="vendor-user-1",
                actor_type="vendor_user",
            ),
        )
    assert exc.value.code.endswith(".invalid_transition")


def test_complete_rejects_a_project_that_is_not_in_progress(db_session):
    install, vendor = _install(db_session, InstallationProjectStatus.approved.value)
    with pytest.raises(VendorProjectLifecycleError) as exc:
        stage_project_transition(
            db_session,
            StageVendorProjectTransition(
                project_id=str(install.id),
                vendor_id=str(vendor.id),
                action="complete",
                actor_id="vendor-user-1",
                actor_type="vendor_user",
            ),
        )
    assert exc.value.code.endswith(".invalid_transition")


def test_lifecycle_evidence_is_append_only(db_session):
    install, vendor = _install(db_session, InstallationProjectStatus.approved.value)
    stage_project_transition(
        db_session,
        StageVendorProjectTransition(
            project_id=str(install.id),
            vendor_id=str(vendor.id),
            action="start",
            actor_id="vendor-user-1",
            actor_type="vendor_user",
        ),
    )
    evidence = db_session.query(InstallationProjectLifecycleEvent).one()
    evidence.actor_id = "rewritten"
    with pytest.raises(InstallationProjectLifecycleEventImmutableError):
        db_session.flush()
    db_session.rollback()


def test_lifecycle_routes_and_template_are_thin_action_adapters():
    root = Path(__file__).resolve().parents[1]
    routes = (root / "app/web/vendor_portal.py").read_text(encoding="utf-8")
    migration = (
        root / "alembic/versions/369_vendor_project_lifecycle_evidence.py"
    ).read_text(encoding="utf-8")
    template = (root / "templates/vendor/project_detail.html").read_text(
        encoding="utf-8"
    )
    sot = (root / "docs/SOT_RELATIONSHIP_MAP.md").read_text(encoding="utf-8")

    assert "issue_project_lifecycle" in routes
    assert "stage_project_transition(" not in routes
    assert "project.lifecycle_action.preview_url" in template
    assert "action_permitted(request, project.lifecycle_action)" in template
    assert 'onsubmit="return confirm(' not in template
    assert "operations.vendor_project_lifecycle" in sot
    assert "installation_project_lifecycle_events" in sot
    assert "installation_project_lifecycle_events_append_only" in migration
    assert "vendor_project.started" in sot
    assert "vendor_project.completed" in sot
    owner = service_relationship("operations.vendor_project_lifecycle")
    assert owner.module == "app.services.vendor_project_lifecycle"
