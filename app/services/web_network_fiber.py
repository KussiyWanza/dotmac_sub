"""Service helpers for admin fiber-network web routes."""

from __future__ import annotations

import heapq
import json
import logging
import math
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.inspection import inspect
from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.fiber_change_request import FiberChangeRequestStatus
from app.models.network import (
    FdhCabinet,
    FiberAccessPoint,
    FiberSegment,
    FiberSplice,
    FiberSpliceClosure,
    FiberSpliceTray,
    FiberStrand,
    OntAssignment,
    OntUnit,
    Splitter,
)
from app.models.subscriber import Address, Subscriber
from app.schemas.fiber_cost_items import FiberCostEstimate, FiberPricingState
from app.services import fiber_change_requests as change_request_service
from app.services import fiber_cost_items, settings_spec

logger = logging.getLogger(__name__)


def serialize_cost_estimate(estimate: FiberCostEstimate) -> dict[str, object]:
    """Serialize the typed owner outcome at the web/JSON boundary."""

    return {
        "currency": estimate.currency,
        "is_complete": estimate.is_complete,
        "unpriced": [code.value for code in estimate.unpriced],
        "total": str(estimate.total),
        "lines": [
            {
                "code": line.code.value,
                "label": line.label,
                "unit": line.unit.value,
                "amount": str(line.amount),
                "quantity": str(line.quantity),
                "total": str(line.total),
            }
            for line in estimate.lines
        ],
    }


def serialize_pricing_state(state: FiberPricingState) -> dict[str, object]:
    """Serialize pricing readiness for the map template."""

    return {
        "currency": state.currency,
        "item_count": state.item_count,
        "unpriced": [code.value for code in state.unpriced],
        "is_complete": state.is_complete,
    }


