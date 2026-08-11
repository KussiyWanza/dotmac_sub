"""Read helpers for the admin field maps (live map + movement playback).

Backs the dispatch ``live-map`` and ``movement-playback`` admin pages over
sub's native Phase-2 field tracking data:

* ``field_tech_presence`` — latest technician position snapshot (plain lat/lng
  floats, ``app/models/field_location.py``).
* ``field_work_order_movements`` — technician travel legs
  (``app/models/field_movement.py``).

Technician positions are plain lat/lng columns (no PostGIS geometry), so these
feeds are ordinary JSON. The ``ST_AsGeoJSON`` path is only used by the vendor
route geometry service (``app/services/vendor_routes_api.py``).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.dispatch import TechnicianProfile
from app.models.field_location import FieldTechPresence
from app.models.field_movement import FieldWorkOrderMovement
from app.models.subscriber import Address, Subscriber
from app.models.system_user import SystemUser
from app.models.work_order import WorkOrder
from app.schemas.field import (
    FieldLiveMapFeed,
    FieldLiveMapFeedQuery,
    FieldLiveMapPosition,
    FieldLiveMapSearchQuery,
    FieldLiveMapSearchResponse,
    FieldLiveMapSearchResult,
)
from app.services.field.jobs import _location
from app.services.service_address import service_address


def _now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso(value: datetime | None) -> str | None:
    aware = _as_utc(value)
    return aware.isoformat() if aware else None


def _technician_label(profile: TechnicianProfile | None) -> str:
    """Human label for a technician, mirroring the dispatch web service."""
    if profile is None:
        return "Unknown technician"
    user = getattr(profile, "system_user", None)
    if user is not None:
        name = (user.display_name or f"{user.first_name} {user.last_name}").strip()
        if name:
            return name
        if user.email:
            return user.email
    metadata = profile.metadata_ or {}
    for key in ("name", "display_name"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return profile.title or profile.crm_person_id or str(profile.person_id)


def list_technician_positions(
    db: Session,
    query: FieldLiveMapFeedQuery,
) -> FieldLiveMapFeed:
    """Latest known position for each technician sharing location.

    Returns a small JSON feed the live-map polls every ~30s.
    """
    rows = (
        db.query(FieldTechPresence)
        .filter(FieldTechPresence.location_sharing_enabled.is_(True))
        .filter(FieldTechPresence.last_latitude.isnot(None))
        .filter(FieldTechPresence.last_longitude.isnot(None))
        .order_by(FieldTechPresence.last_location_at.desc())
        .limit(query.limit)
        .all()
    )
    now = _now()
    items: list[FieldLiveMapPosition] = []
    live_count = 0
    for presence in rows:
        latitude = presence.last_latitude
        longitude = presence.last_longitude
        if latitude is None or longitude is None:
            continue
        last_at = _as_utc(presence.last_location_at)
        is_live = bool(
            last_at and (now - last_at).total_seconds() <= query.stale_after_seconds
        )
        if is_live:
            live_count += 1
        items.append(
            FieldLiveMapPosition(
                technician_id=presence.technician_id,
                person_id=presence.person_id,
                label=_technician_label(presence.technician),
                status=presence.status,
                latitude=latitude,
                longitude=longitude,
                accuracy_m=presence.last_location_accuracy_m,
                last_location_at=_as_utc(presence.last_location_at),
                is_live=is_live,
            )
        )
    return FieldLiveMapFeed(
        count=len(items),
        live_count=live_count,
        stale_after_seconds=query.stale_after_seconds,
        items=items,
    )


def _address_text(address: Address | None) -> str | None:
    if address is None:
        return None
    parts = (
        address.address_line1,
        address.address_line2,
        address.city,
        address.region,
    )
    text = ", ".join(part.strip() for part in parts if part and part.strip())
    return text or None


def search_live_map(
    db: Session,
    search: FieldLiveMapSearchQuery,
) -> FieldLiveMapSearchResponse:
    """Search sharing technicians and mapped native work orders for admins."""
    term = search.query.strip()
    if not term:
        return FieldLiveMapSearchResponse(query="", count=0, items=[])
    like = f"%{term}%"
    items: list[FieldLiveMapSearchResult] = []

    technician_rows = (
        db.query(FieldTechPresence)
        .join(
            TechnicianProfile,
            TechnicianProfile.id == FieldTechPresence.technician_id,
        )
        .outerjoin(SystemUser, SystemUser.id == TechnicianProfile.system_user_id)
        .filter(FieldTechPresence.location_sharing_enabled.is_(True))
        .filter(FieldTechPresence.last_latitude.isnot(None))
        .filter(FieldTechPresence.last_longitude.isnot(None))
        .filter(
            or_(
                SystemUser.display_name.ilike(like),
                SystemUser.first_name.ilike(like),
                SystemUser.last_name.ilike(like),
                SystemUser.email.ilike(like),
                TechnicianProfile.title.ilike(like),
                TechnicianProfile.region.ilike(like),
                TechnicianProfile.crm_person_id.ilike(like),
            )
        )
        .order_by(FieldTechPresence.last_location_at.desc())
        .limit(search.limit)
        .all()
    )
    for presence in technician_rows:
        latitude = presence.last_latitude
        longitude = presence.last_longitude
        if latitude is None or longitude is None:
            continue
        items.append(
            FieldLiveMapSearchResult(
                kind="technician",
                id=str(presence.technician_id),
                label=_technician_label(presence.technician),
                detail=presence.status.replace("_", " "),
                latitude=latitude,
                longitude=longitude,
                status=presence.status,
            )
        )

    remaining = search.limit - len(items)
    if remaining > 0:
        service_street_match = (
            db.query(Address.id)
            .filter(Address.subscriber_id == WorkOrder.subscriber_id)
            .filter(
                or_(
                    Address.label.ilike(like),
                    Address.address_line1.ilike(like),
                    Address.address_line2.ilike(like),
                    Address.city.ilike(like),
                    Address.region.ilike(like),
                    Address.lga.ilike(like),
                    Address.postal_code.ilike(like),
                )
            )
            .exists()
        )
        work_orders = (
            db.query(WorkOrder)
            .join(Subscriber, Subscriber.id == WorkOrder.subscriber_id)
            .filter(WorkOrder.is_active.is_(True))
            .filter(
                or_(
                    WorkOrder.public_id.ilike(like),
                    WorkOrder.title.ilike(like),
                    WorkOrder.description.ilike(like),
                    WorkOrder.address.ilike(like),
                    WorkOrder.status.ilike(like),
                    WorkOrder.work_type.ilike(like),
                    WorkOrder.assigned_to_name.ilike(like),
                    WorkOrder.technician_name.ilike(like),
                    WorkOrder.technician_phone.ilike(like),
                    Subscriber.first_name.ilike(like),
                    Subscriber.last_name.ilike(like),
                    Subscriber.email.ilike(like),
                    Subscriber.phone.ilike(like),
                    Subscriber.account_number.ilike(like),
                    Subscriber.address_line1.ilike(like),
                    Subscriber.address_line2.ilike(like),
                    Subscriber.city.ilike(like),
                    Subscriber.region.ilike(like),
                    service_street_match,
                )
            )
            .order_by(
                WorkOrder.scheduled_start.asc().nullslast(),
                WorkOrder.created_at.desc(),
                WorkOrder.id.asc(),
            )
            .limit(remaining * 3)
            .all()
        )
        for work_order in work_orders:
            location = _location(work_order)
            canonical_address = service_address(db, work_order.subscriber_id)
            latitude = location.latitude
            longitude = location.longitude
            if latitude is None or longitude is None:
                latitude = canonical_address.latitude if canonical_address else None
                longitude = canonical_address.longitude if canonical_address else None
            if latitude is None or longitude is None:
                continue
            address_text = work_order.address or _address_text(canonical_address)
            items.append(
                FieldLiveMapSearchResult(
                    kind="work_order",
                    id=work_order.public_id,
                    label=work_order.title or work_order.public_id,
                    detail=address_text,
                    latitude=latitude,
                    longitude=longitude,
                    status=work_order.status,
                    href=f"/admin/dispatch/work-orders/{work_order.public_id}",
                )
            )
            if len(items) >= search.limit:
                break

    return FieldLiveMapSearchResponse(query=term, count=len(items), items=items)


def list_movement_work_orders(db: Session, *, limit: int = 200) -> list[dict]:
    """Distinct work orders that have recorded travel legs (playback picker)."""
    rows = (
        db.query(
            WorkOrder.public_id,
            WorkOrder.title,
        )
        .join(
            FieldWorkOrderMovement,
            WorkOrder.id == FieldWorkOrderMovement.work_order_mirror_id,
        )
        .order_by(FieldWorkOrderMovement.started_at.desc())
        .all()
    )
    seen: dict[str, str] = {}
    for public_id, title in rows:
        if public_id in seen:
            continue
        seen[public_id] = (title or "").strip() or public_id
        if len(seen) >= limit:
            break
    return [{"public_id": wo_id, "label": label} for wo_id, label in seen.items()]


def list_movement_points(
    db: Session,
    *,
    crm_work_order_id: str | None = None,
    technician_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 1000,
) -> dict:
    """Ordered travel points for a public work-order id and/or technician UUID.

    Each leg contributes its start point (at ``started_at``) and, once the
    technician has arrived, its arrival point (at ``arrived_at``). Points are
    returned in chronological order for the client-side scrubber.
    """
    query = db.query(FieldWorkOrderMovement)
    if crm_work_order_id:
        wo_id = (
            db.query(WorkOrder.id)
            .filter(WorkOrder.public_id == crm_work_order_id)
            .scalar()
        )
        if wo_id is None:
            return {"leg_count": 0, "point_count": 0, "points": []}
        query = query.filter(FieldWorkOrderMovement.work_order_mirror_id == wo_id)
    if technician_id:
        query = query.filter(
            FieldWorkOrderMovement.actor_technician_id == technician_id
        )
    if since is not None:
        query = query.filter(FieldWorkOrderMovement.started_at >= since)
    if until is not None:
        query = query.filter(FieldWorkOrderMovement.started_at <= until)
    legs = query.order_by(FieldWorkOrderMovement.started_at.asc()).limit(limit).all()

    points: list[dict] = []
    for leg in legs:
        if leg.start_latitude is not None and leg.start_longitude is not None:
            points.append(
                {
                    "latitude": leg.start_latitude,
                    "longitude": leg.start_longitude,
                    "captured_at": _iso(leg.started_at),
                    "kind": "start",
                    "status": leg.status,
                    "label": leg.destination_label,
                }
            )
        if leg.arrival_latitude is not None and leg.arrival_longitude is not None:
            points.append(
                {
                    "latitude": leg.arrival_latitude,
                    "longitude": leg.arrival_longitude,
                    "captured_at": _iso(leg.arrived_at or leg.started_at),
                    "kind": "arrival",
                    "status": leg.status,
                    "label": leg.destination_label,
                }
            )
    return {
        "leg_count": len(legs),
        "point_count": len(points),
        "points": points,
    }
