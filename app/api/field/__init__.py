from datetime import datetime

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.field.attachments import router as attachments_router
from app.api.field.attendance import router as attendance_router
from app.api.field.devices import router as devices_router
from app.api.field.equipment import router as equipment_router
from app.api.field.expense_requests import router as expense_requests_router
from app.api.field.fiber import router as fiber_router
from app.api.field.inventory import router as inventory_router
from app.api.field.locations import router as locations_router
from app.api.field.manager import router as manager_router
from app.api.field.map_assets import router as map_assets_router
from app.api.field.material_requests import router as material_requests_router
from app.api.field.materials import router as materials_router
from app.api.field.notes import router as notes_router
from app.api.field.schedule import router as schedule_router
from app.api.field.transitions import router as transitions_router
from app.api.field.vendor_devices import router as vendor_devices_router
from app.api.field.vendor_map import router as vendor_map_router
from app.api.field.voice import router as voice_router
from app.api.field.worklogs import router as worklogs_router
from app.schemas.common import ListResponse
from app.schemas.field import (
    FieldJobChatMessageCreate,
    FieldJobChatMessageRead,
    FieldJobChatThread,
    FieldJobDestination,
    FieldJobDestinationsResponse,
    FieldJobDetail,
    FieldJobLocation,
    FieldJobLocationUpdate,
    FieldJobSummary,
    FieldMeResponse,
)
from app.services.auth_dependencies import require_user_auth
from app.services.field.chat import field_job_chat
from app.services.field.jobs import field_jobs

router = APIRouter(prefix="/field", tags=["field"])
router.include_router(attendance_router)
router.include_router(attachments_router)
router.include_router(devices_router)
router.include_router(equipment_router)
router.include_router(expense_requests_router)
router.include_router(fiber_router)
router.include_router(inventory_router)
router.include_router(locations_router)
router.include_router(manager_router)
router.include_router(map_assets_router)
router.include_router(material_requests_router)
router.include_router(materials_router)
router.include_router(notes_router)
router.include_router(schedule_router)
router.include_router(transitions_router)
router.include_router(vendor_devices_router)
router.include_router(vendor_map_router)
router.include_router(voice_router)
router.include_router(worklogs_router)


@router.get("/me", response_model=FieldMeResponse)
def field_me(
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    return field_jobs.me(db, auth)


@router.get("/jobs", response_model=ListResponse[FieldJobSummary])
def list_field_jobs(
    status: str | None = None,
    date_from: datetime | None = Query(default=None, alias="from"),
    date_to: datetime | None = Query(default=None, alias="to"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    items = field_jobs.list(
        db,
        auth,
        status=status,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )
    return {"items": items, "count": len(items), "limit": limit, "offset": offset}


@router.get("/jobs/{crm_work_order_id}", response_model=FieldJobDetail)
def get_field_job(
    crm_work_order_id: str,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    return field_jobs.get_detail(db, auth, crm_work_order_id)


@router.get(
    "/jobs/{crm_work_order_id}/destinations",
    response_model=FieldJobDestinationsResponse,
)
def list_field_job_destinations(
    crm_work_order_id: str,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    items = field_jobs.list_destinations(db, auth, crm_work_order_id)
    return {
        "items": [FieldJobDestination(**item) for item in items],
        "count": len(items),
    }


@router.get(
    "/jobs/{crm_work_order_id}/chat",
    response_model=FieldJobChatThread,
)
def get_field_job_chat(
    crm_work_order_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    return field_job_chat.get_thread(db, auth, crm_work_order_id, limit=limit)


@router.post(
    "/jobs/{crm_work_order_id}/chat/messages",
    response_model=FieldJobChatMessageRead,
    status_code=status.HTTP_201_CREATED,
)
def send_field_job_chat_message(
    crm_work_order_id: str,
    payload: FieldJobChatMessageCreate,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    return field_job_chat.send_message(db, auth, crm_work_order_id, body=payload.body)


@router.patch(
    "/jobs/{crm_work_order_id}/location",
    response_model=FieldJobLocation,
)
def update_field_job_location(
    crm_work_order_id: str,
    payload: FieldJobLocationUpdate,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    return field_jobs.update_location(
        db,
        auth,
        crm_work_order_id,
        latitude=payload.latitude,
        longitude=payload.longitude,
    )