def _coerce_float(value: object, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return default
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return default


def _coerce_int(value: object, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _coerce_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return default


def _setting_float(
    db: Session, domain: SettingDomain, key: str, default: float
) -> float:
    return _coerce_float(settings_spec.resolve_value(db, domain, key), default)


def _setting_int(db: Session, domain: SettingDomain, key: str, default: int) -> int:
    return _coerce_int(settings_spec.resolve_value(db, domain, key), default)


def _setting_bool(
    db: Session, domain: SettingDomain, key: str, default: bool = False
) -> bool:
    return _coerce_bool(settings_spec.resolve_value(db, domain, key), default)


def get_fiber_plant_consolidated_data(db: Session) -> dict[str, object]:
    """Return datasets/statistics for the consolidated fiber plant page."""
    cabinets = (
        db.query(FdhCabinet)
        .filter(FdhCabinet.is_active.is_(True))
        .order_by(FdhCabinet.name)
        .limit(200)
        .all()
    )
    splitters = (
        db.query(Splitter)
        .filter(Splitter.is_active.is_(True))
        .order_by(Splitter.name)
        .limit(200)
        .all()
    )
    strands = (
        db.query(FiberStrand)
        .order_by(FiberStrand.cable_name, FiberStrand.strand_number)
        .limit(200)
        .all()
    )
    closures = (
        db.query(FiberSpliceClosure)
        .filter(FiberSpliceClosure.is_active.is_(True))
        .order_by(FiberSpliceClosure.name)
        .limit(200)
        .all()
    )
    change_requests = change_request_service.list_requests(
        db,
        status=FiberChangeRequestStatus.pending,
    )

    strands_available = sum(
        1 for strand in strands if strand.status.value == "available"
    )
    strands_in_use = sum(1 for strand in strands if strand.status.value == "in_use")

    stats = {
        "cabinets": len(cabinets),
        "splitters": len(splitters),
        "strands": len(strands),
        "strands_available": strands_available,
        "strands_in_use": strands_in_use,
        "closures": len(closures),
        "pending_changes": len(change_requests),
    }

    return {
        "stats": stats,
        "cabinets": cabinets,
        "splitters": splitters,
        "strands": strands,
        "closures": closures,
        "change_requests": change_requests,
    }


def has_change_request_conflict(db: Session, change_request) -> bool:
    """Return True if asset changed after request creation."""
    if not change_request.asset_id:
        return False
    _, model = change_request_service._get_model(change_request.asset_type)
    asset = db.get(model, change_request.asset_id)
    if not asset or not getattr(asset, "updated_at", None):
        return False
    asset_updated_at = getattr(asset, "updated_at", None)
    request_created_at = getattr(change_request, "created_at", None)
    if not isinstance(asset_updated_at, datetime) or not isinstance(
        request_created_at, datetime
    ):
        return False
    return asset_updated_at > request_created_at


def serialize_asset(asset) -> dict:
    """Serialize SQLAlchemy model columns for diff views."""
    if not asset:
        return {}
    data: dict[str, object] = {}
    for column in inspect(asset).mapper.column_attrs:
        key = column.key
        if key in {"route_geom", "geom"}:
            continue
        value = getattr(asset, key)
        if hasattr(value, "value"):
            value = value.value
        data[key] = value
    return data


def get_fiber_plant_map_data(db: Session) -> dict[str, object]:
    """Return GeoJSON + stats + cost settings for fiber map page."""
    features: list[dict] = []

    fdh_cabinets = (
        db.query(FdhCabinet)
        .filter(
            FdhCabinet.is_active.is_(True),
            FdhCabinet.latitude.isnot(None),
            FdhCabinet.longitude.isnot(None),
        )
        .all()
    )
    splitter_counts: dict[UUID, int] = {}
    if fdh_cabinets:
        fdh_ids = [fdh.id for fdh in fdh_cabinets]
        for fdh_id, count in (
            db.query(Splitter.fdh_id, func.count(Splitter.id))
            .filter(Splitter.fdh_id.in_(fdh_ids))
            .group_by(Splitter.fdh_id)
            .all()
        ):
            if fdh_id is not None:
                splitter_counts[fdh_id] = int(count)
    for fdh in fdh_cabinets:
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [fdh.longitude, fdh.latitude],
                },
                "properties": {
                    "id": str(fdh.id),
                    "type": "fdh_cabinet",
                    "name": fdh.name,
                    "code": fdh.code,
                    "splitter_count": splitter_counts.get(fdh.id, 0),
                },
            }
        )

    closures = (
        db.query(FiberSpliceClosure)
        .filter(
            FiberSpliceClosure.is_active.is_(True),
            FiberSpliceClosure.latitude.isnot(None),
            FiberSpliceClosure.longitude.isnot(None),
        )
        .all()
    )
    splice_counts: dict[UUID, int] = {}
    tray_counts: dict[UUID, int] = {}
    if closures:
        closure_ids = [closure.id for closure in closures]
        for closure_id, count in (
            db.query(FiberSplice.closure_id, func.count(FiberSplice.id))
            .filter(FiberSplice.closure_id.in_(closure_ids))
            .group_by(FiberSplice.closure_id)
            .all()
        ):
            splice_counts[closure_id] = int(count)
        for closure_id, count in (
            db.query(FiberSpliceTray.closure_id, func.count(FiberSpliceTray.id))
            .filter(FiberSpliceTray.closure_id.in_(closure_ids))
            .group_by(FiberSpliceTray.closure_id)
            .all()
        ):
            tray_counts[closure_id] = int(count)
    for closure in closures:
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [closure.longitude, closure.latitude],
                },
                "properties": {
                    "id": str(closure.id),
                    "type": "splice_closure",
                    "name": closure.name,
                    "splice_count": splice_counts.get(closure.id, 0),
                    "tray_count": tray_counts.get(closure.id, 0),
                },
            }
        )

    access_points = (
        db.query(FiberAccessPoint)
        .filter(
            FiberAccessPoint.is_active.is_(True),
            FiberAccessPoint.latitude.isnot(None),
            FiberAccessPoint.longitude.isnot(None),
        )
        .all()
    )
    for access_point in access_points:
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [access_point.longitude, access_point.latitude],
                },
                "properties": {
                    "id": str(access_point.id),
                    "type": "access_point",
                    "name": access_point.name,
                    "code": access_point.code,
                    "ap_type": access_point.access_point_type,
                    "placement": access_point.placement,
                },
            }
        )

    segments = db.query(FiberSegment).filter(FiberSegment.is_active.is_(True)).all()
    segment_geoms = (
        db.query(FiberSegment, func.ST_AsGeoJSON(FiberSegment.route_geom))
        .filter(FiberSegment.is_active.is_(True), FiberSegment.route_geom.isnot(None))
        .all()
    )
    for segment, geojson_str in segment_geoms:
        if not geojson_str:
            continue
        geom = json.loads(geojson_str)
        features.append(
            {
                "type": "Feature",
                "geometry": geom,
                "properties": {
                    "id": str(segment.id),
                    "type": "fiber_segment",
                    "name": segment.name,
                    "segment_type": segment.segment_type.value
                    if segment.segment_type
                    else None,
                    "cable_type": segment.cable_type.value
                    if segment.cable_type
                    else None,
                    "fiber_count": segment.fiber_count,
                    "length_m": segment.length_m,
                },
            }
        )

    stats = {
        "fdh_cabinets": db.query(func.count(FdhCabinet.id))
        .filter(FdhCabinet.is_active.is_(True))
        .scalar(),
        "fdh_with_location": len(fdh_cabinets),
        "splice_closures": db.query(func.count(FiberSpliceClosure.id))
        .filter(FiberSpliceClosure.is_active.is_(True))
        .scalar(),
        "closures_with_location": len(closures),
        "splitters": db.query(func.count(Splitter.id))
        .filter(Splitter.is_active.is_(True))
        .scalar(),
        "total_splices": db.query(func.count(FiberSplice.id)).scalar(),
        "segments": len(segments),
        "access_points": db.query(func.count(FiberAccessPoint.id))
        .filter(FiberAccessPoint.is_active.is_(True))
        .scalar(),
        "access_points_with_location": len(access_points),
    }

    # The components an estimate is built from, priced or not. The page needs
    # them to render the pricing state; it no longer needs the numbers, because
    # it no longer does the arithmetic.
    cost_state = serialize_pricing_state(fiber_cost_items.pricing_state(db))

    return {
        "geojson_data": {"type": "FeatureCollection", "features": features},
        "stats": stats,
        "cost_state": cost_state,
    }


