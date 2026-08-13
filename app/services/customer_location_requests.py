from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from math import asin, cos, isclose, radians, sin, sqrt
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType, AuditEvent
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.gis import (
    CustomerLocationChangeRequest,
    CustomerLocationChangeRequestStatus,
    GeoArea,
    GeoAreaType,
    GeoLocation,
    GeoLocationType,
)
from app.models.subscriber import Address, AddressType, Subscriber
from app.services import geocoding as geocoding_service
from app.services import gis as gis_service
from app.services import service_address as service_address_service
from app.services.audit_adapter import record_audit_event
from app.services.customer_context import optional_customer_subscriber_id

logger = logging.getLogger(__name__)

DEFAULT_MAP_CENTER = [9.06, 7.49]

# Coverage area types that count as "serviceable" for pin auto-approval.
_COVERAGE_AREA_TYPES = (GeoAreaType.coverage, GeoAreaType.service_area)

# Stamped as the reviewer on auto-approved requests — also how the rate-limit
# guard recognises prior auto-approvals (vs manual ones).
AUTO_VERIFICATION_ACTOR_NAME = "Auto-verification (system)"


def _validate_coordinates(latitude: float, longitude: float) -> tuple[float, float]:
    lat = float(latitude)
    lon = float(longitude)
    if not (-90.0 <= lat <= 90.0):
        raise HTTPException(
            status_code=400, detail="Latitude must be between -90 and 90"
        )
    if not (-180.0 <= lon <= 180.0):
        raise HTTPException(
            status_code=400, detail="Longitude must be between -180 and 180"
        )
    return lat, lon


def _address_summary(
    address: Address | None, subscriber: Subscriber | None
) -> dict[str, Any]:
    if address:
        return {
            "address_id": str(address.id),
            "label": address.label,
            "address_type": address.address_type.value
            if address.address_type
            else None,
            "address_line1": address.address_line1,
            "address_line2": address.address_line2,
            "city": address.city,
            "region": address.region,
            "postal_code": address.postal_code,
            "country_code": address.country_code,
        }
    if subscriber:
        return {
            "address_id": None,
            "label": "Subscriber contact address",
            "address_type": None,
            "address_line1": subscriber.address_line1,
            "address_line2": subscriber.address_line2,
            "city": subscriber.city,
            "region": subscriber.region,
            "postal_code": subscriber.postal_code,
            "country_code": subscriber.country_code,
        }
    return {
        "address_id": None,
        "label": None,
        "address_type": None,
        "address_line1": None,
        "address_line2": None,
        "city": None,
        "region": None,
        "postal_code": None,
        "country_code": None,
    }


def _address_label(address: Address | None, subscriber: Subscriber | None) -> str:
    summary = _address_summary(address, subscriber)
    parts = [
        summary.get("label"),
        summary.get("address_line1"),
        summary.get("city"),
        summary.get("region"),
    ]
    return ", ".join(str(part) for part in parts if part) or "Service location"


def _resolve_service_address(db: Session, subscriber_id: str) -> Address | None:
    # Canonical resolution now lives in the owner module; this thin delegate
    # keeps existing callers stable. See service_address.service_address.
    return service_address_service.service_address(db, subscriber_id)


def _ensure_target_address(
    db: Session,
    subscriber: Subscriber,
    location_request: CustomerLocationChangeRequest,
) -> Address:
    address = None
    if location_request.address_id:
        address = db.get(Address, location_request.address_id)
    if address is None:
        address = _resolve_service_address(db, str(subscriber.id))
    if address is not None:
        return address

    if not (subscriber.address_line1 or "").strip():
        raise HTTPException(
            status_code=400,
            detail="Subscriber has no address record to attach the approved map pin to",
        )

    address = Address(
        subscriber_id=subscriber.id,
        address_type=AddressType.service,
        label="Primary service",
        address_line1=subscriber.address_line1,
        address_line2=subscriber.address_line2,
        city=subscriber.city,
        region=subscriber.region,
        postal_code=subscriber.postal_code,
        country_code=subscriber.country_code,
        is_primary=True,
    )
    db.add(address)
    db.flush()
    return address


