from dataclasses import asdict
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.schemas.common import ListResponse
from app.schemas.field import (
    FieldEquipmentCustodyRead,
    FieldEquipmentIssueRequest,
    FieldEquipmentReturnRequest,
    FieldExpenseRequestRead,
    FieldManagerExpenseRejectRequest,
    FieldManagerJob,
    FieldManagerJobAssignRequest,
    FieldManagerMaterialRejectRequest,
    FieldManagerMeResponse,
    FieldManagerSummary,
    FieldManagerTechniciansResponse,
    FieldMaterialRequestRead,
    TechnicianSatisfactionResponse,
)
from app.schemas.vendor_portal import VendorReview
from app.schemas.vendor_purchase_invoice import (
    VendorPurchaseInvoiceRead,
    VendorPurchaseInvoiceReview,
)
from app.services import technician_satisfaction
from app.services.auth_dependencies import require_any_permission, require_permission
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.field.equipment_custody import field_equipment_custody
from app.services.field.expense_requests import field_expense_requests
from app.services.field.manager import field_manager
from app.services.field.material_requests import field_material_requests
from app.services.owner_commands import CommandContext
from app.services.vendor_portal_operations import (
    ReviewVendorQuoteCommand,
    vendor_portal_operations,
)
from app.services.vendor_purchase_invoices import (
    ReviewVendorPurchaseInvoiceCommand,
    vendor_purchase_invoices,
)

router = APIRouter(prefix="/manager", tags=["field-manager"])

# Manager-mode predicate (ported from CRM): any staff principal holding an
# operations read permission unlocks manager mode; writes stay behind the
# matching write/dispatch permissions.
_manager_access = require_any_permission(
    "operations:work_order:read",
    "operations:technician:read",
    "operations:expense_request:read",
)
_ops_read = require_any_permission(
    "operations:work_order:read",
    "operations:technician:read",
)
_dispatch_write = require_any_permission(
    "operations:work_order:update",
    "operations:work_order:dispatch",
)
_expense_read = require_permission("operations:expense_request:read")
_expense_write = require_permission("operations:expense_request:write")
_material_read = require_any_permission(
    "operations:material_request:read",
    "inventory:read",
)
_material_write = require_any_permission(
    "operations:material_request:write",
    "inventory:write",
)
_asset_custody_read = require_any_permission(
    "operations:asset_custody:read",
    "inventory:read",
)
_asset_custody_write = require_any_permission(
    "operations:asset_custody:write",
    "inventory:write",
)
_purchase_invoice_read = require_any_permission("inventory:read", "finance:ap:read")
_purchase_invoice_write = require_any_permission("inventory:write", "finance:ap:write")


def _vendor_review_context(auth: dict, *, quote_id: str) -> CommandContext:
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor=str(auth["principal_id"]),
        scope=quote_id,
        reason="field_manager_vendor_quote_review",
    )


def _vendor_quote_error(exc: DomainError) -> HTTPException:
    status_code = 404 if exc.code.endswith("not_found") else 409
    return HTTPException(status_code=status_code, detail=exc.message)


def _invoice_review_context(auth: dict, *, invoice_id: str) -> CommandContext:
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor=str(auth["principal_id"]),
        scope=invoice_id,
        reason="field_manager_vendor_purchase_invoice_review",
    )


@router.get("/me", response_model=FieldManagerMeResponse)
def field_manager_me(
    auth: dict = Depends(_manager_access),
    db: Session = Depends(get_db),
):
    return field_manager.me(db, auth)


@router.get("/summary", response_model=FieldManagerSummary)
def field_manager_summary(
    stale_after_seconds: int = Query(default=120, ge=30, le=3600),
    auth: dict = Depends(_ops_read),
    db: Session = Depends(get_db),
):
    return field_manager.summary(db, stale_after_seconds=stale_after_seconds)


@router.get("/technicians", response_model=FieldManagerTechniciansResponse)
def field_manager_technicians(
    stale_after_seconds: int = Query(default=120, ge=30, le=3600),
    limit: int = Query(default=500, ge=1, le=500),
    auth: dict = Depends(_ops_read),
    db: Session = Depends(get_db),
):
    items = field_manager.list_technicians(
        db, stale_after_seconds=stale_after_seconds, limit=limit
    )
    return {
        "items": items,
        "count": len(items),
        "live_count": sum(1 for item in items if item["is_live"]),
        "sharing_count": sum(1 for item in items if item["location_sharing_enabled"]),
        "limit": limit,
        "offset": 0,
    }


@router.get("/technicians/satisfaction", response_model=TechnicianSatisfactionResponse)
def field_manager_technician_satisfaction(
    window_days: int = Query(default=90, ge=1, le=365),
    auth: dict = Depends(_ops_read),
    db: Session = Depends(get_db),
):
    """Customer satisfaction per technician, best-rated first.

    The ratings have always been collected on completed visits; this is the
    first surface that reads them back.
    """
    cards = technician_satisfaction.scorecards(db, window_days=window_days)
    return {
        "items": [asdict(card) for card in cards],
        "count": len(cards),
        "window_days": window_days,
    }