def get_fiber_reports_data(db: Session, map_limit: int | None) -> dict[str, object]:
    """Return aggregated report data and customer map for fiber reports page."""
    stats = {
        "fdh_cabinets": {
            "total": db.query(func.count(FdhCabinet.id)).scalar() or 0,
            "active": db.query(func.count(FdhCabinet.id))
            .filter(FdhCabinet.is_active.is_(True))
            .scalar()
            or 0,
            "with_location": db.query(func.count(FdhCabinet.id))
            .filter(FdhCabinet.latitude.isnot(None), FdhCabinet.longitude.isnot(None))
            .scalar()
            or 0,
        },
        "splice_closures": {
            "total": db.query(func.count(FiberSpliceClosure.id)).scalar() or 0,
            "active": db.query(func.count(FiberSpliceClosure.id))
            .filter(FiberSpliceClosure.is_active.is_(True))
            .scalar()
            or 0,
            "with_location": db.query(func.count(FiberSpliceClosure.id))
            .filter(
                FiberSpliceClosure.latitude.isnot(None),
                FiberSpliceClosure.longitude.isnot(None),
            )
            .scalar()
            or 0,
        },
        "splitters": {
            "total": db.query(func.count(Splitter.id)).scalar() or 0,
            "active": db.query(func.count(Splitter.id))
            .filter(Splitter.is_active.is_(True))
            .scalar()
            or 0,
        },
        "splices": {"total": db.query(func.count(FiberSplice.id)).scalar() or 0},
        "trays": {"total": db.query(func.count(FiberSpliceTray.id)).scalar() or 0},
        "ont_units": {
            "total": db.query(func.count(OntUnit.id)).scalar() or 0,
            "active": db.query(func.count(OntUnit.id))
            .filter(OntUnit.is_active.is_(True))
            .scalar()
            or 0,
            "assigned": db.query(func.count(OntAssignment.id))
            .filter(OntAssignment.active.is_(True))
            .scalar()
            or 0,
        },
    }

    segments = db.query(FiberSegment).filter(FiberSegment.is_active.is_(True)).all()
    segment_stats: dict[str, object] = {
        "feeder": {"count": 0, "length": 0},
        "distribution": {"count": 0, "length": 0},
        "drop": {"count": 0, "length": 0},
    }
    for segment in segments:
        segment_type = (
            segment.segment_type.value if segment.segment_type else "distribution"
        )
        if segment_type in segment_stats:
            entry = segment_stats[segment_type]
            if isinstance(entry, dict):
                entry["count"] = int(entry.get("count", 0)) + 1
                entry["length"] = int(entry.get("length", 0)) + int(
                    segment.length_m or 0
                )

    segment_stats["total_count"] = len(segments)
    segment_stats["total_length"] = sum(
        int(v.get("length", 0)) for v in segment_stats.values() if isinstance(v, dict)
    )
    stats["segments"] = segment_stats

    if map_limit is None:
        map_limit = _setting_int(db, SettingDomain.gis, "map_customer_limit", 0) or None
    if map_limit is not None and map_limit <= 0:
        map_limit = None

    customer_total = (
        db.query(func.count(Address.id))
        .join(OntAssignment, OntAssignment.service_address_id == Address.id)
        .join(Subscriber, Address.subscriber_id == Subscriber.id)
        .filter(
            OntAssignment.active.is_(True),
            Address.latitude.isnot(None),
            Address.longitude.isnot(None),
        )
        .scalar()
        or 0
    )

    customer_addresses_query = (
        db.query(
            Address.id,
            Address.address_line1,
            Address.city,
            Address.latitude,
            Address.longitude,
            Subscriber.first_name,
            Subscriber.last_name,
        )
        .join(OntAssignment, OntAssignment.service_address_id == Address.id)
        .join(Subscriber, Address.subscriber_id == Subscriber.id)
        .filter(
            OntAssignment.active.is_(True),
            Address.latitude.isnot(None),
            Address.longitude.isnot(None),
        )
        .order_by(Address.id)
    )
    if map_limit:
        customer_addresses_query = customer_addresses_query.limit(map_limit)
    customer_addresses = customer_addresses_query.all()

    features: list[dict] = []
    for address in customer_addresses:
        subscriber_name = (
            f"{address.first_name or ''} {address.last_name or ''}".strip() or "Unknown"
        )
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [address.longitude, address.latitude],
                },
                "properties": {
                    "id": str(address.id),
                    "type": "customer",
                    "name": subscriber_name,
                    "address": address.address_line1,
                    "city": address.city or "",
                },
            }
        )

    fdh_cabinets = (
        db.query(FdhCabinet)
        .filter(
            FdhCabinet.is_active.is_(True),
            FdhCabinet.latitude.isnot(None),
            FdhCabinet.longitude.isnot(None),
        )
        .all()
    )
    for fdh in fdh_cabinets:
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [fdh.longitude, fdh.latitude],
                },
                "properties": {
                    "id": str(fdh.id),
                    "type": "fdh_cabinet",
                    "name": fdh.name,
                    "code": fdh.code,
                },
            }
        )

    closures = (
        db.query(FiberSpliceClosure)
        .filter(
            FiberSpliceClosure.is_active.is_(True),
            FiberSpliceClosure.latitude.isnot(None),
            FiberSpliceClosure.longitude.isnot(None),
        )
        .all()
    )
    for closure in closures:
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [closure.longitude, closure.latitude],
                },
                "properties": {
                    "id": str(closure.id),
                    "type": "splice_closure",
                    "name": closure.name,
                },
            }
        )

    return {
        "stats": stats,
        "customer_geojson": {"type": "FeatureCollection", "features": features},
        "customer_count": customer_total,
        "customer_map_count": len(customer_addresses),
    }


