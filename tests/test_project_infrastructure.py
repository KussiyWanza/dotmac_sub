"""Fast behavior checks; migrated PostgreSQL acceptance is a separate lane."""

from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network_monitoring import DeviceType, NetworkDevice, PopSite
from app.models.project import Project, ProjectTemplate, ProjectType
from app.models.vendor_routes import InstallationProject
from app.schemas.infrastructure import (
    InfrastructureReference,
    InfrastructureSearch,
    InfrastructureType,
)
from app.schemas.project import ProjectCreate, ProjectRead, ProjectUpdate
from app.services import infrastructure_catalogue, web_customer_lists, web_projects
from app.services.projects import ProjectServiceError, projects


@pytest.fixture
def infrastructure_scope(db_session: Session) -> tuple[ProjectTemplate, PopSite]:
    template = ProjectTemplate(
        name="Cable Rerun",
        project_type="cable_rerun",
        creates_vendor_assignment_scope=True,
    )
    site = PopSite(name="Test Base Station", zabbix_group_id="project-infra-test")
    db_session.add_all([template, site])
    db_session.commit()
    return template, site


def _create(db: Session, template: ProjectTemplate, site: PopSite | None) -> Project:
    return projects.create(
        db,
        ProjectCreate(
            name="Infrastructure rerun",
            project_type=ProjectType.cable_rerun,
            project_template_id=template.id,
            infrastructure=InfrastructureReference(
                type=InfrastructureType.base_station, id=site.id
            )
            if site
            else None,
        ),
    )


def test_customerless_rerun_creates_scope_and_serializes_reference(
    db_session, infrastructure_scope
):
    template, site = infrastructure_scope
    project = _create(db_session, template, site)
    scope = db_session.scalar(
        select(InstallationProject).where(InstallationProject.project_id == project.id)
    )
    assert scope is not None
    assert scope.subscriber_id is None
    assert scope.status == "draft"
    assert ProjectRead.model_validate(
        project
    ).infrastructure == InfrastructureReference(
        type=InfrastructureType.base_station, id=site.id
    )


def test_edit_selected_infrastructure_repairs_scope_once(
    db_session, infrastructure_scope
):
    template, site = infrastructure_scope
    project = _create(db_session, template, None)
    payload = ProjectUpdate(
        infrastructure=InfrastructureReference(
            type=InfrastructureType.base_station, id=site.id
        )
    )
    projects.update(db_session, str(project.id), payload)
    first = db_session.scalar(
        select(InstallationProject).where(InstallationProject.project_id == project.id)
    )
    assert first is not None
    first_id = first.id
    projects.update(db_session, str(project.id), payload)
    scopes = db_session.scalars(
        select(InstallationProject).where(InstallationProject.project_id == project.id)
    ).all()
    assert [row.id for row in scopes] == [first_id]


def test_invalid_reference_rolls_back_project_creation(
    db_session, infrastructure_scope
):
    template, _ = infrastructure_scope
    with pytest.raises(ProjectServiceError, match="available infrastructure"):
        projects.create(
            db_session,
            ProjectCreate(
                name="Invalid infrastructure rerun",
                project_type=ProjectType.cable_rerun,
                project_template_id=template.id,
                infrastructure=InfrastructureReference(
                    type=InfrastructureType.cabinet, id=uuid4()
                ),
            ),
        )
    assert (
        db_session.scalar(
            select(Project).where(Project.name == "Invalid infrastructure rerun")
        )
        is None
    )


def test_wrong_kind_reference_is_rejected(db_session, infrastructure_scope):
    template, site = infrastructure_scope
    with pytest.raises(ProjectServiceError, match="available infrastructure"):
        projects.create(
            db_session,
            ProjectCreate(
                name="Wrong kind",
                project_type=ProjectType.cable_rerun,
                project_template_id=template.id,
                infrastructure=InfrastructureReference(
                    type=InfrastructureType.access_point, id=site.id
                ),
            ),
        )


