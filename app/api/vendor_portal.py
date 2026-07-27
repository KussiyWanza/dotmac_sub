"""Native vendor portal API backed by Sub domain tables."""

from collections.abc import Callable
from typing import TypeVar
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.schemas.common import ListResponse
from app.schemas.vendor_portal import (
    VendorAsBuiltCreate,
    VendorQuoteCreate,
    VendorQuoteLineCreate,
    VendorQuoteLineUpdate,
    VendorRouteRevisionCreate,
    VendorSubmissionConfirm,
)
from app.schemas.vendor_purchase_invoice import (
    VendorPurchaseInvoiceCreate,
    VendorPurchaseInvoiceLineCreate,
    VendorPurchaseInvoiceLineUpdate,
    VendorPurchaseInvoiceRead,
    VendorPurchaseInvoiceUpdate,
)
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.field.vendor_auth import require_native_vendor_context
from app.services.owner_commands import CommandContext
from app.services.vendor_portal_operations import (
    AddVendorQuoteLineCommand,
    CreateVendorQuoteCommand,
    CreateVendorRouteRevisionCommand,
    DeleteVendorQuoteLineCommand,
    SubmitVendorRouteRevisionCommand,
    UpdateVendorQuoteLineCommand,
    vendor_portal_operations,
)
from app.services.vendor_purchase_invoices import (
    AddVendorPurchaseInvoiceLineCommand,
    CreateVendorPurchaseInvoiceCommand,
    DeleteVendorPurchaseInvoiceLineCommand,
    UpdateVendorPurchaseInvoiceCommand,
    UpdateVendorPurchaseInvoiceLineCommand,
    UploadVendorPurchaseInvoiceAttachmentCommand,
    vendor_purchase_invoices,
)
from app.services.vendor_submission_proposals import (
    ConfirmVendorSubmissionCommand,
    confirm_submission,
    issue_as_built_submission,
    issue_purchase_invoice_submission,
    issue_quote_submission,
)

router = APIRouter(
    prefix="/vendor",
    tags=["vendor-portal"],
    # Membership authz, not RBAC: every route requires an active FieldVendorUser
    # of an active FieldVendor linked to the native vendor domain (409 when
    # unlinked). See tests/test_vendor_portal_auth.py for the behavior pins.
    dependencies=[Depends(require_native_vendor_context)],
)
ResultT = TypeVar("ResultT")


def _vendor_id(context: dict) -> str:
    return str(context["native_vendor_id"])


def _command_context(context: dict, *, scope: str, reason: str) -> CommandContext:
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor=str(context["principal_id"]),
        scope=scope,
        reason=reason,
    )


def _vendor_http_error(exc: DomainError) -> HTTPException:
    suffix = exc.code.rsplit(".", 1)[-1]
    if suffix.endswith("not_found"):
        status_code = 404
    elif suffix in {
        "project_not_assigned",
        "quote_creation_not_allowed",
        "proposal_context_mismatch",
    }:
        status_code = 403
    elif suffix in {
        "quote_line_required",
        "as_built_evidence_required",
        "invalid_as_built_route",
        "invalid_payload",
        "invalid_proposal",
        "empty_attachment",
        "invalid_attachment",
        "invoice_number_required",
        "invoice_line_required",
    }:
        status_code = 422
    elif suffix in {
        "bidding_closed",
        "quote_not_editable",
        "quote_not_submittable",
        "quote_not_reviewable",
        "route_revision_not_draft",
        "expired_proposal",
        "confirmation_in_progress",
        "stale_proposal",
        "missing_result_evidence",
        "active_caller_transaction",
        "invoice_not_editable",
        "invoice_number_conflict",
        "submitted_quote_required",
    }:
        status_code = 409
    else:
        status_code = 500
    return HTTPException(
        status_code=status_code,
        detail={"code": exc.code, "message": exc.message, "details": exc.details},
    )


def _vendor_call(operation: Callable[[], ResultT]) -> ResultT:
    try:
        return operation()
    except DomainError as exc:
        raise _vendor_http_error(exc) from exc


@router.get("/projects/available")
def list_available_projects(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    context: dict = Depends(require_native_vendor_context),
    db: Session = Depends(get_db),
):
    items = vendor_portal_operations.list_projects(
        db, _vendor_id(context), available=True, limit=limit, offset=offset
    )
    return {"items": items, "count": len(items), "limit": limit, "offset": offset}


