"""PostgreSQL regressions for vendor project workspace reads."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.models.project import Project
from app.models.vendor_routes import (
    InstallationProject,
    InstallationProjectStatus,
    ProjectQuote,
    Vendor,
)
from app.services.vendor_portal_operations import vendor_portal_operations


def test_project_lists_handle_json_projects_without_duplicate_rows(db_session):
    """Project JSON fields must not make either vendor dashboard list fail."""
    now = datetime.now(UTC)
    vendor = Vendor(name="Dashboard Vendor", code=f"DV-{uuid4().hex[:8]}")
    available_project = Project(
        name="Available vendor project",
        tags=["vendor", "available"],
        metadata_={"source": "postgres-regression"},
    )
    quoted_project = Project(
        name="Quoted vendor project",
        tags=["vendor", "quoted"],
        metadata_={"source": "postgres-regression"},
    )
    db_session.add_all([vendor, available_project, quoted_project])
    db_session.flush()

    available_installation = InstallationProject(
        project_id=available_project.id,
        status=InstallationProjectStatus.open_for_bidding.value,
        bidding_open_at=now - timedelta(hours=1),
        bidding_close_at=now + timedelta(hours=1),
    )
    quoted_installation = InstallationProject(
        project_id=quoted_project.id,
        status=InstallationProjectStatus.quoted.value,
    )
    db_session.add_all([available_installation, quoted_installation])
    db_session.flush()
    db_session.add_all(
        [
            ProjectQuote(project_id=quoted_installation.id, vendor_id=vendor.id),
            ProjectQuote(project_id=quoted_installation.id, vendor_id=vendor.id),
        ]
    )
    db_session.flush()

    available_rows = vendor_portal_operations.list_projects(
        db_session,
        str(vendor.id),
        available=True,
        limit=50,
        offset=0,
    )
    quoted_rows = vendor_portal_operations.list_projects(
        db_session,
        str(vendor.id),
        available=False,
        limit=50,
        offset=0,
    )

    assert [row["id"] for row in available_rows] == [available_installation.id]
    assert [row["id"] for row in quoted_rows] == [quoted_installation.id]
