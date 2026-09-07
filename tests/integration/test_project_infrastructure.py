"""Real migrated PostgreSQL contract for project infrastructure referents."""

from uuid import uuid4

import pytest
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.network_monitoring import PopSite
from app.models.project import ProjectInfrastructure, ProjectTemplate, ProjectType
from app.models.vendor_routes import InstallationProject
from app.schemas.infrastructure import InfrastructureReference, InfrastructureType
from app.schemas.project import ProjectCreate, ProjectUpdate
from app.services.projects import projects


def test_migrated_customerless_project_scope_is_idempotent(db_session: Session) -> None:
    site = PopSite(name="Migrated cable rerun site")
    template = ProjectTemplate(
        name="Migrated rerun",
        project_type="cable_rerun",
        creates_vendor_assignment_scope=True,
    )
    db_session.add_all([site, template])
    db_session.commit()
    reference = InfrastructureReference(type=InfrastructureType.location, id=site.id)
    project = projects.create(
        db_session,
        ProjectCreate(
            name="Migrated rerun",
            project_type=ProjectType.cable_rerun,
            project_template_id=template.id,
            infrastructure=reference,
        ),
    )
    projects.update(
        db_session, str(project.id), ProjectUpdate(infrastructure=reference)
    )
    scopes = db_session.scalars(
        select(InstallationProject).where(InstallationProject.project_id == project.id)
    ).all()
    assert len(scopes) == 1
    assert scopes[0].subscriber_id is None
    assert project.infrastructure.id == site.id


def test_migrated_schema_rejects_missing_and_multiple_targets(
    db_session: Session,
) -> None:
    project = projects.create(
        db_session,
        ProjectCreate(name="Constraint test", project_type=ProjectType.cable_rerun),
    )
    site = PopSite(name="Constraint site")
    db_session.add(site)
    db_session.flush()
    for targets in (
        {},
        {"location_id": uuid4()},
        {"location_id": site.id, "base_station_id": site.id},
    ):
        with pytest.raises(IntegrityError), db_session.begin_nested():
            db_session.execute(
                insert(ProjectInfrastructure).values(project_id=project.id, **targets)
            )
