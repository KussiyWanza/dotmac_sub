from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.schemas.field import (
    FieldPresenceRead,
    FieldRouteResponse,
    LocationIngestResponse,
    LocationPingBatch,
    LocationSharingUpdate,
)
from app.services.auth_dependencies import require_user_auth
from app.services.field.location_tracking import (
    LocationPingCommand,
    field_location_tracking,
)
from app.services.field.routing import field_routing

router = APIRouter(prefix="/locations", tags=["field-locations"])


@router.post("", response_model=LocationIngestResponse)
def ingest_locations(
    payload: LocationPingBatch,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    outcome = field_location_tracking.record_batch(
        db,
        auth,
        [LocationPingCommand(**ping.model_dump()) for ping in payload.pings],
    )
    return {
        "accepted": outcome.accepted,
        "errors": [
            {
                "index": issue.index,
                "code": issue.code,
                "detail": issue.detail,
            }
            for issue in outcome.errors
        ],
        "presence": outcome.presence,
        "transitions": [
            {
                "crm_work_order_id": transition.crm_work_order_id,
                "event": transition.event,
                "distance_m": transition.distance_m,
            }
            for transition in outcome.transitions
        ],
    }


@router.put("/sharing", response_model=FieldPresenceRead)
def update_sharing(
    payload: LocationSharingUpdate,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    return field_location_tracking.set_sharing(
        db,
        auth,
        enabled=payload.enabled,
        status=payload.status,
    )


@router.get("/me", response_model=FieldPresenceRead)
def get_my_presence(
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    return field_location_tracking.get_or_create_presence(db, auth)


@router.get("/route", response_model=FieldRouteResponse)
def my_day_route(
    start_lat: float = Query(ge=-90, le=90),
    start_lng: float = Query(ge=-180, le=180),
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    return {
        "route": field_routing.order_day_route(
            db,
            auth,
            start_latitude=start_lat,
            start_longitude=start_lng,
        )
    }
