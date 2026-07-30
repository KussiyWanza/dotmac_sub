"""Read-only field map assets backed by sub's native plant/GIS tables."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from math import asin, cos, radians, sin, sqrt
from typing import Any, Protocol, cast

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType, AuditEvent
from app.models.field_map import FieldMapAssetLocationProvenance
from app.models.gis import ServiceBuilding
from app.models.network import FdhCabinet, FiberAccessPoint, FiberSpliceClosure
from app.models.wireless_mast import WirelessMast
from app.services.common import coerce_uuid

_EARTH_RADIUS_M = 6_371_000.0
_METERS_PER_DEGREE = 111_320.0
AssetPayload = dict[str, Any]
AssetPayloads = list[AssetPayload]
AssetTypeFilter = list[str] | None
_CONFIDENCE = {
    None: 0,
    "unknown": 0,
    "gps": 10,
    "mobile": 10,
    "manual": 20,
    "revert": 30,
    "survey": 40,
}


class _MapAssetModel(Protocol):
    is_active: Any
    latitude: Any
    longitude: Any
    updated_at: Any


@dataclass(frozen=True)
class _AssetConfig:
    asset_type: str
    model: type[_MapAssetModel]
    title: str
    subtitle: Callable[[Any], str | None]


def _compact(parts: list[str | None]) -> str | None:
    text = " - ".join(part for part in parts if part)
    return text or None


def _label(value: Any) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "value", value))


def _status(row: Any) -> str | None:
    status = _label(getattr(row, "status", None))
    if status:
        return status
    active = getattr(row, "is_active", None)
    if active is None:
        return None
    return "active" if active else "inactive"


def _point_wkt(latitude: float, longitude: float) -> str:
    return f"SRID=4326;POINT({longitude} {latitude})"


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _confidence(source: str | None) -> int:
    return _CONFIDENCE.get((source or "unknown").casefold(), 0)


def _actor_uuid(actor_id: str | None):
    if not actor_id:
        return None
    try:
        return coerce_uuid(actor_id)
    except ValueError:
        return None


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    d_lat = radians(lat2 - lat1)
    d_lon = radians(lon2 - lon1)
    r_lat1 = radians(lat1)
    r_lat2 = radians(lat2)
    a = sin(d_lat / 2) ** 2 + cos(r_lat1) * cos(r_lat2) * sin(d_lon / 2) ** 2
    return 2 * _EARTH_RADIUS_M * asin(sqrt(a))


def _bounds(
    latitude: float, longitude: float, radius_m: float
) -> tuple[float, float, float, float]:
    lat_delta = radius_m / _METERS_PER_DEGREE
    lng_scale = max(cos(radians(latitude)), 0.01)
    lng_delta = radius_m / (_METERS_PER_DEGREE * lng_scale)
    return (
        latitude - lat_delta,
        latitude + lat_delta,
        longitude - lng_delta,
        longitude + lng_delta,
    )


_ASSETS: dict[str, _AssetConfig] = {
    "fdh_cabinet": _AssetConfig(
        asset_type="fdh_cabinet",
        model=cast(type[_MapAssetModel], FdhCabinet),
        title="name",
        subtitle=lambda row: row.code,
    ),
    "splice_closure": _AssetConfig(
        asset_type="splice_closure",
        model=cast(type[_MapAssetModel], FiberSpliceClosure),
        title="name",
        subtitle=lambda row: None,
    ),
    "fiber_access_point": _AssetConfig(
        asset_type="fiber_access_point",
        model=cast(type[_MapAssetModel], FiberAccessPoint),
        title="name",
        subtitle=lambda row: _compact(
            [row.code, row.access_point_type, row.placement, row.street]
        ),
    ),
    "service_building": _AssetConfig(
        asset_type="service_building",
        model=cast(type[_MapAssetModel], ServiceBuilding),
        title="name",
        subtitle=lambda row: _compact([row.code, row.clli, row.street]),
    ),
    "wireless_mast": _AssetConfig(
        asset_type="wireless_mast",
        model=cast(type[_MapAssetModel], WirelessMast),
        title="name",
        subtitle=lambda row: _compact([row.structure_type, row.owner]),
    ),
}


def _configs(asset_types: AssetTypeFilter) -> list[_AssetConfig]:
    if not asset_types:
        return list(_ASSETS.values())
    unknown = sorted(set(asset_types) - set(_ASSETS))
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported map asset type: {', '.join(unknown)}",
        )
    return [_ASSETS[asset_type] for asset_type in asset_types]


def _base_query(db: Session, config: _AssetConfig):
    model = config.model
    return (
        db.query(model)
        .filter(model.is_active.is_(True))
        .filter(model.latitude.isnot(None))
        .filter(model.longitude.isnot(None))
    )


def _asset_row(db: Session, asset_type: str, asset_id: str) -> tuple[_AssetConfig, Any]:
    config = _ASSETS.get(asset_type)
    if config is None:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported map asset type: {asset_type}",
        )
    try:
        asset_uuid = coerce_uuid(asset_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid asset id") from exc
    row = db.get(config.model, asset_uuid)
    if row is None or getattr(row, "is_active", True) is False:
        raise HTTPException(status_code=404, detail="Map asset not found")
    return config, row


def _serialize(
    row: Any,
    config: _AssetConfig,
    *,
    distance_m: float | None = None,
) -> AssetPayload:
    return {
        "id": row.id,
        "type": config.asset_type,
        "title": getattr(row, config.title),
        "subtitle": config.subtitle(row),
        "latitude": float(row.latitude),
        "longitude": float(row.longitude),
        "status": _status(row),
        "updated_at": getattr(row, "updated_at", None),
        "distance_m": distance_m,
    }


class FieldMapAssets:
    @staticmethod
    def list(
        db: Session,
        *,
        asset_types: AssetTypeFilter = None,
        updated_since: datetime | None = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> AssetPayloads:
        items: AssetPayloads = []
        for config in _configs(asset_types):
            model = config.model
            query = _base_query(db, config)
            if updated_since is not None:
                query = query.filter(model.updated_at >= updated_since)
            rows = query.all()
            items.extend(_serialize(row, config) for row in rows)

        items.sort(key=lambda item: (item["type"], item["title"], str(item["id"])))
        return items[offset : offset + limit]

    @staticmethod
    def nearby(
        db: Session,
        *,
        latitude: float,
        longitude: float,
        radius_m: float = 500.0,
        asset_types: AssetTypeFilter = None,
        limit: int = 50,
    ) -> AssetPayloads:
        min_lat, max_lat, min_lng, max_lng = _bounds(latitude, longitude, radius_m)
        items: AssetPayloads = []
        for config in _configs(asset_types):
            model = config.model
            rows = (
                _base_query(db, config)
                .filter(model.latitude.between(min_lat, max_lat))
                .filter(model.longitude.between(min_lng, max_lng))
                .all()
            )
            for row in rows:
                distance = _haversine_m(
                    latitude,
                    longitude,
                    float(row.latitude),
                    float(row.longitude),
                )
                if distance <= radius_m:
                    items.append(_serialize(row, config, distance_m=round(distance, 1)))

        items.sort(key=lambda item: (item["distance_m"] or 0.0, item["title"]))
        return items[:limit]

    @staticmethod
    def search(
        db: Session,
        query: str,
        *,
        asset_types: AssetTypeFilter = None,
        limit: int = 20,
    ) -> AssetPayloads:
        term = query.strip().casefold()
        if not term:
            return []

        items: AssetPayloads = []
        for config in _configs(asset_types):
            for row in _base_query(db, config).all():
                payload = _serialize(row, config)
                searchable = " ".join(
                    part
                    for part in [
                        payload["title"],
                        payload["subtitle"],
                        payload["status"],
                    ]
                    if part
                ).casefold()
                if term in searchable:
                    items.append(payload)
                    if len(items) >= limit:
                        return items

        items.sort(key=lambda item: (item["type"], item["title"]))
        return items[:limit]

    @staticmethod
    def update_location(
        db: Session,
        *,
        asset_type: str,
        asset_id: str,
        latitude: float,
        longitude: float,
        actor_id: str | None = None,
        expected_updated_at: datetime | None = None,
        source: str | None = None,
        accuracy_m: float | None = None,
        client_ref: str | None = None,
        force: bool = False,
        audit_context: dict[str, Any] | None = None,
    ) -> AssetPayload:
        config, row = _asset_row(db, asset_type, asset_id)
        current_updated_at = _as_utc(getattr(row, "updated_at", None))
        if (
            expected_updated_at is not None
            and current_updated_at is not None
            and current_updated_at != _as_utc(expected_updated_at)
        ):
            raise HTTPException(
                status_code=409,
                detail="Map asset was modified since it was loaded",
            )

        asset_uuid = coerce_uuid(asset_id)
        provenance = (
            db.query(FieldMapAssetLocationProvenance)
            .filter(
                FieldMapAssetLocationProvenance.asset_type == asset_type,
                FieldMapAssetLocationProvenance.asset_id == asset_uuid,
            )
            .one_or_none()
        )
        if (
            not force
            and provenance is not None
            and _confidence(source) < _confidence(provenance.source)
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    "Refusing to overwrite a higher-confidence map asset "
                    "location; pass force=true to override"
                ),
            )

        previous = {
            "latitude": float(row.latitude) if row.latitude is not None else None,
            "longitude": float(row.longitude) if row.longitude is not None else None,
        }
        row.latitude = float(latitude)
        row.longitude = float(longitude)
        if hasattr(row, "geom"):
            row.geom = _point_wkt(float(latitude), float(longitude))

        db.add(
            AuditEvent(
                actor_type=AuditActorType.user if actor_id else AuditActorType.system,
                actor_id=actor_id,
                action="field:map_asset:update_location",
                # The snake_case asset key, not the ORM class name: every plant
                # detail page filters activity on this exact string, so
                # "FdhCabinet" silently produced an empty activity feed and a
                # field move was traceable only by SQL.
                entity_type=asset_type,
                entity_id=str(asset_uuid),
                status_code=200,
                is_success=True,
                metadata_={
                    "asset_type": asset_type,
                    "from": previous,
                    "to": {"latitude": float(latitude), "longitude": float(longitude)},
                    "source": source,
                    "accuracy_m": accuracy_m,
                    "client_ref": client_ref,
                    "forced": bool(force),
                    **(audit_context or {}),
                },
            )
        )

        actor_uuid = _actor_uuid(actor_id)
        if provenance is None:
            db.add(
                FieldMapAssetLocationProvenance(
                    asset_type=asset_type,
                    asset_id=asset_uuid,
                    source=source,
                    accuracy_m=accuracy_m,
                    updated_by_principal_id=actor_uuid,
                )
            )
        else:
            provenance.source = source
            provenance.accuracy_m = accuracy_m
            provenance.updated_by_principal_id = actor_uuid

        db.commit()
        db.refresh(row)
        return _serialize(row, config)

    @staticmethod
    def revert_location(
        db: Session,
        *,
        asset_type: str,
        asset_id: str,
        actor_id: str | None = None,
    ) -> AssetPayload:
        config, row = _asset_row(db, asset_type, asset_id)
        last = (
            db.query(AuditEvent)
            .filter(AuditEvent.action == "field:map_asset:update_location")
            # Rows written before the key was corrected carry the ORM class
            # name, so a revert must still find a technician's last move.
            .filter(AuditEvent.entity_type.in_((asset_type, config.model.__name__)))
            .filter(AuditEvent.entity_id == str(row.id))
            .order_by(AuditEvent.occurred_at.desc())
            .first()
        )
        if last is None or not last.metadata_:
            raise HTTPException(status_code=404, detail="No location change to revert")
        previous = (last.metadata_ or {}).get("from") or {}
        prev_latitude = previous.get("latitude")
        prev_longitude = previous.get("longitude")
        if prev_latitude is None or prev_longitude is None:
            raise HTTPException(
                status_code=422,
                detail="The previous location was empty; nothing to revert to",
            )
        return FieldMapAssets.update_location(
            db,
            asset_type=asset_type,
            asset_id=asset_id,
            latitude=float(prev_latitude),
            longitude=float(prev_longitude),
            actor_id=actor_id,
            source="revert",
            force=True,
            audit_context={"revert_of": str(last.id)},
        )


field_map_assets = FieldMapAssets()
