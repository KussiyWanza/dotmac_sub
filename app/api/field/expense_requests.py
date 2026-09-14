from uuid import UUID, uuid4

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
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
    FieldExpenseApproverRead,
    FieldExpenseBankRead,
    FieldExpenseCategoryRead,
    FieldExpenseDestinationRead,
    FieldExpenseDestinationVerify,
    FieldExpenseFormContextRead,
    FieldExpenseProfileDestinationRead,
    FieldExpenseRequestCreate,
    FieldExpenseRequestItemRead,
    FieldExpenseRequestRead,
    FieldExpenseRequestSubmit,
    FieldExpenseVendorRead,
)
from app.services.auth_dependencies import require_user_auth
from app.services.db_session_adapter import db_session_adapter
from app.services.dotmac_erp.expense_form_contracts import ExpenseDestinationMode
from app.services.field.attachments import field_attachments
from app.services.field.expense_categories import (
    ExpenseCategoryQueryError,
    ListExpenseCategories,
    list_expense_categories,
)
from app.services.field.expense_requests import (
    CancelFieldExpenseRequest,
    ExpenseRequestLineInput,
    ExpenseRequestStatus,
    ExpenseRequestView,
    ExpenseWorkOrderIdentity,
    FieldExpenseRequestError,
    GetFieldExpenseFormContext,
    ListFieldExpenseVendors,
    RequesterExpenseDetailQuery,
    RequesterExpenseHistoryQuery,
    ResolveFieldExpenseSubmissionContext,
    SubmitFieldExpenseRequest,
    VerifyFieldExpenseDestination,
    cancel_field_expense_request_command,
    field_expense_requests,
    get_requester_expense_request,
    list_expense_vendors,
    list_requester_expense_requests,
    resolve_field_expense_submission_context,
    submit_field_expense_request_command,
    verify_field_expense_destination,
)
from app.services.field.expense_requests import (
    get_field_expense_form_context as resolve_field_expense_form_context,
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
    if exc.code.endswith(
        ("work_order_not_found", "requester_not_found", "request_not_found")
    ):
        status_code = 404
    elif exc.code.endswith("_unavailable"):
        status_code = 503
    elif exc.code.endswith(
        ("invalid_request", "approver_invalid", "destination_invalid")
    ):
        status_code = 422
    else:
        status_code = 409
    return HTTPException(
        status_code=status_code,
        detail={"code": exc.code, "message": exc.message, "details": exc.details},
    )


def _history_status(value: str | None) -> ExpenseRequestStatus | None:
    if value is None:
        return None
    try:
        return ExpenseRequestStatus(value.strip().lower())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid expense status") from exc


def _expense_read(view: ExpenseRequestView) -> FieldExpenseRequestRead:
    return FieldExpenseRequestRead(
        id=view.id,
        work_order_id=view.work_order_id,
        crm_expense_request_id=view.crm_expense_request_id,
        requested_by_person_id=view.requested_by_person_id,
        requested_by_system_user_id=view.requested_by_system_user_id,
        requested_by_name=view.requested_by_name,
        selected_approver_erp_id=view.selected_approver_erp_id,
        selected_approver_name=view.selected_approver_name,
        selected_approver_email=view.selected_approver_email,
        payment_destination_mode=view.payment_destination_mode,
        recipient_bank_name=view.recipient_bank_name,
        masked_account_number=view.masked_account_number,
        verified_beneficiary_name=view.verified_beneficiary_name,
        status=view.status.value,
        purpose=view.purpose,
        expense_date=view.expense_date,
        currency=view.currency,
        notes=view.notes,
        rejection_reason=view.rejection_reason,
        expense_system=view.expense_system,
        expense_claim_reference=view.expense_claim_reference,
        expense_claim_number=view.expense_claim_number,
        expense_claim_status=view.expense_claim_status,
        erp_sync_status=(
            view.erp_sync_status.value if view.erp_sync_status is not None else None
        ),
        erp_sync_error=view.erp_sync_error,
        payment_status=view.payment_status,
        payment_intent_id=view.payment_intent_id,
        payment_error=view.payment_error,
        client_ref=view.client_ref,
        total_amount=view.total_amount,
        submitted_at=view.submitted_at,
        approved_at=view.approved_at,
        rejected_at=view.rejected_at,
        paid_at=view.paid_at,
        created_at=view.created_at,
        updated_at=view.updated_at,
        items=[
            FieldExpenseRequestItemRead(
                id=item.id,
                category_code=item.category_code,
                category_name=item.category_name,
                description=item.description,
                amount=item.amount,
                expense_date=item.expense_date,
                vendor_name=item.vendor_name,
                receipt_url=item.receipt_url,
                receipt_attachment_id=item.receipt_attachment_id,
                notes=item.notes,
            )
            for item in view.items
        ],
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


@router.get("/vendors", response_model=ListResponse[FieldExpenseVendorRead])
def list_field_expense_vendors(
    q: str | None = Query(default=None, max_length=120),
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    del auth
    items = list_expense_vendors(
        db=db,
        query=ListFieldExpenseVendors(search=q, limit=limit, offset=offset),
    )
    return {
        "items": [
            FieldExpenseVendorRead(id=item.id, label=item.label) for item in items
        ],
        "count": len(items),
        "limit": limit,
        "offset": offset,
    }


@router.get("/form-context", response_model=FieldExpenseFormContextRead)
def get_field_expense_form_context(
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
) -> FieldExpenseFormContextRead:
    try:
        context = resolve_field_expense_form_context(
            db=db,
            query=GetFieldExpenseFormContext(
                requester_system_user_id=UUID(str(auth["principal_id"]))
            ),
        )
    except FieldExpenseRequestError as exc:
        raise _expense_command_error(exc) from exc
    return FieldExpenseFormContextRead(
        approvers=[
            FieldExpenseApproverRead(
                erp_employee_id=approver.erp_employee_id,
                system_user_id=approver.system_user_id,
                display_name=approver.display_name,
                email=approver.email,
            )
            for approver in context.approvers
        ],
        banks=[FieldExpenseBankRead(**bank.model_dump()) for bank in context.banks],
        profile_destination=FieldExpenseProfileDestinationRead(
            **context.profile_destination.model_dump()
        ),
    )


@router.post("/payment-destination/verify", response_model=FieldExpenseDestinationRead)
def verify_field_expense_payment_destination(
    payload: FieldExpenseDestinationVerify,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
) -> FieldExpenseDestinationRead:
    try:
        result = verify_field_expense_destination(
            db=db,
            command=VerifyFieldExpenseDestination(
                requester_system_user_id=UUID(str(auth["principal_id"])),
                source_claim_id=payload.source_claim_id,
                mode=ExpenseDestinationMode(payload.mode),
                bank_code=payload.bank_code,
                account_number=payload.account_number,
                beneficiary_name=payload.beneficiary_name,
            ),
        )
    except FieldExpenseRequestError as exc:
        raise _expense_command_error(exc) from exc
    return FieldExpenseDestinationRead(
        destination_token=result.destination_token,
        mode=result.mode,
        bank_code=result.bank_code,
        bank_name=result.bank_name,
        masked_account_number=result.masked_account_number,
        verified_beneficiary_name=result.verified_beneficiary_name,
        verified_at=result.verified_at,
        expires_at=result.expires_at,
    )


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
    try:
        page = list_requester_expense_requests(
            db,
            RequesterExpenseHistoryQuery(
                system_user_id=UUID(str(auth["principal_id"])),
                work_order_id=resolved_work_order_id,
                status=_history_status(status_filter),
                limit=limit,
                offset=offset,
            ),
        )
    except FieldExpenseRequestError as exc:
        raise _expense_command_error(exc) from exc
    return ListResponse[FieldExpenseRequestRead](
        items=[_expense_read(item) for item in page.items],
        count=page.total,
        limit=page.limit,
        offset=page.offset,
    )


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
    del payload, auth, db
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail=(
            "Standalone expense drafts are retired; submit a route-bound "
            "work-order expense through /expense-requests/submit."
        ),
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
        submission_context = resolve_field_expense_submission_context(
            db=db,
            query=ResolveFieldExpenseSubmissionContext(
                requester_system_user_id=UUID(str(auth["principal_id"])),
                selected_approver_erp_id=payload.selected_approver.erp_employee_id,
                source_claim_id=payload.client_ref,
                destination_token=payload.payment_destination.destination_token,
            ),
        )
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
                work_order=ExpenseWorkOrderIdentity(public_id=payload.work_order_id),
                request_id=payload.client_ref,
                purpose=payload.purpose,
                expense_date=payload.expense_date,
                currency=payload.currency,
                notes=payload.notes,
                items=tuple(
                    ExpenseRequestLineInput(**item.model_dump())
                    for item in payload.items
                ),
                selected_approver=submission_context.selected_approver,
                payment_destination=submission_context.payment_destination,
            ),
        )
    except FieldExpenseRequestError as exc:
        raise _expense_command_error(exc) from exc


@router.get("/{expense_request_id}", response_model=FieldExpenseRequestRead)
def get_field_expense_request(
    expense_request_id: UUID,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    try:
        return _expense_read(
            get_requester_expense_request(
                db,
                RequesterExpenseDetailQuery(
                    system_user_id=UUID(str(auth["principal_id"])),
                    request_id=expense_request_id,
                ),
            )
        )
    except FieldExpenseRequestError as exc:
        raise _expense_command_error(exc) from exc


@router.post("/{expense_request_id}/submit", response_model=FieldExpenseRequestRead)
def submit_field_expense_request(
    expense_request_id: UUID,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    del expense_request_id, auth, db
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail=(
            "Legacy draft submission is retired; create and submit through "
            "/expense-requests/submit."
        ),
    )


@router.post("/{expense_request_id}/cancel", response_model=FieldExpenseRequestRead)
def cancel_field_expense_request(
    expense_request_id: UUID,
    auth: dict = Depends(require_user_auth),
    request_id: UUID | None = Header(default=None, alias="X-Request-ID"),
    db: Session = Depends(get_db),
):
    command_id = request_id or uuid4()
    try:
        db_session_adapter.release_read_transaction(db)
        outcome = cancel_field_expense_request_command(
            db,
            command=CancelFieldExpenseRequest(
                context=_command_context(
                    auth,
                    request_id=command_id,
                    reason=f"cancel_expense_request:{expense_request_id}",
                ),
                expense_request_id=expense_request_id,
                requester_person_id=UUID(str(auth["principal_id"])),
            ),
        )
        return field_expense_requests.get(db, auth, outcome.id)
    except FieldExpenseRequestError as exc:
        raise _expense_command_error(exc) from exc
