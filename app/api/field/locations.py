from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
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
from app.services.db_session_adapter import db_session_adapter
from app.services.field.location_tracking import (
    LocationPingCommand,
    field_location_tracking,
)
from app.services.field.routing import field_routing
from app.services.workforce_attendance import (
    WorkforceAttendanceError,
    WorkforceAttendanceService,
)

router = APIRouter(prefix="/locations", tags=["field-locations"])


@router.post("", response_model=LocationIngestResponse)
def ingest_locations(
    payload: LocationPingBatch,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    # record_batch is an owner command and requires a transaction-free
    # session at entry. require_user_auth's own lookup already opened a
    # read transaction on this request-scoped session; release it (it holds
    # no pending mutation) before entering the owner command boundary.
    db_session_adapter.release_read_transaction(db)
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
        "replays": [
            {"index": replay.index, "ping_id": str(replay.ping_id)}
            for replay in outcome.replays
        ],
    }


@router.put("/sharing", response_model=FieldPresenceRead)
def update_sharing(
    request: Request,
    payload: LocationSharingUpdate,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    if payload.enabled:
        if auth.get("principal_type") != "system_user":
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "authorization_failed",
                    "message": "Location sharing requires a technician staff account.",
                },
            )
        try:
            subject = UUID(str(auth["principal_id"]))
            WorkforceAttendanceService(db).require_checked_in_for_shift(
                subject=subject,
                request_id=str(
                    getattr(request.state, "request_id", "field-shift-gate")
                )[:160],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "authorization_failed",
                    "message": "Location sharing requires a technician staff account.",
                },
            ) from exc
        except WorkforceAttendanceError as exc:
            raise HTTPException(
                status_code=503 if exc.unavailable else 409,
                detail={"code": exc.code, "message": exc.message},
            ) from exc
    # set_sharing is likewise an owner command; see ingest_locations above.
    # The attendance query may also have opened a read transaction, so release
    # it only after the gate has consumed the authoritative attendance facts.
    db_session_adapter.release_read_transaction(db)
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