def update_asset_position(
    db: Session,
    *,
    asset_type: str,
    asset_id: str,
    latitude: float,
    longitude: float,
) -> tuple[dict, int]:
    """Update map position for a supported fiber asset."""
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return {"error": "Coordinates out of range"}, 400

    asset: FdhCabinet | FiberSpliceClosure | None
    if asset_type == "fdh_cabinet":
        asset = db.query(FdhCabinet).filter(FdhCabinet.id == asset_id).first()
    elif asset_type == "splice_closure":
        asset = (
            db.query(FiberSpliceClosure)
            .filter(FiberSpliceClosure.id == asset_id)
            .first()
        )
    else:
        return {"error": "Invalid asset type"}, 400

    if not asset:
        return {"error": "Asset not found"}, 404

    asset.latitude = latitude
    asset.longitude = longitude
    db.commit()
    return {
        "success": True,
        "id": str(asset.id),
        "latitude": latitude,
        "longitude": longitude,
    }, 200


def find_nearest_cabinet_data(
    db: Session, *, lat: float, lng: float
) -> tuple[dict, int]:
    """Find nearest cabinet and routing path details for coordinates."""
    max_km = _setting_float(db, SettingDomain.gis, "map_nearest_search_max_km", 50.0)
    snap_max_m = _setting_float(db, SettingDomain.gis, "map_snap_max_m", 250.0)
    allow_fallback = _setting_bool(
        db, SettingDomain.gis, "map_allow_straightline_fallback", False
    )

    cabinets = _nearby_cabinets(db, lat, lng, max_km)
    if not cabinets:
        return {"error": f"No FDH cabinets found within {max_km} km"}, 404

    nearest: FdhCabinet | None = None
    min_distance = float("inf")
    for cabinet in cabinets:
        distance = _haversine_distance(lat, lng, cabinet.latitude, cabinet.longitude)
        if distance < min_distance:
            min_distance = distance
            nearest = cabinet
    if not nearest:
        return {"error": "Could not calculate nearest cabinet"}, 500

    path_coords = None
    path_type = "straight"
    graph, edges = _build_fiber_graph(db)
    start_node, _ = _snap_to_graph(lat, lng, graph, edges, snap_max_m)
    if nearest.latitude is None or nearest.longitude is None:
        return {"error": "Nearest cabinet is missing coordinates"}, 500
    cabinet_node, _ = _snap_to_graph(
        nearest.latitude, nearest.longitude, graph, edges, snap_max_m
    )
    if start_node and cabinet_node:
        path_distance, path = _shortest_path(graph, start_node, cabinet_node)
        if path_distance is not None and path:
            min_distance = path_distance
            path_coords = [[node[1], node[0]] for node in path]
            path_type = "fiber"
    if path_coords is None and not allow_fallback:
        return {"error": "No fiber route found to nearest cabinet"}, 404

    return {
        "cabinet": {
            "id": str(nearest.id),
            "name": nearest.name,
            "code": nearest.code,
            "latitude": nearest.latitude,
            "longitude": nearest.longitude,
        },
        "distance_m": round(min_distance, 2),
        "distance_display": _distance_display(min_distance),
        "path_coords": path_coords,
        "path_type": path_type,
        "customer_coords": {"latitude": lat, "longitude": lng},
        # Priced HERE, not in the browser. The page used to receive four
        # numbers and do the arithmetic itself, which put the breakdown a user
        # reads in the layer least able to explain it and made it untestable
        # without a browser. Routing already costs a request, so the estimate
        # rides along with the distance it prices.
        "cost_estimate": serialize_cost_estimate(
            fiber_cost_items.estimate_for_distance(db, Decimal(str(min_distance)))
        ),
    }, 200


