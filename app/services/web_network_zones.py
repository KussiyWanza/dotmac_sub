"""Service helpers for admin network zone web routes."""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.sql.elements import ColumnElement
from starlette.datastructures import FormData

from app.models.gis import GeoArea
from app.models.network import FdhCabinet, NetworkZone, OntUnit, Splitter
from app.services import network as network_service

logger = logging.getLogger(__name__)


def _form_str(form: FormData, key: str, default: str = "") -> str:
    value = form.get(key, default)
    return value.strip() if isinstance(value, str) else default


def list_page_data(
    db: Session,
    status: str | None = None,
    *,
    search: str | None = None,
    page: int = 1,
    per_page: int = 25,
) -> dict[str, object]:
    """Return zone list with summary stats."""
    status_filter = (status or "all").strip().lower()
    search_filter = (search or "").strip()
    filters: list[ColumnElement[bool]] = []
    if status_filter == "active":
        filters.append(NetworkZone.is_active.is_(True))
    elif status_filter == "inactive":
        filters.append(NetworkZone.is_active.is_(False))
    if search_filter:
        pattern = f"%{search_filter}%"
        filters.append(
            NetworkZone.name.ilike(pattern) | NetworkZone.description.ilike(pattern)
        )

    count_stmt = select(func.count(NetworkZone.id))
    if filters:
        count_stmt = count_stmt.where(*filters)
    filtered_total = db.scalar(count_stmt) or 0
    total_pages = max(1, (filtered_total + per_page - 1) // per_page)
    page = min(page, total_pages)

    ont_count = (
        select(func.count(OntUnit.id))
        .where(OntUnit.zone_id == NetworkZone.id)
        .correlate(NetworkZone)
        .scalar_subquery()
    )
    splitter_count = (
        select(func.count(Splitter.id))
        .where(Splitter.zone_id == NetworkZone.id)
        .correlate(NetworkZone)
        .scalar_subquery()
    )
    fdh_count = (
        select(func.count(FdhCabinet.id))
        .where(FdhCabinet.zone_id == NetworkZone.id)
        .correlate(NetworkZone)
        .scalar_subquery()
    )
    # The GeoArea binding is joined into this same statement: the list has a
    # bounded-query contract, so it reports the binding declared on each zone.
    # Inherited and stale resolution stay on the detail page, which resolves
    # the full parent chain through the zones owner.
    stmt = (
        select(
            NetworkZone,
            ont_count,
            splitter_count,
            fdh_count,
            GeoArea.name,
            GeoArea.is_active,
        )
        .outerjoin(GeoArea, GeoArea.id == NetworkZone.geo_area_id)
        .options(joinedload(NetworkZone.parent))
        .order_by(NetworkZone.name)
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    if filters:
        stmt = stmt.where(*filters)
    rows = db.execute(stmt).all()
    zones = [row[0] for row in rows]
    zone_stats = {
        str(row[0].id): {
            "ont_count": row[1],
            "splitter_count": row[2],
            "fdh_count": row[3],
        }
        for row in rows
    }
    zone_geo_areas = {
        str(row[0].id): {
            "bound": row[0].geo_area_id is not None,
            "name": row[4] if row[5] else None,
            "unavailable": row[0].geo_area_id is not None and not row[5],
        }
        for row in rows
    }

    stats_row = db.execute(
        select(
            func.count(NetworkZone.id),
            func.count(NetworkZone.id).filter(NetworkZone.is_active.is_(True)),
            func.count(NetworkZone.id).filter(NetworkZone.is_active.is_(False)),
        )
    ).one()

    return {
        "zones": zones,
        "zone_geo_areas": zone_geo_areas,
        "zone_stats": zone_stats,
        "status_filter": status_filter,
        "search": search_filter,
        "stats": {
            "total": stats_row[0],
            "active": stats_row[1],
            "inactive": stats_row[2],
        },
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total": filtered_total,
            "total_pages": total_pages,
        },
    }


def detail_page_data(db: Session, zone_id: str) -> dict[str, object] | None:
    """Return zone detail page payload."""
    zone = network_service.network_zones.get_or_none(db, zone_id)
    if not zone:
        return None

    # Child zones
    children = network_service.network_zones.list(
        db, parent_id=str(zone.id), is_active=True
    )

    # Parent chain
    parent = None
    if zone.parent_id:
        parent = network_service.network_zones.get_or_none(db, str(zone.parent_id))

    # Associated infrastructure counts
    ont_count = (
        db.scalar(
            select(func.count()).select_from(OntUnit).where(OntUnit.zone_id == zone.id)
        )
        or 0
    )
    splitter_count = (
        db.scalar(
            select(func.count())
            .select_from(Splitter)
            .where(Splitter.zone_id == zone.id)
        )
        or 0
    )
    fdh_count = (
        db.scalar(
            select(func.count())
            .select_from(FdhCabinet)
            .where(FdhCabinet.zone_id == zone.id)
        )
        or 0
    )

    geo_area = (
        db.get(GeoArea, zone.geo_area_id) if zone.geo_area_id is not None else None
    )
    effective = network_service.network_zones.resolve_geo_area(db, zone.id)
    effective_geo_area = (
        db.get(GeoArea, effective.geo_area_id)
        if effective.geo_area_id is not None
        else None
    )

    return {
        "zone": zone,
        "parent": parent,
        "children": children,
        "geo_area": geo_area,
        "effective_geo_resolution": effective,
        "effective_geo_area": effective_geo_area,
        "infra_stats": {
            "ont_count": ont_count,
            "splitter_count": splitter_count,
            "fdh_count": fdh_count,
        },
    }


def build_form_context(
    db: Session,
    *,
    zone: NetworkZone | None,
    action_url: str,
    error: str | None = None,
) -> dict[str, object]:
    """Build form context for create/edit."""
    # Available parent zones (exclude self for edit)
    parent_zones = network_service.network_zones.list(db, is_active=True)
    if zone:
        parent_zones = [z for z in parent_zones if z.id != zone.id]

    geo_areas = db.scalars(
        select(GeoArea).where(GeoArea.is_active.is_(True)).order_by(GeoArea.name)
    ).all()

    context: dict[str, object] = {
        "zone": zone,
        "parent_zones": parent_zones,
        "geo_areas": geo_areas,
        "action_url": action_url,
    }
    if error:
        context["error"] = error
    return context


def edit_form_context(
    db: Session,
    *,
    zone_id: str,
    error: str | None = None,
) -> dict[str, object] | None:
    """Build edit form context, returning None when the zone does not exist."""
    zone = network_service.network_zones.get_or_none(db, zone_id)
    if not zone:
        return None
    return build_form_context(
        db,
        zone=zone,
        action_url=f"/admin/network/zones/{zone.id}",
        error=error,
    )


def parse_form_values(form: FormData) -> dict[str, object]:
    """Parse zone form fields into normalized values."""
    lat_str = _form_str(form, "latitude")
    lon_str = _form_str(form, "longitude")
    latitude = float(lat_str) if lat_str else None
    longitude = float(lon_str) if lon_str else None

    return {
        "name": _form_str(form, "name"),
        "description": _form_str(form, "description") or None,
        "parent_id": _form_str(form, "parent_id") or None,
        "geo_area_id": _form_str(form, "geo_area_id") or None,
        "latitude": latitude,
        "longitude": longitude,
        "is_active": _form_str(form, "is_active") == "true",
    }


def validate_form(values: dict[str, object]) -> str | None:
    """Validate zone form values. Returns error message or None."""
    name = values.get("name")
    if not name or not str(name).strip():
        return "Zone name is required."
    return None


def create_zone(db: Session, values: dict[str, object]) -> NetworkZone:
    """Create a zone from validated form values."""
    return network_service.network_zones.create(
        db,
        name=str(values["name"]),
        description=str(values["description"]) if values.get("description") else None,
        parent_id=str(values["parent_id"]) if values.get("parent_id") else None,
        geo_area_id=str(values["geo_area_id"]) if values.get("geo_area_id") else None,
        latitude=values.get("latitude"),  # type: ignore[arg-type]
        longitude=values.get("longitude"),  # type: ignore[arg-type]
        is_active=bool(values.get("is_active", True)),
    )


def update_zone(db: Session, zone_id: str, values: dict[str, object]) -> NetworkZone:
    """Update a zone from validated form values."""
    parent_id = values.get("parent_id")
    return network_service.network_zones.update(
        db,
        zone_id,
        name=str(values["name"]),
        description=str(values["description"]) if values.get("description") else None,
        parent_id=str(parent_id) if parent_id else None,
        clear_parent=not parent_id,
        geo_area_id=(str(values["geo_area_id"]) if values.get("geo_area_id") else None),
        clear_geo_area=not values.get("geo_area_id"),
        latitude=values.get("latitude"),  # type: ignore[arg-type]
        longitude=values.get("longitude"),  # type: ignore[arg-type]
        is_active=bool(values.get("is_active", True)),
    )


def zones_for_forms(db: Session) -> list[NetworkZone]:
    """Return active zones for form select dropdowns."""
    return network_service.network_zones.list(db, is_active=True)
