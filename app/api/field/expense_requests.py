from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.field.work_order_compat import resolve_work_order_id
from app.schemas.common import ListResponse
from app.schemas.field import (
    FieldAttachmentRead,
    FieldExpenseCategoryRead,
    FieldExpenseRequestCreate,
    FieldExpenseRequestRead,
    FieldExpenseRequestSubmit,
)
from app.services.auth_dependencies import require_user_auth
from app.services.db_session_adapter import db_session_adapter
from app.services.field.attachments import field_attachments
from app.services.field.expense_categories import (
    ExpenseCategoryQueryError,
    ListExpenseCategories,
    list_expense_categories,
)
from app.services.field.expense_requests import (
    ExpenseRequestLineInput,
    FieldExpenseRequestError,
    SubmitFieldExpenseRequest,
    field_expense_requests,
    submit_field_expense_request_command,
)
from app.services.owner_commands import CommandContext

router = APIRouter(prefix="/expense-requests", tags=["field-expense-requests"])


def _command_context(auth: dict, *, request_id: UUID, reason: str) -> CommandContext:
    principal_id = UUID(str(auth["principal_id"]))
    return CommandContext(
        command_id=request_id,
        correlation_id=request_id,
        actor=f"user:{principal_id}",
        scope="field:expense_requests:write",
        reason=reason,
        idempotency_key=str(request_id),
    )


def _expense_command_error(exc: FieldExpenseRequestError) -> HTTPException:
    if exc.code.endswith("work_order_not_found") or exc.code.endswith(
        "requester_not_found"
    ):
        status_code = 404
    elif exc.code.endswith("invalid_request"):
        status_code = 422
    else:
        status_code = 409
    return HTTPException(
        status_code=status_code,
        detail={"code": exc.code, "message": exc.message, "details": exc.details},
    )


@router.get("/categories", response_model=ListResponse[FieldExpenseCategoryRead])
def list_field_expense_categories(
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    del auth
    try:
        items = list_expense_categories(db, ListExpenseCategories())
    except ExpenseCategoryQueryError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    return {"items": items, "count": len(items), "limit": len(items), "offset": 0}


@router.post("/receipts", response_model=FieldAttachmentRead, status_code=201)
def upload_field_expense_receipt(
    file: UploadFile = File(...),
    work_order_id: str | None = Form(default=None),
    crm_work_order_id: str | None = Form(default=None, deprecated=True),
    client_ref: UUID | None = Form(default=None),
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    resolved_work_order_id = resolve_work_order_id(
        work_order_id=work_order_id, crm_work_order_id=crm_work_order_id
    )
    if resolved_work_order_id is None:
        raise HTTPException(status_code=422, detail="work_order_id is required")
    return field_attachments.create(
        db,
        auth,
        kind="document",
        file_name=file.filename or "receipt",
        mime_type=file.content_type,
        content=file.file.read(),
        client_ref=client_ref,
        crm_work_order_id=resolved_work_order_id,
    )


@router.get("", response_model=ListResponse[FieldExpenseRequestRead])
def list_field_expense_requests(
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
    items = field_expense_requests.list_mine(
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
    response_model=FieldExpenseRequestRead,
    status_code=status.HTTP_201_CREATED,
)
def create_field_expense_request(
    payload: FieldExpenseRequestCreate,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    return field_expense_requests.create(
        db,
        auth,
        crm_work_order_id=payload.work_order_id,
        purpose=payload.purpose,
        expense_date=payload.expense_date,
        currency=payload.currency,
        notes=payload.notes,
        client_ref=payload.client_ref,
        items=[item.model_dump() for item in payload.items],
    )


@router.post(
    "/submit",
    response_model=FieldExpenseRequestRead,
    status_code=status.HTTP_201_CREATED,
)
def create_and_submit_field_expense_request(
    payload: FieldExpenseRequestSubmit,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    try:
        db_session_adapter.release_read_transaction(db)
        return submit_field_expense_request_command(
            db,
            SubmitFieldExpenseRequest(
                context=_command_context(
                    auth,
                    request_id=payload.client_ref,
                    reason="field_expense_request_submission",
                ),
                requester_person_id=UUID(str(auth["principal_id"])),
                work_order_public_id=payload.work_order_id,
                request_id=payload.client_ref,
                purpose=payload.purpose,
                expense_date=payload.expense_date,
                currency=payload.currency,
                notes=payload.notes,
                items=tuple(
                    ExpenseRequestLineInput(**item.model_dump())
                    for item in payload.items
                ),
            ),
        )
    except FieldExpenseRequestError as exc:
        raise _expense_command_error(exc) from exc


@router.get("/{expense_request_id}", response_model=FieldExpenseRequestRead)
def get_field_expense_request(
    expense_request_id: str,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    return field_expense_requests.get(db, auth, expense_request_id)


@router.post("/{expense_request_id}/submit", response_model=FieldExpenseRequestRead)
def submit_field_expense_request(
    expense_request_id: str,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    return field_expense_requests.submit(db, auth, expense_request_id)


@router.post("/{expense_request_id}/cancel", response_model=FieldExpenseRequestRead)
def cancel_field_expense_request(
    expense_request_id: str,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    return field_expense_requests.cancel(db, auth, expense_request_id)