def get_plan_options_data(db: Session, *, lat: float, lng: float) -> tuple[dict, int]:
    """Return nearest cabinet options ordered by distance."""
    max_km = _setting_float(db, SettingDomain.gis, "map_nearest_search_max_km", 50.0)
    cabinets = _nearby_cabinets(db, lat, lng, max_km)
    if not cabinets:
        return {"error": f"No FDH cabinets found within {max_km} km"}, 404

    options = []
    for cabinet in cabinets:
        distance = _haversine_distance(lat, lng, cabinet.latitude, cabinet.longitude)
        options.append(
            {
                "id": str(cabinet.id),
                "name": cabinet.name,
                "code": cabinet.code,
                "latitude": cabinet.latitude,
                "longitude": cabinet.longitude,
                "distance_m": round(distance, 2),
                "distance_display": _distance_display(distance),
            }
        )
    options.sort(key=lambda item: item["distance_m"])
    return {
        "options": options[:10],
        "customer_coords": {"latitude": lat, "longitude": lng},
    }, 200


def get_plan_route_data(
    db: Session,
    *,
    lat: float,
    lng: float,
    cabinet_id: str,
) -> tuple[dict, int]:
    """Return fiber-only route path from coordinates to selected cabinet."""
    cabinet = (
        db.query(FdhCabinet)
        .filter(
            FdhCabinet.id == cabinet_id,
            FdhCabinet.is_active.is_(True),
            FdhCabinet.latitude.isnot(None),
            FdhCabinet.longitude.isnot(None),
        )
        .first()
    )
    if not cabinet:
        return {"error": "Cabinet not found"}, 404

    snap_max_m = _setting_float(db, SettingDomain.gis, "map_snap_max_m", 250.0)
    graph, edges = _build_fiber_graph(db)
    start_node, start_snap = _snap_to_graph(lat, lng, graph, edges, snap_max_m)
    if cabinet.latitude is None or cabinet.longitude is None:
        return {"error": "Cabinet is missing coordinates"}, 400
    cabinet_node, cabinet_snap = _snap_to_graph(
        cabinet.latitude, cabinet.longitude, graph, edges, snap_max_m
    )
    if not start_node or not cabinet_node:
        return {
            "error": "Unable to snap to fiber network",
            "start_snap_m": start_snap,
            "cabinet_snap_m": cabinet_snap,
        }, 404

    distance, path = _shortest_path(graph, start_node, cabinet_node)
    if distance is None or not path:
        return {"error": "No fiber route found"}, 404

    return {
        "cabinet": {
            "id": str(cabinet.id),
            "name": cabinet.name,
            "code": cabinet.code,
            "latitude": cabinet.latitude,
            "longitude": cabinet.longitude,
        },
        "distance_m": round(distance, 2),
        "distance_display": _distance_display(distance),
        "path_coords": [[node[1], node[0]] for node in path],
        "path_type": "fiber",
        "cost_estimate": serialize_cost_estimate(
            fiber_cost_items.estimate_for_distance(db, Decimal(str(distance)))
        ),
    }, 200


