from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.field.work_order_compat import resolve_work_order_id
from app.schemas.common import ListResponse
from app.schemas.field import (
    FieldMaterialRequestCreate,
    FieldMaterialRequestRead,
    FieldMaterialRequestSubmit,
)
from app.services.auth_dependencies import require_user_auth
from app.services.db_session_adapter import db_session_adapter
from app.services.field.material_requests import (
    CreateStaffMaterialRequest,
    MaterialRequestError,
    MaterialRequestLineInput,
    MaterialRequestPriority,
    create_staff_material_request,
    field_material_requests,
)
from app.services.owner_commands import CommandContext

router = APIRouter(prefix="/material-requests", tags=["field-material-requests"])


def _context(auth: dict, request_id: UUID) -> CommandContext:
    principal_id = UUID(str(auth["principal_id"]))
    return CommandContext(
        command_id=request_id,
        correlation_id=request_id,
        actor=f"user:{principal_id}",
        scope="field:material_requests:write",
        reason="field_material_request_submission",
        idempotency_key=str(request_id),
    )


def _material_outcome(outcome) -> dict:
    return {
        "id": outcome.id,
        "work_order_id": outcome.work_order_public_id,
        "crm_material_request_id": None,
        "requested_by_person_id": outcome.requested_by_person_id,
        "requested_by_system_user_id": outcome.requested_by_system_user_id,
        "status": outcome.status,
        "priority": outcome.priority,
        "notes": outcome.notes,
        "source_warehouse_code": outcome.source_warehouse_code,
        "support_system": outcome.support_system,
        "support_reference": outcome.support_reference,
        "support_status": outcome.support_status,
        "submitted_at": outcome.submitted_at,
        "approved_at": outcome.approved_at,
        "rejected_at": outcome.rejected_at,
        "fulfilled_at": outcome.fulfilled_at,
        "created_at": outcome.created_at,
        "updated_at": outcome.updated_at,
        "items": [
            {
                "id": item.id,
                "item_id": item.item_id,
                "sku": item.sku,
                "name": item.name,
                "unit": item.unit,
                "quantity": item.quantity,
                "notes": item.notes,
                "serial_numbers": list(item.serial_numbers),
            }
            for item in outcome.items
        ],
    }


@router.get("", response_model=ListResponse[FieldMaterialRequestRead])
def list_field_material_requests(
    work_order_id: str | None = None,
    crm_work_order_id: str | None = Query(default=None, deprecated=True),
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    resolved_work_order_id = resolve_work_order_id(
        work_order_id=work_order_id, crm_work_order_id=crm_work_order_id
    )
    items = field_material_requests.list_mine(
        db,
        auth,
        crm_work_order_id=resolved_work_order_id,
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


@router.post(
    "/submit",
    response_model=FieldMaterialRequestRead,
    status_code=status.HTTP_201_CREATED,
)
def create_and_submit_field_material_request(
    payload: FieldMaterialRequestSubmit,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    try:
        db_session_adapter.release_read_transaction(db)
        outcome = create_staff_material_request(
            db,
            CreateStaffMaterialRequest(
                context=_context(auth, payload.client_ref),
                work_order_id=None,
                work_order_public_id=payload.work_order_id,
                request_id=payload.client_ref,
                priority=MaterialRequestPriority(payload.priority),
                source_warehouse_code=payload.source_warehouse_code,
                notes=payload.notes,
                items=tuple(
                    MaterialRequestLineInput(
                        item_id=item.item_id,
                        quantity=item.quantity,
                        notes=item.notes,
                        serial_numbers=tuple(item.serial_numbers),
                    )
                    for item in payload.items
                ),
            ),
        )
    except MaterialRequestError as exc:
        status_code = 404 if exc.code.endswith("not_found") else 409
        if exc.code.endswith("invalid_request"):
            status_code = 422
        raise HTTPException(
            status_code=status_code,
            detail={"code": exc.code, "message": exc.message, "details": exc.details},
        ) from exc
    return _material_outcome(outcome)


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