@router.get("/projects/mine")
def list_my_projects(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    context: dict = Depends(require_native_vendor_context),
    db: Session = Depends(get_db),
):
    items = vendor_portal_operations.list_projects(
        db, _vendor_id(context), available=False, limit=limit, offset=offset
    )
    return {"items": items, "count": len(items), "limit": limit, "offset": offset}


@router.post("/quotes", status_code=status.HTTP_201_CREATED)
def create_quote(
    payload: VendorQuoteCreate,
    context: dict = Depends(require_native_vendor_context),
    db: Session = Depends(get_db),
):
    vendor_id = _vendor_id(context)
    command_context = _command_context(
        context,
        scope=vendor_id,
        reason="vendor_quote_creation",
    )
    db_session_adapter.release_read_transaction(db)
    return _vendor_call(
        lambda: vendor_portal_operations.create_quote(
            db,
            CreateVendorQuoteCommand(
                context=command_context,
                payload=payload,
                vendor_id=vendor_id,
                user_id=str(context["principal_id"]),
            ),
        )
    )


@router.get("/quotes/{quote_id}")
def get_quote(
    quote_id: str,
    context: dict = Depends(require_native_vendor_context),
    db: Session = Depends(get_db),
):
    return _vendor_call(
        lambda: vendor_portal_operations.get_quote(db, quote_id, _vendor_id(context))
    )


@router.post("/quotes/{quote_id}/line-items", status_code=status.HTTP_201_CREATED)
def add_quote_line(
    quote_id: str,
    payload: VendorQuoteLineCreate,
    context: dict = Depends(require_native_vendor_context),
    db: Session = Depends(get_db),
):
    vendor_id = _vendor_id(context)
    command_context = _command_context(
        context,
        scope=vendor_id,
        reason="vendor_quote_line_creation",
    )
    db_session_adapter.release_read_transaction(db)
    return _vendor_call(
        lambda: vendor_portal_operations.add_quote_line(
            db,
            AddVendorQuoteLineCommand(
                context=command_context,
                quote_id=quote_id,
                payload=payload,
                vendor_id=vendor_id,
            ),
        )
    )


@router.patch("/quotes/{quote_id}/line-items/{line_id}")
def update_quote_line(
    quote_id: str,
    line_id: str,
    payload: VendorQuoteLineUpdate,
    context: dict = Depends(require_native_vendor_context),
    db: Session = Depends(get_db),
):
    vendor_id = _vendor_id(context)
    command_context = _command_context(
        context,
        scope=vendor_id,
        reason="vendor_quote_line_update",
    )
    db_session_adapter.release_read_transaction(db)
    return _vendor_call(
        lambda: vendor_portal_operations.update_quote_line(
            db,
            UpdateVendorQuoteLineCommand(
                context=command_context,
                quote_id=quote_id,
                line_id=line_id,
                payload=payload,
                vendor_id=vendor_id,
            ),
        )
    )


@router.delete("/quotes/{quote_id}/line-items/{line_id}")
def delete_quote_line(
    quote_id: str,
    line_id: str,
    context: dict = Depends(require_native_vendor_context),
    db: Session = Depends(get_db),
):
    vendor_id = _vendor_id(context)
    command_context = _command_context(
        context,
        scope=vendor_id,
        reason="vendor_quote_line_deletion",
    )
    db_session_adapter.release_read_transaction(db)
    return _vendor_call(
        lambda: vendor_portal_operations.delete_quote_line(
            db,
            DeleteVendorQuoteLineCommand(
                context=command_context,
                quote_id=quote_id,
                line_id=line_id,
                vendor_id=vendor_id,
            ),
        )
    )


@router.post("/quotes/{quote_id}/submit")
def submit_quote(
    quote_id: str,
    context: dict = Depends(require_native_vendor_context),
    db: Session = Depends(get_db),
):
    return _vendor_call(
        lambda: issue_quote_submission(
            db,
            quote_id=quote_id,
            vendor_id=_vendor_id(context),
            user_id=str(context["principal_id"]),
        )
    )


@router.post("/quotes/{quote_id}/route-revisions", status_code=status.HTTP_201_CREATED)
def create_route_revision(
    quote_id: str,
    payload: VendorRouteRevisionCreate,
    context: dict = Depends(require_native_vendor_context),
    db: Session = Depends(get_db),
):
    vendor_id = _vendor_id(context)
    command_context = _command_context(
        context,
        scope=vendor_id,
        reason="vendor_route_revision_creation",
    )
    db_session_adapter.release_read_transaction(db)
    return _vendor_call(
        lambda: vendor_portal_operations.create_route_revision(
            db,
            CreateVendorRouteRevisionCommand(
                context=command_context,
                quote_id=quote_id,
                payload=payload,
                vendor_id=vendor_id,
            ),
        )
    )


