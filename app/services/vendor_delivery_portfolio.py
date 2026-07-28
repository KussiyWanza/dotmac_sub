"""Permission-scoped operational portfolio for one native vendor.

This resolver composes already-authoritative vendor project, delivery,
financial, and supply facts. It never writes, commits, or infers provider
outcomes from Dotmac decisions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload, selectinload

from app.models.project import Project
from app.models.vendor_routes import (
    InstallationProject,
    InstallationProjectStatus,
    ProjectQuote,
)
from app.schemas.status_presentation import StatusPresentation
from app.services.project_vendor_delivery import (
    ProjectVendorDeliveryProjection,
    ProjectVendorDeliveryVisibility,
    project_vendor_delivery_from_record,
)
from app.services.status_presentation import (
    installation_project_status_presentation,
)
from app.services.ui_contracts import Kpi, StateValue
from app.services.vendor_supply_views import (
    AdvanceView,
    MaterialReleaseView,
    latest_advances_for_projects,
    latest_material_releases_for_projects,
)


@dataclass(frozen=True, slots=True)
class VendorPortfolioQuery:
    vendor_id: UUID
    visibility: ProjectVendorDeliveryVisibility
    search: str | None = None
    status: InstallationProjectStatus | None = None
    limit: int = 25
    offset: int = 0


@dataclass(frozen=True, slots=True)
class VendorPortfolioStatusOption:
    value: InstallationProjectStatus
    presentation: StatusPresentation


@dataclass(frozen=True, slots=True)
class VendorPortfolioProject:
    project_id: UUID
    installation_project_id: UUID
    name: str
    code: str | None
    status: StatusPresentation
    updated_at: datetime
    detail_url: str
    delivery: ProjectVendorDeliveryProjection
    latest_material_release: MaterialReleaseView | None
    latest_advance: AdvanceView | None


@dataclass(frozen=True, slots=True)
class VendorDeliveryPortfolio:
    vendor_id: UUID
    items: tuple[VendorPortfolioProject, ...]
    kpis: tuple[Kpi, ...]
    status_options: tuple[VendorPortfolioStatusOption, ...]
    total: int
    limit: int
    offset: int
    has_previous: bool
    has_next: bool


_KPI_STATUSES = (
    InstallationProjectStatus.approved,
    InstallationProjectStatus.in_progress,
    InstallationProjectStatus.completed,
)


def _cohort_url(
    vendor_id: UUID,
    status: InstallationProjectStatus | None = None,
) -> str:
    base = f"/admin/vendors/{vendor_id}#delivery-portfolio"
    if status is None:
        return base
    return (
        f"/admin/vendors/{vendor_id}?project_status={status.value}#delivery-portfolio"
    )


def _kpis(
    vendor_id: UUID,
    counts: dict[str, int],
) -> tuple[Kpi, ...]:
    items = [
        Kpi(
            label="Assigned projects",
            value=StateValue.present(sum(counts.values())),
            cohort_url=_cohort_url(vendor_id),
            unit="projects",
        )
    ]
    for status in _KPI_STATUSES:
        presentation = installation_project_status_presentation(status)
        items.append(
            Kpi(
                label=f"{presentation.label} projects",
                value=StateValue.present(counts.get(status.value, 0)),
                cohort_url=_cohort_url(vendor_id, status),
                tone=presentation.tone,
                icon=presentation.icon,
                unit="projects",
            )
        )
    return tuple(items)


def _status_counts(db: Session, vendor_id: UUID) -> dict[str, int]:
    rows = (
        db.query(InstallationProject.status, func.count(InstallationProject.id))
        .join(Project, InstallationProject.project_id == Project.id)
        .filter(
            InstallationProject.assigned_vendor_id == vendor_id,
            InstallationProject.is_active.is_(True),
            Project.is_active.is_(True),
        )
        .group_by(InstallationProject.status)
        .all()
    )
    return {str(status): int(count) for status, count in rows}


def _filtered_query(db: Session, query: VendorPortfolioQuery):
    rows = (
        db.query(InstallationProject)
        .join(Project, InstallationProject.project_id == Project.id)
        .filter(
            InstallationProject.assigned_vendor_id == query.vendor_id,
            InstallationProject.is_active.is_(True),
            Project.is_active.is_(True),
        )
    )
    search = str(query.search or "").strip()
    if search:
        pattern = f"%{search}%"
        rows = rows.filter(
            or_(
                Project.name.ilike(pattern),
                Project.code.ilike(pattern),
                Project.number.ilike(pattern),
            )
        )
    if query.status is not None:
        rows = rows.filter(InstallationProject.status == query.status.value)
    return rows


def get_vendor_delivery_portfolio(
    db: Session,
    query: VendorPortfolioQuery,
) -> VendorDeliveryPortfolio:
    """Return one stable, paginated vendor portfolio from committed facts."""

    normalized_limit = max(10, min(int(query.limit), 100))
    normalized_offset = max(0, int(query.offset))
    filtered = _filtered_query(db, query)
    total = int(
        filtered.with_entities(func.count(InstallationProject.id)).scalar() or 0
    )
    rows = (
        filtered.options(
            joinedload(InstallationProject.project),
            joinedload(InstallationProject.assigned_vendor),
            selectinload(InstallationProject.quotes).joinedload(ProjectQuote.vendor),
            selectinload(InstallationProject.quotes).selectinload(
                ProjectQuote.route_revisions
            ),
            selectinload(InstallationProject.as_built_routes),
            selectinload(InstallationProject.purchase_invoices),
        )
        .order_by(
            InstallationProject.updated_at.desc(),
            InstallationProject.id.desc(),
        )
        .offset(normalized_offset)
        .limit(normalized_limit)
        .all()
    )
    installation_ids = tuple(row.id for row in rows)
    materials = (
        latest_material_releases_for_projects(
            db,
            project_ids=installation_ids,
            vendor_id=query.vendor_id,
        )
        if query.visibility.can_read_operations
        else ()
    )
    advances = (
        latest_advances_for_projects(
            db,
            project_ids=installation_ids,
            vendor_id=query.vendor_id,
        )
        if query.visibility.can_read_financials
        else ()
    )
    materials_by_project = {item.project.id: item for item in materials}
    advances_by_project = {item.project.id: item for item in advances}

    items: list[VendorPortfolioProject] = []
    for row in rows:
        delivery = project_vendor_delivery_from_record(
            row,
            visibility=query.visibility,
        )
        if delivery is None:
            continue
        native = row.project
        items.append(
            VendorPortfolioProject(
                project_id=native.id,
                installation_project_id=row.id,
                name=native.name,
                code=native.number or native.code,
                status=installation_project_status_presentation(row.status),
                updated_at=row.updated_at,
                detail_url=f"/admin/projects/{native.number or native.id}",
                delivery=delivery,
                latest_material_release=materials_by_project.get(row.id),
                latest_advance=advances_by_project.get(row.id),
            )
        )

    counts = _status_counts(db, query.vendor_id)
    return VendorDeliveryPortfolio(
        vendor_id=query.vendor_id,
        items=tuple(items),
        kpis=_kpis(query.vendor_id, counts),
        status_options=tuple(
            VendorPortfolioStatusOption(
                value=status,
                presentation=installation_project_status_presentation(status),
            )
            for status in InstallationProjectStatus
        ),
        total=total,
        limit=normalized_limit,
        offset=normalized_offset,
        has_previous=normalized_offset > 0,
        has_next=normalized_offset + len(rows) < total,
    )
