from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.schemas.common import ListResponse
from app.schemas.field import FieldMaterialRequestCreate, FieldMaterialRequestRead
from app.services.auth_dependencies import require_user_auth
from app.services.field.material_requests import field_material_requests

router = APIRouter(prefix="/material-requests", tags=["field-material-requests"])


@router.get("", response_model=ListResponse[FieldMaterialRequestRead])
def list_field_material_requests(
    work_order_id: str | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    items = field_material_requests.list_mine(
        db,
        auth,
        crm_work_order_id=work_order_id,
        status=status_filter,
        limit=limit,
        offset=offset,
    )
    return {"items": items, "count": len(items), "limit": limit, "offset": offset}


@router.post(
    "",
    response_model=FieldMaterialRequestRead,
    status_code=status.HTTP_201_CREATED,
)
def create_field_material_request(
    payload: FieldMaterialRequestCreate,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    return field_material_requests.create(
        db,
        auth,
        crm_work_order_id=payload.work_order_id,
        priority=payload.priority,
        notes=payload.notes,
        source_warehouse_code=payload.source_warehouse_code,
        items=[item.model_dump() for item in payload.items],
    )


@router.get("/{material_request_id}", response_model=FieldMaterialRequestRead)
def get_field_material_request(
    material_request_id: str,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    return field_material_requests.get(db, auth, material_request_id)


@router.post("/{material_request_id}/submit", response_model=FieldMaterialRequestRead)
def submit_field_material_request(
    material_request_id: str,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    return field_material_requests.submit(db, auth, material_request_id)