@router.post("/route-revisions/{revision_id}/submit")
def submit_route_revision(
    revision_id: str,
    context: dict = Depends(require_native_vendor_context),
    db: Session = Depends(get_db),
):
    vendor_id = _vendor_id(context)
    command_context = _command_context(
        context,
        scope=vendor_id,
        reason="vendor_route_revision_submission",
    )
    db_session_adapter.release_read_transaction(db)
    return _vendor_call(
        lambda: vendor_portal_operations.submit_route_revision(
            db,
            SubmitVendorRouteRevisionCommand(
                context=command_context,
                revision_id=revision_id,
                vendor_id=vendor_id,
                user_id=str(context["principal_id"]),
            ),
        )
    )


@router.post("/as-built", status_code=status.HTTP_201_CREATED)
def submit_as_built(
    payload: VendorAsBuiltCreate,
    context: dict = Depends(require_native_vendor_context),
    db: Session = Depends(get_db),
):
    return _vendor_call(
        lambda: issue_as_built_submission(
            db,
            payload=payload,
            vendor_id=_vendor_id(context),
            user_id=str(context["principal_id"]),
        )
    )


@router.post("/projects/{project_id}/submissions/confirm")
def confirm_vendor_submission(
    project_id: str,
    payload: VendorSubmissionConfirm,
    context: dict = Depends(require_native_vendor_context),
    db: Session = Depends(get_db),
):
    vendor_id = _vendor_id(context)
    command_context = _command_context(
        context,
        scope=vendor_id,
        reason="vendor_submission_confirmation",
    )
    db_session_adapter.release_read_transaction(db)
    return _vendor_call(
        lambda: confirm_submission(
            db,
            ConfirmVendorSubmissionCommand(
                context=command_context,
                confirmation_token=payload.confirmation_token,
                vendor_id=vendor_id,
                user_id=str(context["principal_id"]),
                project_id=project_id,
            ),
        )
    )


@router.get(
    "/purchase-invoices", response_model=ListResponse[VendorPurchaseInvoiceRead]
)
def list_purchase_invoices(
    project_id: str | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    context: dict = Depends(require_native_vendor_context),
    db: Session = Depends(get_db),
):
    items = vendor_purchase_invoices.list(
        db,
        vendor_id=_vendor_id(context),
        project_id=project_id,
        status=status_filter,
        limit=limit,
        offset=offset,
    )
    return {"items": items, "count": len(items), "limit": limit, "offset": offset}


@router.post(
    "/purchase-invoices",
    response_model=VendorPurchaseInvoiceRead,
    status_code=status.HTTP_201_CREATED,
)
def create_purchase_invoice(
    payload: VendorPurchaseInvoiceCreate,
    context: dict = Depends(require_native_vendor_context),
    db: Session = Depends(get_db),
):
    vendor_id = _vendor_id(context)
    command_context = _command_context(
        context, scope=vendor_id, reason="vendor_purchase_invoice_creation"
    )
    db_session_adapter.release_read_transaction(db)
    return _vendor_call(
        lambda: vendor_purchase_invoices.create(
            db,
            CreateVendorPurchaseInvoiceCommand(
                context=command_context,
                payload=payload,
                vendor_id=vendor_id,
                created_by_system_user_id=str(context["principal_id"]),
            ),
        )
    )


@router.get("/purchase-invoices/{invoice_id}", response_model=VendorPurchaseInvoiceRead)
def get_purchase_invoice(
    invoice_id: str,
    context: dict = Depends(require_native_vendor_context),
    db: Session = Depends(get_db),
):
    return vendor_purchase_invoices.get(db, invoice_id, vendor_id=_vendor_id(context))


@router.patch(
    "/purchase-invoices/{invoice_id}", response_model=VendorPurchaseInvoiceRead
)
def update_purchase_invoice(
    invoice_id: str,
    payload: VendorPurchaseInvoiceUpdate,
    context: dict = Depends(require_native_vendor_context),
    db: Session = Depends(get_db),
):
    vendor_id = _vendor_id(context)
    command_context = _command_context(
        context, scope=vendor_id, reason="vendor_purchase_invoice_update"
    )
    db_session_adapter.release_read_transaction(db)
    return _vendor_call(
        lambda: vendor_purchase_invoices.update(
            db,
            UpdateVendorPurchaseInvoiceCommand(
                context=command_context,
                invoice_id=invoice_id,
                payload=payload,
                vendor_id=vendor_id,
            ),
        )
    )