def test_draft_scope_cannot_lose_its_only_referent(db_session, infrastructure_scope):
    template, site = infrastructure_scope
    project = _create(db_session, template, site)
    with pytest.raises(ProjectServiceError, match="Keep an infrastructure"):
        projects.update(db_session, str(project.id), ProjectUpdate(infrastructure=None))
    db_session.refresh(project)
    assert project.infrastructure.id == site.id


def test_published_scope_cannot_be_retargeted(db_session, infrastructure_scope):
    template, site = infrastructure_scope
    project = _create(db_session, template, site)
    other = PopSite(name="Other station", zabbix_group_id="project-infra-other")
    db_session.add(other)
    scope = db_session.scalar(
        select(InstallationProject).where(InstallationProject.project_id == project.id)
    )
    scope.status = "open_for_bidding"
    db_session.commit()
    with pytest.raises(ProjectServiceError, match="assigned or published"):
        projects.update(
            db_session,
            str(project.id),
            ProjectUpdate(
                infrastructure=InfrastructureReference(
                    type=InfrastructureType.base_station, id=other.id
                )
            ),
        )


def test_unrelated_edit_preserves_infrastructure(db_session, infrastructure_scope):
    template, site = infrastructure_scope
    project = _create(db_session, template, site)
    project = projects.update(
        db_session,
        str(project.id),
        ProjectUpdate(description="Revised work instructions"),
    )
    assert project.infrastructure.id == site.id


def test_scope_without_template_stays_unassigned_and_can_be_cleared(
    db_session, infrastructure_scope
):
    _, site = infrastructure_scope
    project = projects.create(
        db_session,
        ProjectCreate(
            name="Planning",
            project_type=ProjectType.cable_rerun,
            infrastructure=InfrastructureReference(
                type=InfrastructureType.location, id=site.id
            ),
        ),
    )
    assert (
        db_session.scalar(
            select(InstallationProject).where(
                InstallationProject.project_id == project.id
            )
        )
        is None
    )
    project = projects.update(
        db_session, str(project.id), ProjectUpdate(infrastructure=None)
    )
    assert project.infrastructure is None


def test_catalogue_matches_customer_lookup_and_excludes_inactive_ap(
    db_session, infrastructure_scope
):
    _, site = infrastructure_scope
    active = NetworkDevice(
        name="Target Active AP",
        device_type=DeviceType.access_point,
        pop_site_id=site.id,
        is_active=True,
    )
    inactive = NetworkDevice(
        name="Target Inactive AP",
        device_type=DeviceType.access_point,
        pop_site_id=site.id,
        is_active=False,
    )
    db_session.add_all([active, inactive])
    db_session.commit()
    query = InfrastructureSearch(type=InfrastructureType.access_point, query="Target")
    result = infrastructure_catalogue.search(db_session, query=query)
    customer = web_customer_lists.search_customer_infrastructure_options(
        db_session, infrastructure_type="access_point", query="Target"
    )
    assert [row.id for row in result.results] == [active.id]
    assert [row.id for row in customer] == [active.id]
    assert (
        infrastructure_catalogue.resolve(
            db_session,
            reference=InfrastructureReference(
                type=InfrastructureType.access_point, id=inactive.id
            ),
        )
        is None
    )
    assert (
        infrastructure_catalogue.search(
            db_session,
            query=InfrastructureSearch(type=InfrastructureType.access_point, query="T"),
        ).results
        == ()
    )


def test_form_reference_round_trip_and_partial_selection(
    db_session, infrastructure_scope
):
    template, site = infrastructure_scope
    project = web_projects.create_project_from_form(
        db_session,
        request=None,
        actor_id=None,
        name="Form rerun",
        project_type="cable_rerun",
        project_template_id=str(template.id),
        infrastructure_type="base_station",
        infrastructure_id=str(site.id),
    )
    context = web_projects.build_project_form_context(db_session, project=project)
    assert context["infrastructure_editor"]["selected"]["label"] == site.name
    with pytest.raises(ValueError, match="Choose an infrastructure"):
        web_projects.create_project_from_form(
            db_session,
            request=None,
            actor_id=None,
            name="Partial selection",
            infrastructure_type="base_station",
            infrastructure_id="",
        )
