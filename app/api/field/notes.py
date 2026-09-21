from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.schemas.field import FieldNoteCreate, FieldNoteRead
from app.services.auth_dependencies import require_user_auth
from app.services.db_session_adapter import db_session_adapter
from app.services.field.note_commands import (
    CreateFieldWorkOrderNote,
    FieldNoteCommandError,
    create_field_work_order_note,
)
from app.services.owner_commands import CommandContext

router = APIRouter(tags=["field-notes"])


@router.post(
    "/jobs/{crm_work_order_id}/notes",
    response_model=FieldNoteRead,
    status_code=status.HTTP_201_CREATED,
)
def create_field_note(
    crm_work_order_id: str,
    payload: FieldNoteCreate,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    request_id = payload.client_ref or uuid4()
    principal_id = UUID(str(auth["principal_id"]))
    try:
        db_session_adapter.release_read_transaction(db)
        return create_field_work_order_note(
            db,
            CreateFieldWorkOrderNote(
                context=CommandContext(
                    command_id=request_id,
                    correlation_id=request_id,
                    actor=f"user:{principal_id}",
                    scope="field:work_order_notes:write",
                    reason="field_work_order_note_creation",
                    idempotency_key=str(request_id),
                ),
                requester_system_user_id=principal_id,
                work_order_public_id=crm_work_order_id,
                request_id=request_id,
                body=payload.body,
                is_internal=payload.is_internal,
                attachment_ids=tuple(payload.attachment_ids),
            ),
        )
    except FieldNoteCommandError as exc:
        if exc.code.endswith(
            ("requester_not_found", "work_order_not_found", "attachment_not_found")
        ):
            status_code = status.HTTP_404_NOT_FOUND
        elif exc.code.endswith("attachment_forbidden"):
            status_code = status.HTTP_403_FORBIDDEN
        elif exc.code.endswith("invalid_request"):
            status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
        else:
            status_code = status.HTTP_409_CONFLICT
        raise HTTPException(
            status_code=status_code,
            detail={"code": exc.code, "message": exc.message, "details": exc.details},
        ) from exc
