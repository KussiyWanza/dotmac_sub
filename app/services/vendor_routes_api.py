"""GeoJSON read service for the native vendor fiber routes (maps §C).

Serves ``proposed_route_revisions.route_geom`` and ``as_built_routes.route_geom``
(LINESTRING, SRID 4326 — ``app/models/vendor_routes.py``) as GeoJSON for the
admin vendor route-view map. Mirrors ``fiber_plant_api``'s ``ST_AsGeoJSON``
pattern: the loaded geometry value is passed back through ``ST_AsGeoJSON`` and
parsed into a GeoJSON geometry dict.
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.vendor_routes import (
    AsBuiltRoute,
    InstallationProject,
    ProjectQuote,
    ProposedRouteRevision,
)

logger = logging.getLogger(__name__)


def _geom_to_geojson(db: Session, geom) -> dict | None:
    """Convert a loaded geometry value to a GeoJSON geometry dict."""
    if geom is None:
        return None
    try:
        raw = db.query(func.ST_AsGeoJSON(geom)).scalar()
    except Exception:  # pragma: no cover - defensive (non-PostGIS backends)
        logger.warning("ST_AsGeoJSON failed for route geometry", exc_info=True)
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _project_label(project: InstallationProject | None) -> str:
    if project is None:
        return "Unknown project"
    native = getattr(project, "project", None)
    if native is not None and getattr(native, "name", None):
        return native.name
    if project.procurement_order_reference:
        return project.procurement_order_reference
    return f"Project {str(project.id)[:8]}"


def build_project_route_geojson(db: Session, project_id: str) -> dict:
    """All proposed + as-built routes for an installation project as GeoJSON.

    Returns a ``FeatureCollection``; each feature carries a ``kind`` property of
    ``proposed`` or ``as_built`` plus revision/status metadata.
    """
    features: list[dict] = []

    revisions = (
        db.query(ProposedRouteRevision)
        .join(ProjectQuote, ProposedRouteRevision.quote_id == ProjectQuote.id)
        .filter(ProjectQuote.project_id == project_id)
        .filter(ProposedRouteRevision.route_geom.isnot(None))
        .order_by(ProposedRouteRevision.revision_number.asc())
        .all()
    )
    for revision in revisions:
        geometry = _geom_to_geojson(db, revision.route_geom)
        if geometry is None:
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": geometry,
                "properties": {
                    "id": str(revision.id),
                    "kind": "proposed",
                    "quote_id": str(revision.quote_id),
                    "revision_number": revision.revision_number,
                    "status": revision.status,
                    "length_meters": revision.length_meters,
                },
            }
        )

    as_builts = (
        db.query(AsBuiltRoute)
        .filter(AsBuiltRoute.project_id == project_id)
        .filter(AsBuiltRoute.route_geom.isnot(None))
        .order_by(AsBuiltRoute.version.asc())
        .all()
    )
    for as_built in as_builts:
        geometry = _geom_to_geojson(db, as_built.route_geom)
        if geometry is None:
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": geometry,
                "properties": {
                    "id": str(as_built.id),
                    "kind": "as_built",
                    "status": as_built.status,
                    "version": as_built.version,
                    "length_meters": as_built.actual_length_meters,
                    "variation_type": as_built.variation_type,
                },
            }
        )

    return {"type": "FeatureCollection", "features": features}


def build_as_built_route_geojson(db: Session, as_built_id: str) -> dict:
    """One as-built submission as a permission-scoped review map."""

    row = db.query(AsBuiltRoute).filter(AsBuiltRoute.id == as_built_id).one_or_none()
    if row is None:
        return {"type": "FeatureCollection", "features": []}
    geometry = _geom_to_geojson(db, row.route_geom)
    if geometry is None:
        return {"type": "FeatureCollection", "features": []}
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": geometry,
                "properties": {
                    "id": str(row.id),
                    "kind": "as_built",
                    "status": row.status,
                    "version": row.version,
                    "length_meters": row.actual_length_meters,
                    "variation_type": row.variation_type,
                },
            }
        ],
    }


def get_route_project(db: Session, project_id: str) -> dict | None:
    """Summary of a single installation project for the route-view page header."""
    project = (
        db.query(InstallationProject)
        .filter(InstallationProject.id == project_id)
        .one_or_none()
    )
    if project is None:
        return None
    vendor = getattr(project, "assigned_vendor", None)
    return {
        "id": str(project.id),
        "label": _project_label(project),
        "status": project.status,
        "vendor": vendor.name if vendor is not None else None,
    }


def list_route_projects(db: Session) -> list[dict]:
    """Installation projects that carry any saved route geometry."""
    proposed_project_ids = {
        row[0]
        for row in (
            db.query(ProjectQuote.project_id)
            .join(
                ProposedRouteRevision,
                ProposedRouteRevision.quote_id == ProjectQuote.id,
            )
            .filter(ProposedRouteRevision.route_geom.isnot(None))
            .distinct()
            .all()
        )
    }
    as_built_project_ids = {
        row[0]
        for row in (
            db.query(AsBuiltRoute.project_id)
            .filter(AsBuiltRoute.route_geom.isnot(None))
            .distinct()
            .all()
        )
    }
    project_ids = proposed_project_ids | as_built_project_ids
    if not project_ids:
        return []

    projects = (
        db.query(InstallationProject)
        .filter(InstallationProject.id.in_(project_ids))
        .all()
    )
    items = [
        {
            "id": str(project.id),
            "label": _project_label(project),
            "status": project.status,
            "vendor": (
                project.assigned_vendor.name
                if getattr(project, "assigned_vendor", None) is not None
                else None
            ),
            "has_proposed": project.id in proposed_project_ids,
            "has_as_built": project.id in as_built_project_ids,
        }
        for project in projects
    ]
    items.sort(key=lambda item: str(item["label"]).lower())
    return items