def _distance_display(distance_m: float) -> str:
    if distance_m >= 1000:
        return f"{distance_m / 1000:.2f} km"
    return f"{distance_m:.0f} m"


def _haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return radius * c


def _node_key(lon: float, lat: float) -> tuple[float, float]:
    return round(lon, 6), round(lat, 6)


def _to_meters(lat0: float, lon: float, lat: float) -> tuple[float, float]:
    radius = 6371000.0
    x_val = math.radians(lon) * radius * math.cos(math.radians(lat0))
    y_val = math.radians(lat) * radius
    return x_val, y_val


def _closest_point_on_segment(
    lat0: float,
    lon: float,
    lat: float,
    lon1: float,
    lat1: float,
    lon2: float,
    lat2: float,
) -> tuple[float, float, float]:
    px, py = _to_meters(lat0, lon, lat)
    ax, ay = _to_meters(lat0, lon1, lat1)
    bx, by = _to_meters(lat0, lon2, lat2)
    dx = bx - ax
    dy = by - ay
    if dx == 0 and dy == 0:
        return lon1, lat1, math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    cx = ax + t * dx
    cy = ay + t * dy
    c_lon = math.degrees(cx / (6371000.0 * math.cos(math.radians(lat0))))
    c_lat = math.degrees(cy / 6371000.0)
    return c_lon, c_lat, math.hypot(px - cx, py - cy)