@router.post(
    "/purchase-invoices/{invoice_id}/line-items",
    response_model=VendorPurchaseInvoiceRead,
    status_code=status.HTTP_201_CREATED,
)
def add_purchase_invoice_line(
    invoice_id: str,
    payload: VendorPurchaseInvoiceLineCreate,
    context: dict = Depends(require_native_vendor_context),
    db: Session = Depends(get_db),
):
    vendor_id = _vendor_id(context)
    command_context = _command_context(
        context, scope=vendor_id, reason="vendor_purchase_invoice_line_addition"
    )
    db_session_adapter.release_read_transaction(db)
    return _vendor_call(
        lambda: vendor_purchase_invoices.add_line(
            db,
            AddVendorPurchaseInvoiceLineCommand(
                context=command_context,
                invoice_id=invoice_id,
                payload=payload,
                vendor_id=vendor_id,
            ),
        )
    )


@router.patch(
    "/purchase-invoices/{invoice_id}/line-items/{line_id}",
    response_model=VendorPurchaseInvoiceRead,
)
def update_purchase_invoice_line(
    invoice_id: str,
    line_id: str,
    payload: VendorPurchaseInvoiceLineUpdate,
    context: dict = Depends(require_native_vendor_context),
    db: Session = Depends(get_db),
):
    vendor_id = _vendor_id(context)
    command_context = _command_context(
        context, scope=vendor_id, reason="vendor_purchase_invoice_line_update"
    )
    db_session_adapter.release_read_transaction(db)
    return _vendor_call(
        lambda: vendor_purchase_invoices.update_line(
            db,
            UpdateVendorPurchaseInvoiceLineCommand(
                context=command_context,
                invoice_id=invoice_id,
                line_id=line_id,
                payload=payload,
                vendor_id=vendor_id,
            ),
        )
    )


@router.delete(
    "/purchase-invoices/{invoice_id}/line-items/{line_id}",
    response_model=VendorPurchaseInvoiceRead,
)
def delete_purchase_invoice_line(
    invoice_id: str,
    line_id: str,
    context: dict = Depends(require_native_vendor_context),
    db: Session = Depends(get_db),
):
    vendor_id = _vendor_id(context)
    command_context = _command_context(
        context, scope=vendor_id, reason="vendor_purchase_invoice_line_deletion"
    )
    db_session_adapter.release_read_transaction(db)
    return _vendor_call(
        lambda: vendor_purchase_invoices.delete_line(
            db,
            DeleteVendorPurchaseInvoiceLineCommand(
                context=command_context,
                invoice_id=invoice_id,
                line_id=line_id,
                vendor_id=vendor_id,
            ),
        )
    )


@router.post(
    "/purchase-invoices/{invoice_id}/attachment",
    response_model=VendorPurchaseInvoiceRead,
)
async def upload_purchase_invoice_attachment(
    invoice_id: str,
    attachment: UploadFile = File(...),
    context: dict = Depends(require_native_vendor_context),
    db: Session = Depends(get_db),
):
    content = await attachment.read()
    vendor_id = _vendor_id(context)
    command_context = _command_context(
        context, scope=vendor_id, reason="vendor_purchase_invoice_attachment_upload"
    )
    db_session_adapter.release_read_transaction(db)
    return _vendor_call(
        lambda: vendor_purchase_invoices.upload_attachment(
            db,
            UploadVendorPurchaseInvoiceAttachmentCommand(
                context=command_context,
                invoice_id=invoice_id,
                vendor_id=vendor_id,
                file_name=attachment.filename or "invoice.pdf",
                content_type=attachment.content_type,
                content=content,
            ),
        )
    )


@router.get("/purchase-invoices/{invoice_id}/attachment")
def download_purchase_invoice_attachment(
    invoice_id: str,
    context: dict = Depends(require_native_vendor_context),
    db: Session = Depends(get_db),
):
    file, stream = vendor_purchase_invoices.attachment_file(
        db, invoice_id, vendor_id=_vendor_id(context)
    )
    return StreamingResponse(
        stream.chunks,
        media_type=stream.content_type or "application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{file.original_filename}"'
        },
    )


@router.post(
    "/purchase-invoices/{invoice_id}/submit",
)
def submit_purchase_invoice(
    invoice_id: str,
    context: dict = Depends(require_native_vendor_context),
    db: Session = Depends(get_db),
):
    return _vendor_call(
        lambda: issue_purchase_invoice_submission(
            db,
            invoice_id=invoice_id,
            vendor_id=_vendor_id(context),
            user_id=str(context["principal_id"]),
        )
    )