def _upsert_geo_location_for_address(db: Session, address: Address) -> None:
    if address.latitude is None or address.longitude is None:
        return
    existing = (
        db.query(GeoLocation).filter(GeoLocation.address_id == address.id).first()
    )
    name = _address_label(address, None)
    if existing:
        existing.name = name
        existing.location_type = GeoLocationType.address
        existing.latitude = float(address.latitude)
        existing.longitude = float(address.longitude)
        existing.is_active = True
        gis_service._sync_location_geometry(existing)
        return
    geo_location = GeoLocation(
        name=name,
        location_type=GeoLocationType.address,
        latitude=float(address.latitude),
        longitude=float(address.longitude),
        address_id=address.id,
        is_active=True,
    )
    gis_service._sync_location_geometry(geo_location)
    db.add(geo_location)


def _audit(
    db: Session,
    *,
    actor_id: str | None,
    action: str,
    entity_id: str,
    metadata: dict[str, Any] | None = None,
    actor_type: AuditActorType = AuditActorType.user,
) -> AuditEvent | None:
    return record_audit_event(
        db,
        actor_type=actor_type,
        actor_id=actor_id,
        action=action,
        entity_type="customer_location_change_request",
        entity_id=entity_id,
        status_code=200,
        is_success=True,
        metadata=metadata,
        defer_until_commit=False,
    )


def _request_summary(item: CustomerLocationChangeRequest) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "status": item.status.value,
        "requested_latitude": item.requested_latitude,
        "requested_longitude": item.requested_longitude,
        "current_latitude": item.current_latitude,
        "current_longitude": item.current_longitude,
        "customer_note": item.customer_note,
        "review_note": item.review_note,
        "reviewed_by_actor_id": item.reviewed_by_actor_id,
        "reviewed_by_actor_name": item.reviewed_by_actor_name,
    }


def get_customer_location_page_context(db: Session, customer: dict) -> dict[str, Any]:
    subscriber_id = optional_customer_subscriber_id(db, customer)
    if not subscriber_id:
        raise HTTPException(status_code=404, detail="Subscriber account not found")

    subscriber = db.get(Subscriber, subscriber_id)
    if not subscriber:
        raise HTTPException(status_code=404, detail="Subscriber account not found")

    address = _resolve_service_address(db, str(subscriber.id))
    requests = (
        db.query(CustomerLocationChangeRequest)
        .filter(CustomerLocationChangeRequest.subscriber_id == subscriber.id)
        .order_by(CustomerLocationChangeRequest.created_at.desc())
        .limit(10)
        .all()
    )
    pending_request = next(
        (
            item
            for item in requests
            if item.status == CustomerLocationChangeRequestStatus.pending
        ),
        None,
    )

    current_latitude = (
        float(address.latitude) if address and address.latitude is not None else None
    )
    current_longitude = (
        float(address.longitude) if address and address.longitude is not None else None
    )
    draft_latitude = (
        float(pending_request.requested_latitude)
        if pending_request
        else (float(current_latitude) if current_latitude is not None else None)
    )
    draft_longitude = (
        float(pending_request.requested_longitude)
        if pending_request
        else (float(current_longitude) if current_longitude is not None else None)
    )
    center = (
        [draft_latitude, draft_longitude]
        if draft_latitude is not None and draft_longitude is not None
        else DEFAULT_MAP_CENTER
    )
    has_address_anchor = bool(address or (subscriber.address_line1 or "").strip())

    return {
        "location_address": _address_summary(address, subscriber),
        "location_address_label": _address_label(address, subscriber),
        "current_latitude": current_latitude,
        "current_longitude": current_longitude,
        "draft_latitude": draft_latitude,
        "draft_longitude": draft_longitude,
        "map_center": center,
        "pending_request": pending_request,
        "request_history": requests,
        "can_submit_request": pending_request is None and has_address_anchor,
        "has_address_anchor": has_address_anchor,
    }