def _build_fiber_graph(db: Session):
    graph: dict[tuple[float, float], list[tuple[tuple[float, float], float]]] = {}
    edges: list[
        tuple[
            tuple[float, float],
            tuple[float, float],
            tuple[float, float],
            tuple[float, float],
        ]
    ] = []

    def add_edge(a: tuple[float, float], b: tuple[float, float]) -> None:
        dist = _haversine_distance(a[1], a[0], b[1], b[0])
        graph.setdefault(a, []).append((b, dist))
        graph.setdefault(b, []).append((a, dist))

    segments = (
        db.query(func.ST_AsGeoJSON(FiberSegment.route_geom))
        .filter(
            FiberSegment.is_active.is_(True),
            FiberSegment.route_geom.isnot(None),
        )
        .all()
    )
    for (geojson_str,) in segments:
        if not geojson_str:
            continue
        geom = json.loads(geojson_str)
        if geom.get("type") == "LineString":
            lines = [geom["coordinates"]]
        elif geom.get("type") == "MultiLineString":
            lines = geom["coordinates"]
        else:
            continue
        for line in lines:
            if len(line) < 2:
                continue
            for index in range(len(line) - 1):
                lon1, lat1 = line[index]
                lon2, lat2 = line[index + 1]
                a = _node_key(lon1, lat1)
                b = _node_key(lon2, lat2)
                add_edge(a, b)
                edges.append((a, b, (lon1, lat1), (lon2, lat2)))
    return graph, edges


def _snap_to_graph(
    lat_in: float,
    lon_in: float,
    graph: dict[tuple[float, float], list[tuple[tuple[float, float], float]]],
    edges: list[
        tuple[
            tuple[float, float],
            tuple[float, float],
            tuple[float, float],
            tuple[float, float],
        ]
    ],
    snap_max_m: float,
) -> tuple[tuple[float, float] | None, float]:
    if not edges:
        return None, float("inf")
    best = None
    best_dist = float("inf")
    best_edge = None
    for a, b, a_coord, b_coord in edges:
        c_lon, c_lat, dist = _closest_point_on_segment(
            lat_in,
            lon_in,
            lat_in,
            a_coord[0],
            a_coord[1],
            b_coord[0],
            b_coord[1],
        )
        if dist < best_dist:
            best_dist = dist
            best = _node_key(c_lon, c_lat)
            best_edge = (a, b)
    if best is None or best_dist > snap_max_m:
        return None, best_dist
    if best in graph:
        return best, best_dist
    if best_edge:
        a, b = best_edge
        dist_a = _haversine_distance(best[1], best[0], a[1], a[0])
        dist_b = _haversine_distance(best[1], best[0], b[1], b[0])
        graph.setdefault(best, [])
        graph.setdefault(a, []).append((best, dist_a))
        graph.setdefault(b, []).append((best, dist_b))
        graph[best].append((a, dist_a))
        graph[best].append((b, dist_b))
    return best, best_dist


def _shortest_path(
    graph: dict[tuple[float, float], list[tuple[tuple[float, float], float]]],
    start: tuple[float, float],
    target: tuple[float, float],
) -> tuple[float | None, list[tuple[float, float]] | None]:
    dist_map = {start: 0.0}
    prev: dict[tuple[float, float], tuple[float, float] | None] = {start: None}
    heap = [(0.0, start)]
    visited = set()
    while heap:
        dist, node = heapq.heappop(heap)
        if node in visited:
            continue
        visited.add(node)
        if node == target:
            path = []
            cur: tuple[float, float] | None = node
            while cur is not None:
                path.append(cur)
                cur = prev.get(cur)
            path.reverse()
            return dist, path
        for neighbor, weight in graph.get(node, []):
            if neighbor in visited:
                continue
            nd = dist + weight
            if nd < dist_map.get(neighbor, float("inf")):
                dist_map[neighbor] = nd
                prev[neighbor] = node
                heapq.heappush(heap, (nd, neighbor))
    return None, None


def _nearby_cabinets(db: Session, lat: float, lng: float, max_km: float):
    max_deg = max_km / 111.0
    if max_deg <= 0:
        return []
    return (
        db.query(FdhCabinet)
        .filter(
            FdhCabinet.is_active.is_(True),
            FdhCabinet.latitude.isnot(None),
            FdhCabinet.longitude.isnot(None),
            FdhCabinet.latitude.between(lat - max_deg, lat + max_deg),
            FdhCabinet.longitude.between(lng - max_deg, lng + max_deg),
        )
        .all()
    )