@router.get("/jobs", response_model=ListResponse[FieldManagerJob])
def field_manager_jobs(
    status: str | None = None,
    assigned_to_person_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    auth: dict = Depends(_ops_read),
    db: Session = Depends(get_db),
):
    items = field_manager.list_jobs(
        db,
        status=status,
        assigned_to_person_id=assigned_to_person_id,
        limit=limit,
        offset=offset,
    )
    return {"items": items, "count": len(items), "limit": limit, "offset": offset}


@router.post("/jobs/{crm_work_order_id}/assign", response_model=FieldManagerJob)
def field_manager_assign_job(
    crm_work_order_id: str,
    payload: FieldManagerJobAssignRequest,
    auth: dict = Depends(_dispatch_write),
    request_id: str | None = Header(default=None, alias="X-Request-ID"),
    db: Session = Depends(get_db),
):
    return field_manager.assign_job(
        db,
        crm_work_order_id,
        person_id=payload.person_id,
        scheduled_start=payload.scheduled_start,
        scheduled_end=payload.scheduled_end,
        status=payload.status,
        auth=auth,
        request_id=request_id,
    )


@router.get("/expenses", response_model=ListResponse[FieldExpenseRequestRead])
def field_manager_expenses(
    status_filter: str | None = Query(default="submitted", alias="status"),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    auth: dict = Depends(_expense_read),
    db: Session = Depends(get_db),
):
    items = field_expense_requests.list_all(
        db, status=status_filter, limit=limit, offset=offset
    )
    return {"items": items, "count": len(items), "limit": limit, "offset": offset}


@router.post(
    "/expenses/{expense_request_id}/approve",
    response_model=FieldExpenseRequestRead,
)
def field_manager_approve_expense(
    expense_request_id: str,
    auth: dict = Depends(_expense_write),
    db: Session = Depends(get_db),
):
    return field_expense_requests.approve(db, expense_request_id)


@router.post(
    "/expenses/{expense_request_id}/reject",
    response_model=FieldExpenseRequestRead,
)
def field_manager_reject_expense(
    expense_request_id: str,
    payload: FieldManagerExpenseRejectRequest,
    auth: dict = Depends(_expense_write),
    db: Session = Depends(get_db),
):
    return field_expense_requests.reject(db, expense_request_id, payload.reason)


@router.get("/materials", response_model=ListResponse[FieldMaterialRequestRead])
def field_manager_material_requests(
    status_filter: str | None = Query(default="submitted", alias="status"),
    crm_work_order_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    auth: dict = Depends(_material_read),
    db: Session = Depends(get_db),
):
    items = field_material_requests.list_all(
        db,
        status=status_filter,
        crm_work_order_id=crm_work_order_id,
        limit=limit,
        offset=offset,
    )
    return {"items": items, "count": len(items), "limit": limit, "offset": offset}


@router.post(
    "/materials/{material_request_id}/approve",
    response_model=FieldMaterialRequestRead,
)
def field_manager_approve_material_request(
    material_request_id: str,
    auth: dict = Depends(_material_write),
    db: Session = Depends(get_db),
):
    return field_material_requests.approve(db, material_request_id)


@router.post(
    "/materials/{material_request_id}/reject",
    response_model=FieldMaterialRequestRead,
)
def field_manager_reject_material_request(
    material_request_id: str,
    payload: FieldManagerMaterialRejectRequest,
    auth: dict = Depends(_material_write),
    db: Session = Depends(get_db),
):
    return field_material_requests.reject(db, material_request_id, payload.reason)


@router.post(
    "/materials/{material_request_id}/issue",
    response_model=FieldMaterialRequestRead,
)
def field_manager_issue_material_request(
    material_request_id: str,
    auth: dict = Depends(_material_write),
    db: Session = Depends(get_db),
):
    return field_material_requests.issue(db, material_request_id)


@router.post(
    "/materials/{material_request_id}/fulfill",
    response_model=FieldMaterialRequestRead,
)
def field_manager_fulfill_material_request(
    material_request_id: str,
    auth: dict = Depends(_material_write),
    db: Session = Depends(get_db),
):
    return field_material_requests.fulfill(db, material_request_id)


@router.get(
    "/equipment-custody",
    response_model=ListResponse[FieldEquipmentCustodyRead],
)
def field_manager_equipment_custody(
    technician_id: str | None = None,
    asset_source: str | None = None,
    status_filter: str = Query(default="issued", alias="status"),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    auth: dict = Depends(_asset_custody_read),
    db: Session = Depends(get_db),
):
    items = field_equipment_custody.list_all(
        db,
        technician_id=technician_id,
        asset_source=asset_source,
        status=status_filter,
        limit=limit,
        offset=offset,
    )
    return {"items": items, "count": len(items), "limit": limit, "offset": offset}