def submit_request(
    db: Session,
    *,
    subscriber_id: str,
    latitude: float,
    longitude: float,
    customer_note: str | None,
    actor_id: str | None,
    actor_name: str | None,
    submitted_from_ip: str | None = None,
) -> CustomerLocationChangeRequest:
    lat, lon = _validate_coordinates(latitude, longitude)

    subscriber = db.get(Subscriber, subscriber_id)
    if not subscriber:
        raise HTTPException(status_code=404, detail="Subscriber account not found")

    address = _resolve_service_address(db, subscriber_id)
    if address is None and not (subscriber.address_line1 or "").strip():
        raise HTTPException(
            status_code=400,
            detail="No service address is available for this account yet",
        )
    existing_pending = (
        db.query(CustomerLocationChangeRequest)
        .filter(CustomerLocationChangeRequest.subscriber_id == subscriber.id)
        .filter(
            CustomerLocationChangeRequest.status
            == CustomerLocationChangeRequestStatus.pending
        )
        .order_by(CustomerLocationChangeRequest.created_at.desc())
        .first()
    )
    if existing_pending:
        raise HTTPException(
            status_code=400,
            detail="You already have a pending location correction awaiting review",
        )

    current_latitude = (
        float(address.latitude) if address and address.latitude is not None else None
    )
    current_longitude = (
        float(address.longitude) if address and address.longitude is not None else None
    )
    if (
        current_latitude is not None
        and current_longitude is not None
        and isclose(current_latitude, lat, abs_tol=1e-7)
        and isclose(current_longitude, lon, abs_tol=1e-7)
    ):
        raise HTTPException(
            status_code=400,
            detail="The new pin matches the current approved location",
        )

    location_request = CustomerLocationChangeRequest(
        subscriber_id=subscriber.id,
        address_id=address.id if address else None,
        status=CustomerLocationChangeRequestStatus.pending,
        current_latitude=current_latitude,
        current_longitude=current_longitude,
        requested_latitude=lat,
        requested_longitude=lon,
        customer_note=(customer_note or "").strip() or None,
        submitted_from_ip=submitted_from_ip,
        metadata_={
            "submitted_by_name": actor_name,
            "address_snapshot": _address_summary(address, subscriber),
        },
    )
    db.add(location_request)
    db.commit()
    db.refresh(location_request)

    _audit(
        db,
        actor_id=actor_id,
        action="customer_location_change_requested",
        entity_id=str(location_request.id),
        metadata={
            "subscriber_id": str(subscriber.id),
            "requested_latitude": lat,
            "requested_longitude": lon,
            "current_latitude": current_latitude,
            "current_longitude": current_longitude,
            "customer_note": location_request.customer_note,
        },
    )

    # Auto-verify safe pin nudges; everything else stays in the manual queue.
    approved, reason, signals = evaluate_auto_approval(
        db,
        subscriber_id=subscriber.id,
        current_latitude=current_latitude,
        current_longitude=current_longitude,
        requested_latitude=lat,
        requested_longitude=lon,
    )
    # Shadow mode records what auto-approval WOULD have done but still routes to
    # manual review — lets ops watch the decisions before trusting automation.
    shadow = _gis_setting_bool(db, "location_auto_approve_shadow", True)
    effective_approved = approved and not shadow
    location_request.metadata_ = {
        **(location_request.metadata_ or {}),
        "auto_decision": {
            "approved": effective_approved,
            "would_approve": approved,
            "shadow": shadow,
            "reason": reason,
            "signals": signals,
        },
    }
    db.commit()
    if effective_approved:
        return approve_request(
            db,
            request_id=str(location_request.id),
            actor_id=None,
            actor_name=AUTO_VERIFICATION_ACTOR_NAME,
            review_note=reason,
            actor_type=AuditActorType.system,
        )
    db.refresh(location_request)
    return location_request


def cancel_request(
    db: Session,
    *,
    request_id: str,
    subscriber_id: str,
    actor_id: str | None,
) -> CustomerLocationChangeRequest:
    location_request = db.get(CustomerLocationChangeRequest, request_id)
    if not location_request or str(location_request.subscriber_id) != str(
        subscriber_id
    ):
        raise HTTPException(
            status_code=404, detail="Location correction request not found"
        )
    if location_request.status != CustomerLocationChangeRequestStatus.pending:
        raise HTTPException(
            status_code=400, detail="Only pending requests can be canceled"
        )

    location_request.status = CustomerLocationChangeRequestStatus.cancelled
    location_request.reviewed_at = datetime.now(UTC)
    db.commit()
    db.refresh(location_request)

    _audit(
        db,
        actor_id=actor_id,
        action="customer_location_change_canceled",
        entity_id=str(location_request.id),
        metadata={"subscriber_id": subscriber_id},
    )
    return location_request


