"""Network zone management services.

This module is the single owner of network-zone facts, including the typed
zone -> GeoArea binding: writers declare ``geo_area_id`` here, and consumers
resolve a zone's effective GeoArea only through ``resolve_geo_area``.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.gis import GeoArea
from app.models.network import NetworkZone
from app.services.common import coerce_uuid

logger = logging.getLogger(__name__)


def _validated_geo_area_id(db: Session, geo_area_id: str) -> uuid.UUID:
    area_id = coerce_uuid(geo_area_id)
    area = db.get(GeoArea, area_id)
    if area is None or not area.is_active:
        raise HTTPException(
            status_code=400,
            detail="geo_area_id must reference an active GeoArea",
        )
    return area_id


class NetworkZones:
    """CRUD operations for network zones."""

    @staticmethod
    def list(
        db: Session,
        *,
        is_active: bool | None = None,
        parent_id: str | None = None,
        order_by: str = "name",
        order_dir: str = "asc",
        limit: int = 200,
        offset: int = 0,
    ) -> list[NetworkZone]:
        """List network zones with optional filtering."""
        stmt = select(NetworkZone)
        if is_active is not None:
            stmt = stmt.where(NetworkZone.is_active.is_(is_active))
        if parent_id:
            stmt = stmt.where(NetworkZone.parent_id == coerce_uuid(parent_id))
        elif parent_id == "":
            # Explicitly filter for top-level zones
            stmt = stmt.where(NetworkZone.parent_id.is_(None))

        col = getattr(NetworkZone, order_by, NetworkZone.name)
        stmt = stmt.order_by(col.desc() if order_dir == "desc" else col.asc())
        stmt = stmt.limit(limit).offset(offset)
        return list(db.scalars(stmt).all())

    @staticmethod
    def get(db: Session, zone_id: str) -> NetworkZone:
        """Get a network zone by ID or raise 404."""
        zone = db.get(NetworkZone, coerce_uuid(zone_id))
        if not zone:
            raise HTTPException(status_code=404, detail="Network zone not found")
        return zone

    @staticmethod
    def get_or_none(db: Session, zone_id: str) -> NetworkZone | None:
        """Get a network zone by ID or return None."""
        return db.get(NetworkZone, coerce_uuid(zone_id))

    @staticmethod
    def resolve_geo_area(
        db: Session, zone_id: str | uuid.UUID | None
    ) -> uuid.UUID | None:
        """Owner query: the GeoArea this zone belongs to.

        A zone without its own binding inherits through the parent chain. An
        explicit binding on the nearest bound active zone is authoritative; if
        its GeoArea has been retired the resolution degrades to ``None`` rather
        than skipping upward, so routing falls back to global policies instead
        of silently rebinding to a wider area.
        """

        current = coerce_uuid(str(zone_id)) if zone_id else None
        seen: set[uuid.UUID] = set()
        while current is not None and current not in seen:
            seen.add(current)
            zone = db.get(NetworkZone, current)
            if zone is None:
                return None
            if zone.is_active and zone.geo_area_id is not None:
                area = db.get(GeoArea, zone.geo_area_id)
                if area is not None and area.is_active:
                    return zone.geo_area_id
                return None
            current = zone.parent_id
        return None

    @staticmethod
    def create(
        db: Session,
        *,
        name: str,
        description: str | None = None,
        parent_id: str | None = None,
        geo_area_id: str | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
        is_active: bool = True,
    ) -> NetworkZone:
        """Create a new network zone."""
        zone = NetworkZone(
            name=name,
            description=description,
            parent_id=coerce_uuid(parent_id) if parent_id else None,
            geo_area_id=(
                _validated_geo_area_id(db, geo_area_id) if geo_area_id else None
            ),
            latitude=latitude,
            longitude=longitude,
            is_active=is_active,
        )
        db.add(zone)
        db.commit()
        db.refresh(zone)
        logger.info("Created network zone %s: %s", zone.id, zone.name)
        return zone

    @staticmethod
    def update(
        db: Session,
        zone_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        parent_id: str | None = None,
        geo_area_id: str | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
        is_active: bool | None = None,
        clear_parent: bool = False,
        clear_geo_area: bool = False,
    ) -> NetworkZone:
        """Update an existing network zone."""
        zone = db.get(NetworkZone, coerce_uuid(zone_id))
        if not zone:
            raise HTTPException(status_code=404, detail="Network zone not found")
        if name is not None:
            zone.name = name
        if description is not None:
            zone.description = description
        if clear_parent:
            zone.parent_id = None
        elif parent_id is not None:
            zone.parent_id = coerce_uuid(parent_id)
        if clear_geo_area:
            zone.geo_area_id = None
        elif geo_area_id is not None:
            zone.geo_area_id = _validated_geo_area_id(db, geo_area_id)
        if latitude is not None:
            zone.latitude = latitude
        if longitude is not None:
            zone.longitude = longitude
        if is_active is not None:
            zone.is_active = is_active
        db.commit()
        db.refresh(zone)
        logger.info("Updated network zone %s: %s", zone.id, zone.name)
        return zone

    @staticmethod
    def delete(db: Session, zone_id: str) -> None:
        """Delete (soft-delete) a network zone."""
        zone = db.get(NetworkZone, coerce_uuid(zone_id))
        if not zone:
            raise HTTPException(status_code=404, detail="Network zone not found")
        zone.is_active = False
        db.commit()
        logger.info("Soft-deleted network zone %s", zone_id)

    @staticmethod
    def count(db: Session, *, is_active: bool | None = None) -> int:
        """Count network zones."""
        stmt = select(func.count()).select_from(NetworkZone)
        if is_active is not None:
            stmt = stmt.where(NetworkZone.is_active.is_(is_active))
        return db.scalar(stmt) or 0


network_zones = NetworkZones()