@router.post(
    "/equipment-custody",
    response_model=FieldEquipmentCustodyRead,
    status_code=201,
)
def field_manager_issue_equipment(
    payload: FieldEquipmentIssueRequest,
    auth: dict = Depends(_asset_custody_write),
    db: Session = Depends(get_db),
):
    return field_equipment_custody.issue(
        db,
        asset_source=payload.asset_source,
        asset_id=str(payload.asset_id),
        technician_id=str(payload.technician_id),
        condition_on_issue=payload.condition_on_issue,
        notes=payload.notes,
    )


@router.post(
    "/equipment-custody/{custody_id}/return",
    response_model=FieldEquipmentCustodyRead,
)
def field_manager_return_equipment(
    custody_id: str,
    payload: FieldEquipmentReturnRequest,
    auth: dict = Depends(_asset_custody_write),
    db: Session = Depends(get_db),
):
    return field_equipment_custody.return_asset(
        db,
        custody_id,
        status=payload.status,
        condition_on_return=payload.condition_on_return,
        notes=payload.notes,
    )


@router.get(
    "/vendor-purchase-invoices",
    response_model=ListResponse[VendorPurchaseInvoiceRead],
)
def field_manager_vendor_purchase_invoices(
    status_filter: str | None = Query(default="submitted", alias="status"),
    vendor_id: str | None = None,
    project_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _auth: dict = Depends(_purchase_invoice_read),
    db: Session = Depends(get_db),
):
    items = vendor_purchase_invoices.list(
        db,
        vendor_id=vendor_id,
        project_id=project_id,
        status=status_filter,
        limit=limit,
        offset=offset,
    )
    return {"items": items, "count": len(items), "limit": limit, "offset": offset}


@router.get(
    "/vendor-purchase-invoices/{invoice_id}",
    response_model=VendorPurchaseInvoiceRead,
)
def field_manager_vendor_purchase_invoice(
    invoice_id: str,
    _auth: dict = Depends(_purchase_invoice_read),
    db: Session = Depends(get_db),
):
    return vendor_purchase_invoices.get(db, invoice_id)


@router.post(
    "/vendor-purchase-invoices/{invoice_id}/approve",
    response_model=VendorPurchaseInvoiceRead,
)
def field_manager_approve_vendor_purchase_invoice(
    invoice_id: str,
    payload: VendorPurchaseInvoiceReview,
    auth: dict = Depends(_purchase_invoice_write),
    db: Session = Depends(get_db),
):
    context = _invoice_review_context(auth, invoice_id=invoice_id)
    db_session_adapter.release_read_transaction(db)
    try:
        return vendor_purchase_invoices.review(
            db,
            ReviewVendorPurchaseInvoiceCommand(
                context=context,
                invoice_id=invoice_id,
                reviewer_system_user_id=str(auth["principal_id"]),
                approve=True,
                review_notes=payload.review_notes,
            ),
        )
    except DomainError as exc:
        raise _vendor_quote_error(exc) from exc


@router.post(
    "/vendor-purchase-invoices/{invoice_id}/reject",
    response_model=VendorPurchaseInvoiceRead,
)
def field_manager_reject_vendor_purchase_invoice(
    invoice_id: str,
    payload: VendorPurchaseInvoiceReview,
    auth: dict = Depends(_purchase_invoice_write),
    db: Session = Depends(get_db),
):
    context = _invoice_review_context(auth, invoice_id=invoice_id)
    db_session_adapter.release_read_transaction(db)
    try:
        return vendor_purchase_invoices.review(
            db,
            ReviewVendorPurchaseInvoiceCommand(
                context=context,
                invoice_id=invoice_id,
                reviewer_system_user_id=str(auth["principal_id"]),
                approve=False,
                review_notes=payload.review_notes,
            ),
        )
    except DomainError as exc:
        raise _vendor_quote_error(exc) from exc


@router.post("/vendor-quotes/{quote_id}/approve")
def field_manager_approve_vendor_quote(
    quote_id: str,
    payload: VendorReview,
    auth: dict = Depends(_purchase_invoice_write),
    db: Session = Depends(get_db),
):
    context = _vendor_review_context(auth, quote_id=quote_id)
    db_session_adapter.release_read_transaction(db)
    try:
        return vendor_portal_operations.review_quote(
            db,
            ReviewVendorQuoteCommand(
                context=context,
                quote_id=quote_id,
                reviewer_id=str(auth["principal_id"]),
                approve=True,
                notes=payload.review_notes,
            ),
        )
    except DomainError as exc:
        raise _vendor_quote_error(exc) from exc


@router.post("/vendor-quotes/{quote_id}/request-revision")
def field_manager_request_vendor_quote_revision(
    quote_id: str,
    payload: VendorReview,
    auth: dict = Depends(_purchase_invoice_write),
    db: Session = Depends(get_db),
):
    context = _vendor_review_context(auth, quote_id=quote_id)
    db_session_adapter.release_read_transaction(db)
    try:
        return vendor_portal_operations.review_quote(
            db,
            ReviewVendorQuoteCommand(
                context=context,
                quote_id=quote_id,
                reviewer_id=str(auth["principal_id"]),
                approve=False,
                notes=payload.review_notes,
            ),
        )
    except DomainError as exc:
        raise _vendor_quote_error(exc) from exc