def list_requests(
    db: Session,
    *,
    status: CustomerLocationChangeRequestStatus | None = None,
    limit: int = 100,
) -> list[CustomerLocationChangeRequest]:
    query = db.query(CustomerLocationChangeRequest)
    if status is not None:
        query = query.filter(CustomerLocationChangeRequest.status == status)
    return (
        query.order_by(
            CustomerLocationChangeRequest.created_at.desc(),
            CustomerLocationChangeRequest.updated_at.desc(),
        )
        .limit(limit)
        .all()
    )


def get_admin_review_context(
    db: Session, *, status: CustomerLocationChangeRequestStatus | None = None
) -> dict[str, Any]:
    requests = list_requests(db, status=status, limit=100)
    pending_count = (
        db.query(CustomerLocationChangeRequest)
        .filter(
            CustomerLocationChangeRequest.status
            == CustomerLocationChangeRequestStatus.pending
        )
        .count()
    )
    approved_count = (
        db.query(CustomerLocationChangeRequest)
        .filter(
            CustomerLocationChangeRequest.status
            == CustomerLocationChangeRequestStatus.approved
        )
        .count()
    )
    rejected_count = (
        db.query(CustomerLocationChangeRequest)
        .filter(
            CustomerLocationChangeRequest.status
            == CustomerLocationChangeRequestStatus.rejected
        )
        .count()
    )
    return {
        "location_change_requests": requests,
        "pending_location_change_count": pending_count,
        "approved_location_change_count": approved_count,
        "rejected_location_change_count": rejected_count,
        "location_change_filter": status.value if status else "",
    }


def approve_request(
    db: Session,
    *,
    request_id: str,
    actor_id: str | None,
    actor_name: str | None,
    review_note: str | None,
    actor_type: AuditActorType = AuditActorType.user,
) -> CustomerLocationChangeRequest:
    location_request = db.get(CustomerLocationChangeRequest, request_id)
    if not location_request:
        raise HTTPException(
            status_code=404, detail="Location correction request not found"
        )
    if location_request.status != CustomerLocationChangeRequestStatus.pending:
        raise HTTPException(
            status_code=400, detail="Location correction request already processed"
        )

    subscriber = db.get(Subscriber, location_request.subscriber_id)
    if not subscriber:
        raise HTTPException(status_code=404, detail="Subscriber account not found")

    address = _ensure_target_address(db, subscriber, location_request)
    address.latitude = float(location_request.requested_latitude)
    address.longitude = float(location_request.requested_longitude)
    address.geom = gis_service._point_wkt(address.longitude, address.latitude)
    _upsert_geo_location_for_address(db, address)

    now = datetime.now(UTC)
    location_request.address_id = address.id
    location_request.status = CustomerLocationChangeRequestStatus.approved
    location_request.review_note = (review_note or "").strip() or None
    location_request.reviewed_by_actor_id = actor_id
    location_request.reviewed_by_actor_name = actor_name
    location_request.reviewed_at = now
    location_request.applied_at = now
    db.commit()
    db.refresh(location_request)

    _audit(
        db,
        actor_id=actor_id,
        actor_type=actor_type,
        action="customer_location_change_approved",
        entity_id=str(location_request.id),
        metadata={
            "subscriber_id": str(subscriber.id),
            "address_id": str(address.id),
            "review_note": location_request.review_note,
            "requested_latitude": location_request.requested_latitude,
            "requested_longitude": location_request.requested_longitude,
        },
    )
    return location_request


def reject_request(
    db: Session,
    *,
    request_id: str,
    actor_id: str | None,
    actor_name: str | None,
    review_note: str | None,
) -> CustomerLocationChangeRequest:
    location_request = db.get(CustomerLocationChangeRequest, request_id)
    if not location_request:
        raise HTTPException(
            status_code=404, detail="Location correction request not found"
        )
    if location_request.status != CustomerLocationChangeRequestStatus.pending:
        raise HTTPException(
            status_code=400, detail="Location correction request already processed"
        )

    location_request.status = CustomerLocationChangeRequestStatus.rejected
    location_request.review_note = (review_note or "").strip() or None
    location_request.reviewed_by_actor_id = actor_id
    location_request.reviewed_by_actor_name = actor_name
    location_request.reviewed_at = datetime.now(UTC)
    db.commit()
    db.refresh(location_request)

    _audit(
        db,
        actor_id=actor_id,
        actor_type=AuditActorType.user,
        action="customer_location_change_rejected",
        entity_id=str(location_request.id),
        metadata={
            "subscriber_id": str(location_request.subscriber_id),
            "review_note": location_request.review_note,
        },
    )
    return location_request


# --- Address geocoding on customer self-service save ----------------------------
#
# Customer profile edits write the typed address onto the Subscriber, but
# coordinates live on the service Address (same place staff geocoding and the
# approved map pin write). This best-effort helper resolves the typed address to
# coordinates and back-fills them so a self-service address actually lands on the
# map and can be serviceability-checked — WITHOUT overwriting an existing pin.


def geocode_service_address(
    db: Session, subscriber: Subscriber, *, force: bool = False
) -> dict[str, Any] | None:
    """Geocode the subscriber's typed service address and back-fill coordinates.

    Best-effort and never raises — geocoding is advisory, and many addresses
    (especially in Nigeria) won't resolve cleanly. Skips when the service
    Address already has coordinates so an approved/manual map pin is never
    silently moved (pass ``force=True`` to re-pin from the typed address).
    Returns ``{latitude, longitude, display_name}`` when it set coordinates,
    else ``None``.
    """
    composed = {
        "address_line1": subscriber.address_line1,
        "address_line2": subscriber.address_line2,
        "city": subscriber.city,
        "region": subscriber.region,
        "postal_code": subscriber.postal_code,
        "country_code": subscriber.country_code,
    }
    if not (composed["address_line1"] or "").strip():
        return None
    try:
        address = _resolve_service_address(db, str(subscriber.id))
        if address is None:
            address = Address(
                subscriber_id=subscriber.id,
                address_type=AddressType.service,
                label="Primary service",
                is_primary=True,
                **composed,
            )
            db.add(address)
            db.flush()
        elif (
            not force and address.latitude is not None and address.longitude is not None
        ):
            return None  # preserve the existing pin

        # Back-off: an address that won't resolve shouldn't hit the geocoder on
        # every profile save. Stamp each attempt and skip if one was made within
        # the retry window (0 disables the back-off; force bypasses it).
        retry_days = _gis_setting_int(db, "location_geocode_retry_days", 7)
        meta = dict(subscriber.metadata_ or {})
        if not force and retry_days > 0:
            last = meta.get("geocode_attempted_at")
            if last:
                try:
                    last_dt = datetime.fromisoformat(str(last))
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.replace(tzinfo=UTC)
                    if datetime.now(UTC) - last_dt < timedelta(days=retry_days):
                        return None
                except ValueError:
                    pass
        meta["geocode_attempted_at"] = datetime.now(UTC).isoformat()
        subscriber.metadata_ = meta
        db.flush()

        result = geocoding_service.geocode_address(db, dict(composed))
        lat = result.get("latitude")
        lon = result.get("longitude")
        if lat is None or lon is None:
            return None
        address.latitude = float(lat)
        address.longitude = float(lon)
        # Match approve_request's existing arg order for Address.geom.
        address.geom = gis_service._point_wkt(address.longitude, address.latitude)
        _upsert_geo_location_for_address(db, address)
        db.flush()
        return {
            "latitude": float(lat),
            "longitude": float(lon),
            "display_name": _address_label(address, subscriber),
        }
    except Exception:  # noqa: BLE001 - geocoding is advisory, never block a save
        logger.warning(
            "address geocode-on-save skipped for subscriber %s",
            subscriber.id,
            exc_info=True,
        )
        return None


# --- Pin auto-approval ----------------------------------------------------------
#
# A small pin nudge from an already-approved location is inherently safe and is
# the bulk of correction requests ("move my pin to my actual rooftop"). Those
# are auto-approved; first pins, large moves, and (optionally) pins outside
# coverage are left in the manual review queue.


def _gis_setting_raw(db: Session, key: str) -> str | None:
    row = db.scalars(
        select(DomainSetting)
        .where(DomainSetting.domain == SettingDomain.gis)
        .where(DomainSetting.key == key)
        .where(DomainSetting.is_active.is_(True))
    ).first()
    if not row:
        return None
    if row.value_text is not None:
        return row.value_text
    if row.value_json is not None:
        return str(row.value_json)
    return None


def _gis_setting_bool(db: Session, key: str, default: bool) -> bool:
    raw = _gis_setting_raw(db, key)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _gis_setting_int(db: Session, key: str, default: int) -> int:
    raw = _gis_setting_raw(db, key)
    if raw is None:
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres between two lat/lon points."""
    r = 6371000.0
    p1, p2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dlmb = radians(lon2 - lon1)
    a = sin(dphi / 2) ** 2 + cos(p1) * cos(p2) * sin(dlmb / 2) ** 2
    return 2 * r * asin(sqrt(a))


def _coverage_areas_exist(db: Session) -> bool:
    return (
        db.query(GeoArea.id)
        .filter(GeoArea.is_active.is_(True))
        .filter(GeoArea.area_type.in_(_COVERAGE_AREA_TYPES))
        .first()
        is not None
    )


def _recent_auto_approvals(db: Session, subscriber_id, cutoff: datetime) -> int:
    """Count this subscriber's prior *auto*-approved pin moves since ``cutoff``.

    Identified by the system reviewer name so manual approvals don't count. This
    is what bounds incremental drift: many small hops can't each auto-approve."""
    return (
        db.query(CustomerLocationChangeRequest)
        .filter(CustomerLocationChangeRequest.subscriber_id == subscriber_id)
        .filter(
            CustomerLocationChangeRequest.status
            == CustomerLocationChangeRequestStatus.approved
        )
        .filter(
            CustomerLocationChangeRequest.reviewed_by_actor_name
            == AUTO_VERIFICATION_ACTOR_NAME
        )
        .filter(CustomerLocationChangeRequest.reviewed_at >= cutoff)
        .count()
    )


def evaluate_auto_approval(
    db: Session,
    *,
    subscriber_id,
    current_latitude: float | None,
    current_longitude: float | None,
    requested_latitude: float,
    requested_longitude: float,
) -> tuple[bool, str, dict[str, Any]]:
    """Decide whether a pin correction can be auto-approved.

    Returns ``(approved, reason, signals)``. Conservative by design: only a
    small move from an existing approved pin auto-approves; first pins and large
    moves go to manual review. When ``location_auto_require_coverage`` is on and
    coverage polygons exist, the pin must fall inside one. A rolling-window rate
    limit bounds cumulative drift, so repeated small hops can't auto-approve
    their way across town.
    """
    signals: dict[str, Any] = {}
    if not _gis_setting_bool(db, "location_auto_approve_enabled", True):
        return False, "auto-approval disabled", signals

    if current_latitude is None or current_longitude is None:
        signals["first_pin"] = True
        return False, "first pin has no baseline to verify against", signals

    radius_m = _gis_setting_int(db, "location_auto_approve_radius_m", 100)
    distance_m = _haversine_m(
        current_latitude, current_longitude, requested_latitude, requested_longitude
    )
    signals["move_distance_m"] = round(distance_m, 1)
    signals["radius_m"] = radius_m
    if distance_m > radius_m:
        return False, f"pin moved {distance_m:.0f} m (over {radius_m} m)", signals

    try:
        containing = gis_service.GeoAreas.find_containing(
            db, requested_latitude, requested_longitude
        )
        in_coverage = any(a.area_type in _COVERAGE_AREA_TYPES for a in containing)
    except Exception:  # noqa: BLE001 - spatial backend may be unavailable
        logger.debug("coverage containment check failed", exc_info=True)
        in_coverage = False
    signals["in_coverage"] = in_coverage
    if _gis_setting_bool(db, "location_auto_require_coverage", False):
        if _coverage_areas_exist(db) and not in_coverage:
            return False, "pin falls outside service coverage", signals

    # Rate limit: cap auto-approvals per rolling window so a customer can't drift
    # their pin across town in repeated small hops (each ≤ radius).
    window_days = _gis_setting_int(db, "location_auto_approve_window_days", 30)
    max_per_window = _gis_setting_int(db, "location_auto_approve_max_per_window", 1)
    if window_days > 0 and max_per_window > 0:
        cutoff = datetime.now(UTC) - timedelta(days=window_days)
        recent = _recent_auto_approvals(db, subscriber_id, cutoff)
        signals["recent_auto_approvals"] = recent
        if recent >= max_per_window:
            return (
                False,
                f"auto-approval limit reached ({recent} in {window_days}d) — "
                "needs manual review",
                signals,
            )

    return True, f"auto-approved: pin moved {distance_m:.0f} m", signals
