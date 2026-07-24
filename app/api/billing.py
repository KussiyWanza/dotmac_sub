import logging
from datetime import datetime
from decimal import Decimal
from typing import Never
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.api.webhook_observation import webhook_observation
from app.db import finish_read_response, get_db
from app.models.audit import AuditActorType
from app.models.billing import (
    PaymentProviderEventStatus,
    PaymentSettlementOrigin,
    PaymentStatus,
)
from app.schemas.billing import (
    AccountAdjustmentConfirm,
    AccountAdjustmentPreviewRead,
    AccountAdjustmentPreviewRequest,
    AccountAdjustmentRead,
    AccountAdjustmentReversalConfirm,
    AccountAdjustmentReversalPreviewRead,
    AccountAdjustmentReversalPreviewRequest,
    BankAccountCreate,
    BankAccountRead,
    BankAccountUpdate,
    BillingAccountCreate,
    BillingAccountCreditConsumptionEvidenceInspectionRead,
    BillingAccountCreditConsumptionReconciliationConfirm,
    BillingAccountCreditConsumptionReconciliationPreviewRead,
    BillingAccountCreditConsumptionReconciliationRequest,
    BillingAccountCreditConsumptionReconciliationResultRead,
    BillingAccountMissingPaymentReturnEvidenceInspectionRead,
    BillingAccountPaymentConfirm,
    BillingAccountPaymentPreviewRead,
    BillingAccountPaymentPreviewRequest,
    BillingAccountPaymentRefundPreviewRead,
    BillingAccountPaymentRefundRequest,
    BillingAccountPaymentReturnDocumentReconstructionConfirm,
    BillingAccountPaymentReturnDocumentReconstructionPreviewRead,
    BillingAccountPaymentReturnDocumentReconstructionRequest,
    BillingAccountPaymentReturnDocumentReconstructionResultRead,
    BillingAccountPaymentReturnEvidenceInspectionRead,
    BillingAccountPaymentReturnReconciliationConfirm,
    BillingAccountPaymentReturnReconciliationPreviewRead,
    BillingAccountPaymentReturnReconciliationRequest,
    BillingAccountPaymentReturnReconciliationResultRead,
    BillingAccountPaymentReversalPreviewRead,
    BillingAccountPaymentReversalRequest,
    BillingAccountPaymentSettlementEvidenceInspectionRead,
    BillingAccountPaymentSettlementReconciliationConfirm,
    BillingAccountPaymentSettlementReconciliationPreviewRead,
    BillingAccountPaymentSettlementReconciliationRequest,
    BillingAccountPaymentSettlementReconciliationResultRead,
    BillingAccountRead,
    BillingAccountStatement,
    BillingAccountUpdate,
    BillingRunRead,
    CollectionAccountCreate,
    CollectionAccountRead,
    CollectionAccountUpdate,
    CreditNoteApplicationPreviewRead,
    CreditNoteApplicationPreviewRequest,
    CreditNoteApplicationRead,
    CreditNoteApplyRequest,
    CreditNoteCreate,
    CreditNoteIssueConfirmation,
    CreditNoteIssuePreviewRead,
    CreditNoteIssuePreviewRequest,
    CreditNoteIssueRequest,
    CreditNoteLineCreate,
    CreditNoteLineRead,
    CreditNoteLineUpdate,
    CreditNoteRead,
    CreditNoteSyncRead,
    CreditNoteUpdate,
    CreditNoteVoidPreviewRead,
    CreditNoteVoidRequest,
    InvoiceBulkActionResponse,
    InvoiceBulkVoidRequest,
    InvoiceBulkWriteOffRequest,
    InvoiceClosureConfirm,
    InvoiceClosureConfirmationRead,
    InvoiceClosureEvidenceInspectionRead,
    InvoiceClosurePreviewRead,
    InvoiceClosureReconciliationRequest,
    InvoiceCreate,
    InvoiceLineCreate,
    InvoiceLineRead,
    InvoiceLineUpdate,
    InvoiceRead,
    InvoiceRunRequest,
    InvoiceRunResponse,
    InvoiceSyncRead,
    InvoiceUpdate,
    InvoiceWriteOffRequest,
    LedgerEntryCreate,
    LedgerEntryRead,
    PaymentAllocationConfirm,
    PaymentAllocationCreate,
    PaymentAllocationPreviewRead,
    PaymentAllocationPreviewRequest,
    PaymentAllocationRead,
    PaymentChannelAccountCreate,
    PaymentChannelAccountRead,
    PaymentChannelAccountUpdate,
    PaymentChannelCreate,
    PaymentChannelRead,
    PaymentChannelUpdate,
    PaymentCreate,
    PaymentCreationConfirm,
    PaymentCreationPreviewRead,
    PaymentCreationPreviewRequest,
    PaymentInitiateRequest,
    PaymentInitiateResponse,
    PaymentMethodCreate,
    PaymentMethodRead,
    PaymentMethodUpdate,
    PaymentProviderEventIngest,
    PaymentProviderEventRead,
    PaymentProviderRead,
    PaymentRead,
    PaymentRefundPreviewRead,
    PaymentRefundPreviewRequest,
    PaymentRefundRead,
    PaymentRefundRequest,
    PaymentReversalPreviewRead,
    PaymentReversalPreviewRequest,
    PaymentReversalRead,
    PaymentReversalRequest,
    PaymentSettlementConfirm,
    PaymentSettlementEvidenceInspectionRead,
    PaymentSettlementRead,
    PaymentSettlementReconciliationRequest,
    PaymentSyncRead,
    PaymentUpdate,
    PaymentVerifyRequest,
    PaymentVerifyResponse,
    TaxRateCreate,
    TaxRateRead,
    TaxRateUpdate,
)
from app.schemas.common import ListResponse
from app.services import api_billing_webhooks as api_billing_webhooks_service
from app.services import billing as billing_service
from app.services import billing_automation as billing_automation_service
from app.services import customer_portal_flow_payments as customer_payments
from app.services.auth_dependencies import require_permission, require_user_auth
from app.services.billing import adjustments as account_adjustment_service
from app.services.customer_context import require_customer_account_id
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext
from app.services.payment_provider_events import (
    ADMINISTRATIVE_INGEST_SCOPE,
    PaymentProviderEventCommand,
    PaymentProviderEventError,
    PaymentProviderEventQuery,
    ProviderEventOrderBy,
    ProviderEventOrderDirection,
)
from app.services.response import list_response as build_list_response
from app.services.sync_feeds import SYNC_FEED_MAX_PAGE_SIZE

router = APIRouter()

# Public router for inbound payment-provider webhooks. These are mounted WITHOUT
# require_user_auth (see main.py) because external providers authenticate by
# HMAC signature, not a session — gating them behind user auth would 401 every
# real callback. Keep ONLY signature-verified provider callbacks here; anything
# that needs an operator session stays on ``router``.
webhook_router = APIRouter()
logger = logging.getLogger(__name__)
PAYMENT_CHARGE_ERROR_MESSAGE = (
    "We could not charge that saved card. Please use another payment method or "
    "try again later."
)
CARD_SAVE_SUCCESS_MESSAGE = "Your card was saved for future payments."
CARD_SAVE_ERROR_MESSAGE = (
    "Payment was recorded, but we could not save this card. You can add a card "
    "from Payment Methods."
)


# --- Dashboard ---


@router.get(
    "/dashboard",
    tags=["billing"],
    dependencies=[Depends(require_permission("reports:billing:read"))],
)
def billing_dashboard(db: Session = Depends(get_db)) -> dict:
    """Billing dashboard stats for external consumers."""
    from app.services.billing.reporting import billing_reporting

    return billing_reporting.get_dashboard_stats(db)


# --- Invoices ---


@router.post(
    "/invoices",
    response_model=InvoiceRead,
    status_code=status.HTTP_201_CREATED,
    tags=["invoices"],
    dependencies=[Depends(require_permission("billing:invoice:create"))],
)
def create_invoice(payload: InvoiceCreate, db: Session = Depends(get_db)):
    return billing_service.invoices.create(db, payload)


@router.get(
    "/invoices/sync",
    response_model=ListResponse[InvoiceSyncRead],
    tags=["invoices"],
    dependencies=[Depends(require_permission("billing:invoice:read"))],
)
def sync_invoices(
    account_id: str | None = None,
    status: str | None = None,
    is_active: bool | None = None,
    updated_since: datetime | None = Query(
        default=None,
        description="Inclusive invoice updated_at watermark for ERP AR sync.",
    ),
    limit: int = Query(default=500, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """Lightweight, deterministic invoice feed for accounting synchronization."""
    return finish_read_response(
        db,
        billing_service.invoices.sync_list_response(
            db,
            account_id=account_id,
            status=status,
            is_active=is_active,
            updated_since=updated_since,
            limit=limit,
            offset=offset,
        ),
    )


@router.get(
    "/invoices/{invoice_id}",
    response_model=InvoiceRead,
    tags=["invoices"],
    dependencies=[Depends(require_permission("billing:invoice:read"))],
)
def get_invoice(invoice_id: str, db: Session = Depends(get_db)):
    return finish_read_response(db, billing_service.invoices.get(db, invoice_id))


@router.get(
    "/invoices",
    response_model=ListResponse[InvoiceRead],
    tags=["invoices"],
    dependencies=[Depends(require_permission("billing:invoice:read"))],
)
def list_invoices(
    account_id: str | None = None,
    status: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    updated_since: datetime | None = Query(
        default=None,
        description=(
            "ISO8601 cutoff; return only invoices with updated_at >= this. "
            "For incremental (watermark) sync. Omit for full/default behavior."
        ),
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return finish_read_response(
        db,
        billing_service.invoices.list_response(
            db,
            account_id,
            status,
            is_active,
            order_by,
            order_dir,
            limit=limit,
            offset=offset,
            updated_since=updated_since,
        ),
    )


@router.patch(
    "/invoices/{invoice_id}",
    response_model=InvoiceRead,
    tags=["invoices"],
    dependencies=[Depends(require_permission("billing:batch:write"))],
)
def update_invoice(
    invoice_id: str, payload: InvoiceUpdate, db: Session = Depends(get_db)
):
    return billing_service.invoices.update(db, invoice_id, payload)


@router.post(
    "/invoices/{invoice_id}/write-off",
    response_model=InvoiceRead,
    tags=["invoices"],
    dependencies=[Depends(require_permission("billing:invoice:update"))],
)
def write_off_invoice(
    invoice_id: str, payload: InvoiceWriteOffRequest, db: Session = Depends(get_db)
):
    raise HTTPException(
        status_code=409,
        detail=(
            "Invoice write-off requires POST /invoices/{invoice_id}/write-off/preview "
            "followed by /write-off/confirm"
        ),
    )


@router.post(
    "/invoices/{invoice_id}/write-off/preview",
    response_model=InvoiceClosurePreviewRead,
    tags=["invoices"],
    dependencies=[Depends(require_permission("billing:invoice:update"))],
)
def preview_write_off_invoice(invoice_id: str, db: Session = Depends(get_db)):
    return billing_service.invoices.preview_write_off(db, invoice_id)


@router.post(
    "/invoices/{invoice_id}/write-off/confirm",
    response_model=InvoiceClosureConfirmationRead,
    tags=["invoices"],
    dependencies=[Depends(require_permission("billing:invoice:update"))],
)
def confirm_write_off_invoice(
    invoice_id: str, payload: InvoiceClosureConfirm, db: Session = Depends(get_db)
):
    return billing_service.invoices.confirm_write_off(db, invoice_id, payload)


@router.post(
    "/invoices/{invoice_id}/void/preview",
    response_model=InvoiceClosurePreviewRead,
    tags=["invoices"],
    dependencies=[Depends(require_permission("billing:invoice:update"))],
)
def preview_void_invoice(invoice_id: str, db: Session = Depends(get_db)):
    return billing_service.invoices.preview_void(db, invoice_id)


@router.post(
    "/invoices/{invoice_id}/void/confirm",
    response_model=InvoiceClosureConfirmationRead,
    tags=["invoices"],
    dependencies=[Depends(require_permission("billing:invoice:update"))],
)
def confirm_void_invoice(
    invoice_id: str, payload: InvoiceClosureConfirm, db: Session = Depends(get_db)
):
    return billing_service.invoices.confirm_void(db, invoice_id, payload)


@router.get(
    "/invoices/{invoice_id}/closure-evidence/inspect",
    response_model=InvoiceClosureEvidenceInspectionRead,
    tags=["invoices"],
    dependencies=[Depends(require_permission("billing:invoice:update"))],
)
def inspect_invoice_closure_evidence(invoice_id: str, db: Session = Depends(get_db)):
    return billing_service.invoices.inspect_closure_evidence(db, invoice_id)


@router.post(
    "/invoices/{invoice_id}/closure-evidence/reconcile",
    response_model=InvoiceClosureConfirmationRead,
    tags=["invoices"],
    dependencies=[Depends(require_permission("billing:invoice:update"))],
)
def reconcile_invoice_closure_evidence(
    invoice_id: str,
    payload: InvoiceClosureReconciliationRequest,
    db: Session = Depends(get_db),
):
    return billing_service.invoices.reconcile_closure_evidence(db, invoice_id, payload)


@router.post(
    "/invoices/bulk-write-off",
    response_model=InvoiceBulkActionResponse,
    tags=["invoices"],
    dependencies=[Depends(require_permission("billing:invoice:update"))],
)
def bulk_write_off_invoices(
    payload: InvoiceBulkWriteOffRequest, db: Session = Depends(get_db)
):
    response = billing_service.invoices.bulk_write_off_response(db, payload)
    return InvoiceBulkActionResponse(**response)


@router.post(
    "/invoices/bulk-void",
    response_model=InvoiceBulkActionResponse,
    tags=["invoices"],
    dependencies=[Depends(require_permission("billing:invoice:update"))],
)
def bulk_void_invoices(payload: InvoiceBulkVoidRequest, db: Session = Depends(get_db)):
    response = billing_service.invoices.bulk_void_response(db, payload)
    return InvoiceBulkActionResponse(**response)


@router.post(
    "/invoice-runs",
    response_model=InvoiceRunResponse,
    tags=["invoices"],
    dependencies=[Depends(require_permission("billing:invoice:update"))],
)
def run_invoice_cycle(payload: InvoiceRunRequest, db: Session = Depends(get_db)):
    return billing_automation_service.run_invoice_cycle(
        db,
        run_at=payload.run_at,
        billing_cycle=payload.billing_cycle,
        dry_run=payload.dry_run,
    )


@router.get(
    "/billing-runs",
    response_model=ListResponse[BillingRunRead],
    tags=["invoices"],
    dependencies=[Depends(require_permission("billing:batch:read"))],
)
def list_billing_runs(
    status: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return finish_read_response(
        db,
        billing_service.billing_runs.list_response(
            db, status, order_by, order_dir, limit, offset
        ),
    )


@router.get(
    "/billing-runs/{run_id}",
    response_model=BillingRunRead,
    tags=["invoices"],
    dependencies=[Depends(require_permission("billing:batch:read"))],
)
def get_billing_run(run_id: str, db: Session = Depends(get_db)):
    return finish_read_response(db, billing_service.billing_runs.get(db, run_id))


@router.delete(
    "/invoices/{invoice_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["invoices"],
    dependencies=[Depends(require_permission("billing:invoice:delete"))],
)
def delete_invoice(invoice_id: str, db: Session = Depends(get_db)):
    billing_service.invoices.delete(db, invoice_id)


# --- Credit Notes ---


@router.post(
    "/credit-notes",
    response_model=CreditNoteRead,
    status_code=status.HTTP_201_CREATED,
    tags=["credit-notes"],
    dependencies=[Depends(require_permission("billing:credit_note:create"))],
)
def create_credit_note(payload: CreditNoteCreate, db: Session = Depends(get_db)):
    return billing_service.credit_notes.create(db, payload)


@router.post(
    "/credit-notes/issue/preview",
    response_model=CreditNoteIssuePreviewRead,
    tags=["credit-notes"],
    dependencies=[Depends(require_permission("billing:credit_note:create"))],
)
def preview_credit_note_issue(
    payload: CreditNoteIssuePreviewRequest, db: Session = Depends(get_db)
):
    return finish_read_response(
        db, billing_service.credit_notes.preview_issue(db, payload)
    )


@router.post(
    "/credit-notes/issue",
    response_model=CreditNoteRead,
    status_code=status.HTTP_201_CREATED,
    tags=["credit-notes"],
    dependencies=[Depends(require_permission("billing:credit_note:create"))],
)
def issue_credit_note(payload: CreditNoteIssueRequest, db: Session = Depends(get_db)):
    return billing_service.credit_notes.issue_with_evidence(db, payload).credit_note


@router.get(
    "/credit-notes/sync",
    response_model=ListResponse[CreditNoteSyncRead],
    tags=["credit-notes"],
    dependencies=[Depends(require_permission("billing:credit_note:read"))],
)
def sync_credit_notes(
    account_id: str | None = None,
    status: str | None = None,
    is_active: bool | None = None,
    updated_since: datetime | None = Query(
        default=None,
        description="Inclusive credit-note updated_at watermark for ERP sync.",
    ),
    limit: int = Query(
        default=SYNC_FEED_MAX_PAGE_SIZE, ge=1, le=SYNC_FEED_MAX_PAGE_SIZE
    ),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return finish_read_response(
        db,
        billing_service.credit_notes.sync_list_response(
            db,
            account_id=account_id,
            status=status,
            is_active=is_active,
            updated_since=updated_since,
            limit=limit,
            offset=offset,
        ),
    )


@router.get(
    "/credit-notes/{credit_note_id}",
    response_model=CreditNoteRead,
    tags=["credit-notes"],
    dependencies=[Depends(require_permission("billing:credit_note:read"))],
)
def get_credit_note(credit_note_id: str, db: Session = Depends(get_db)):
    return finish_read_response(
        db, billing_service.credit_notes.get(db, credit_note_id)
    )


@router.get(
    "/credit-notes",
    response_model=ListResponse[CreditNoteRead],
    tags=["credit-notes"],
    dependencies=[Depends(require_permission("billing:credit_note:read"))],
)
def list_credit_notes(
    account_id: str | None = None,
    invoice_id: str | None = None,
    status: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    updated_since: datetime | None = Query(
        default=None,
        description=(
            "ISO8601 cutoff; return only credit notes with updated_at >= this. "
            "For incremental (watermark) sync. Omit for full/default behavior."
        ),
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return finish_read_response(
        db,
        billing_service.credit_notes.list_response(
            db,
            account_id,
            invoice_id,
            status,
            is_active,
            order_by,
            order_dir,
            limit=limit,
            offset=offset,
            updated_since=updated_since,
        ),
    )


@router.patch(
    "/credit-notes/{credit_note_id}",
    response_model=CreditNoteRead,
    tags=["credit-notes"],
    dependencies=[Depends(require_permission("billing:credit_note:delete"))],
)
def update_credit_note(
    credit_note_id: str, payload: CreditNoteUpdate, db: Session = Depends(get_db)
):
    return billing_service.credit_notes.update(db, credit_note_id, payload)


@router.delete(
    "/credit-notes/{credit_note_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["credit-notes"],
    dependencies=[Depends(require_permission("billing:credit_note:delete"))],
)
def delete_credit_note(credit_note_id: str, db: Session = Depends(get_db)):
    billing_service.credit_notes.delete(db, credit_note_id)


@router.post(
    "/credit-notes/{credit_note_id}/issue/preview",
    response_model=CreditNoteIssuePreviewRead,
    tags=["credit-notes"],
    dependencies=[Depends(require_permission("billing:credit_note:update"))],
)
def preview_draft_credit_note_issue(credit_note_id: str, db: Session = Depends(get_db)):
    return finish_read_response(
        db, billing_service.credit_notes.preview_draft_issue(db, credit_note_id)
    )


@router.post(
    "/credit-notes/{credit_note_id}/issue",
    response_model=CreditNoteRead,
    tags=["credit-notes"],
    dependencies=[Depends(require_permission("billing:credit_note:update"))],
)
def issue_draft_credit_note(
    credit_note_id: str,
    payload: CreditNoteIssueConfirmation,
    db: Session = Depends(get_db),
):
    return billing_service.credit_notes.issue_draft_with_evidence(
        db, credit_note_id, payload
    ).credit_note


@router.post(
    "/credit-notes/{credit_note_id}/void/preview",
    response_model=CreditNoteVoidPreviewRead,
    tags=["credit-notes"],
    dependencies=[Depends(require_permission("billing:credit_note:update"))],
)
def preview_credit_note_void(credit_note_id: str, db: Session = Depends(get_db)):
    return finish_read_response(
        db, billing_service.credit_notes.preview_void(db, credit_note_id)
    )


@router.post(
    "/credit-notes/{credit_note_id}/void",
    response_model=CreditNoteRead,
    tags=["credit-notes"],
    dependencies=[Depends(require_permission("billing:credit_note:update"))],
)
def void_credit_note(
    credit_note_id: str,
    payload: CreditNoteVoidRequest,
    db: Session = Depends(get_db),
):
    return billing_service.credit_notes.void_with_evidence(
        db, credit_note_id, payload
    ).credit_note


@router.post(
    "/credit-notes/{credit_note_id}/apply/preview",
    response_model=CreditNoteApplicationPreviewRead,
    tags=["credit-notes"],
    dependencies=[Depends(require_permission("billing:credit_note:update"))],
)
def preview_credit_note_application(
    credit_note_id: str,
    payload: CreditNoteApplicationPreviewRequest,
    db: Session = Depends(get_db),
):
    return finish_read_response(
        db,
        billing_service.credit_notes.preview_application(db, credit_note_id, payload),
    )


@router.post(
    "/credit-notes/{credit_note_id}/apply",
    response_model=CreditNoteApplicationRead,
    tags=["credit-notes"],
    dependencies=[Depends(require_permission("billing:credit_note:update"))],
)
def apply_credit_note(
    credit_note_id: str, payload: CreditNoteApplyRequest, db: Session = Depends(get_db)
):
    return billing_service.credit_notes.apply(db, credit_note_id, payload)


# --- Collection Accounts ---


@router.post(
    "/collection-accounts",
    response_model=CollectionAccountRead,
    status_code=status.HTTP_201_CREATED,
    tags=["payment-accounts"],
    dependencies=[Depends(require_permission("billing:account:write"))],
)
def create_collection_account(
    payload: CollectionAccountCreate, db: Session = Depends(get_db)
):
    return billing_service.collection_accounts.create(db, payload)


@router.get(
    "/collection-accounts/{account_id}",
    response_model=CollectionAccountRead,
    tags=["payment-accounts"],
    dependencies=[Depends(require_permission("billing:account:read"))],
)
def get_collection_account(account_id: str, db: Session = Depends(get_db)):
    return billing_service.collection_accounts.get(db, account_id)


@router.get(
    "/collection-accounts",
    response_model=ListResponse[CollectionAccountRead],
    tags=["payment-accounts"],
    dependencies=[Depends(require_permission("billing:account:read"))],
)
def list_collection_accounts(
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return billing_service.collection_accounts.list_response(
        db, is_active, order_by, order_dir, limit, offset
    )


@router.patch(
    "/collection-accounts/{account_id}",
    response_model=CollectionAccountRead,
    tags=["payment-accounts"],
    dependencies=[Depends(require_permission("billing:account:write"))],
)
def update_collection_account(
    account_id: str, payload: CollectionAccountUpdate, db: Session = Depends(get_db)
):
    return billing_service.collection_accounts.update(db, account_id, payload)


# --- Payment Channels ---


@router.post(
    "/payment-channels",
    response_model=PaymentChannelRead,
    status_code=status.HTTP_201_CREATED,
    tags=["payment-channels"],
    dependencies=[Depends(require_permission("billing:channel:write"))],
)
def create_payment_channel(
    payload: PaymentChannelCreate, db: Session = Depends(get_db)
):
    return billing_service.payment_channels.create(db, payload)


@router.get(
    "/payment-channels/sync",
    response_model=ListResponse[PaymentChannelRead],
    tags=["payment-channels"],
    dependencies=[Depends(require_permission("billing:channel:read"))],
)
def sync_payment_channels(
    is_active: bool | None = None,
    updated_since: datetime | None = Query(default=None),
    limit: int = Query(
        default=SYNC_FEED_MAX_PAGE_SIZE, ge=1, le=SYNC_FEED_MAX_PAGE_SIZE
    ),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return finish_read_response(
        db,
        billing_service.payment_channels.sync_list_response(
            db,
            is_active=is_active,
            updated_since=updated_since,
            limit=limit,
            offset=offset,
        ),
    )


@router.get(
    "/payment-channels/{channel_id}",
    response_model=PaymentChannelRead,
    tags=["payment-channels"],
    dependencies=[Depends(require_permission("billing:channel:read"))],
)
def get_payment_channel(channel_id: str, db: Session = Depends(get_db)):
    return billing_service.payment_channels.get(db, channel_id)


@router.get(
    "/payment-channels",
    response_model=ListResponse[PaymentChannelRead],
    tags=["payment-channels"],
    dependencies=[Depends(require_permission("billing:channel:read"))],
)
def list_payment_channels(
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return billing_service.payment_channels.list_response(
        db, is_active, order_by, order_dir, limit, offset
    )


@router.patch(
    "/payment-channels/{channel_id}",
    response_model=PaymentChannelRead,
    tags=["payment-channels"],
    dependencies=[Depends(require_permission("billing:channel:write"))],
)
def update_payment_channel(
    channel_id: str, payload: PaymentChannelUpdate, db: Session = Depends(get_db)
):
    return billing_service.payment_channels.update(db, channel_id, payload)


# --- Payment Channel Accounts ---


@router.post(
    "/payment-channel-accounts",
    response_model=PaymentChannelAccountRead,
    status_code=status.HTTP_201_CREATED,
    tags=["payment-channels"],
    dependencies=[Depends(require_permission("billing:channel:write"))],
)
def create_payment_channel_account(
    payload: PaymentChannelAccountCreate, db: Session = Depends(get_db)
):
    return billing_service.payment_channel_accounts.create(db, payload)


@router.get(
    "/payment-channel-accounts/{mapping_id}",
    response_model=PaymentChannelAccountRead,
    tags=["payment-channels"],
    dependencies=[Depends(require_permission("billing:channel:read"))],
)
def get_payment_channel_account(mapping_id: str, db: Session = Depends(get_db)):
    return billing_service.payment_channel_accounts.get(db, mapping_id)


@router.get(
    "/payment-channel-accounts",
    response_model=ListResponse[PaymentChannelAccountRead],
    tags=["payment-channels"],
    dependencies=[Depends(require_permission("billing:channel:read"))],
)
def list_payment_channel_accounts(
    channel_id: str | None = None,
    collection_account_id: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return billing_service.payment_channel_accounts.list_response(
        db,
        channel_id,
        collection_account_id,
        is_active,
        order_by,
        order_dir,
        limit,
        offset,
    )


@router.patch(
    "/payment-channel-accounts/{mapping_id}",
    response_model=PaymentChannelAccountRead,
    tags=["payment-channels"],
    dependencies=[Depends(require_permission("billing:channel:write"))],
)
def update_payment_channel_account(
    mapping_id: str, payload: PaymentChannelAccountUpdate, db: Session = Depends(get_db)
):
    return billing_service.payment_channel_accounts.update(db, mapping_id, payload)


# --- Payment Allocations ---


@router.post(
    "/payment-allocations",
    response_model=PaymentAllocationRead,
    status_code=status.HTTP_201_CREATED,
    tags=["payments"],
    dependencies=[Depends(require_permission("billing:payment:create"))],
)
def create_payment_allocation(
    payload: PaymentAllocationCreate, db: Session = Depends(get_db)
):
    return billing_service.payment_allocations.create(db, payload)


@router.post(
    "/payment-allocations/preview",
    response_model=PaymentAllocationPreviewRead,
    tags=["payments"],
    dependencies=[Depends(require_permission("billing:payment:create"))],
)
def preview_payment_allocation(
    payload: PaymentAllocationPreviewRequest,
    db: Session = Depends(get_db),
):
    return finish_read_response(
        db, billing_service.payment_allocations.preview(db, payload)
    )


@router.post(
    "/payment-allocations/confirm",
    response_model=PaymentAllocationRead,
    status_code=status.HTTP_201_CREATED,
    tags=["payments"],
    dependencies=[Depends(require_permission("billing:payment:create"))],
)
def confirm_payment_allocation(
    payload: PaymentAllocationConfirm,
    db: Session = Depends(get_db),
):
    return billing_service.payment_allocations.confirm(db, payload).allocation


@router.get(
    "/payment-allocations",
    response_model=ListResponse[PaymentAllocationRead],
    tags=["payments"],
    dependencies=[Depends(require_permission("billing:payment:read"))],
)
def list_payment_allocations(
    payment_id: str | None = None,
    invoice_id: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return finish_read_response(
        db,
        billing_service.payment_allocations.list_response(
            db, payment_id, invoice_id, order_by, order_dir, limit, offset
        ),
    )


@router.delete(
    "/payment-allocations/{allocation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["payments"],
    dependencies=[Depends(require_permission("billing:payment:delete"))],
)
def delete_payment_allocation(allocation_id: str, db: Session = Depends(get_db)):
    billing_service.payment_allocations.delete(db, allocation_id)


# --- Credit Note Lines ---


@router.post(
    "/credit-note-lines",
    response_model=CreditNoteLineRead,
    status_code=status.HTTP_201_CREATED,
    tags=["credit-notes"],
    dependencies=[Depends(require_permission("billing:credit_note:update"))],
)
def create_credit_note_line(
    payload: CreditNoteLineCreate, db: Session = Depends(get_db)
):
    return billing_service.credit_note_lines.create(db, payload)


@router.get(
    "/credit-note-lines/{line_id}",
    response_model=CreditNoteLineRead,
    tags=["credit-notes"],
    dependencies=[Depends(require_permission("billing:credit_note:read"))],
)
def get_credit_note_line(line_id: str, db: Session = Depends(get_db)):
    return billing_service.credit_note_lines.get(db, line_id)


@router.get(
    "/credit-note-lines",
    response_model=ListResponse[CreditNoteLineRead],
    tags=["credit-notes"],
    dependencies=[Depends(require_permission("billing:credit_note:read"))],
)
def list_credit_note_lines(
    credit_note_id: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return billing_service.credit_note_lines.list_response(
        db, credit_note_id, is_active, order_by, order_dir, limit, offset
    )


@router.patch(
    "/credit-note-lines/{line_id}",
    response_model=CreditNoteLineRead,
    tags=["credit-notes"],
    dependencies=[Depends(require_permission("billing:credit_note:update"))],
)
def update_credit_note_line(
    line_id: str, payload: CreditNoteLineUpdate, db: Session = Depends(get_db)
):
    return billing_service.credit_note_lines.update(db, line_id, payload)


@router.delete(
    "/credit-note-lines/{line_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["credit-notes"],
    dependencies=[Depends(require_permission("billing:credit_note:update"))],
)
def delete_credit_note_line(line_id: str, db: Session = Depends(get_db)):
    billing_service.credit_note_lines.delete(db, line_id)


# --- Credit Note Applications ---


@router.get(
    "/credit-note-applications",
    response_model=ListResponse[CreditNoteApplicationRead],
    tags=["credit-notes"],
    dependencies=[Depends(require_permission("billing:credit_note:read"))],
)
def list_credit_note_applications(
    credit_note_id: str | None = None,
    invoice_id: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return billing_service.credit_note_applications.list_response(
        db, credit_note_id, invoice_id, order_by, order_dir, limit, offset
    )


@router.get(
    "/credit-note-applications/{application_id}",
    response_model=CreditNoteApplicationRead,
    tags=["credit-notes"],
    dependencies=[Depends(require_permission("billing:credit_note:read"))],
)
def get_credit_note_application(application_id: str, db: Session = Depends(get_db)):
    return billing_service.credit_note_applications.get(db, application_id)


# --- Invoice Lines ---


@router.post(
    "/invoice-lines",
    response_model=InvoiceLineRead,
    status_code=status.HTTP_201_CREATED,
    tags=["invoice-lines"],
    dependencies=[Depends(require_permission("billing:invoice:update"))],
)
def create_invoice_line(payload: InvoiceLineCreate, db: Session = Depends(get_db)):
    return billing_service.invoice_lines.create(db, payload)


@router.get(
    "/invoice-lines/{line_id}",
    response_model=InvoiceLineRead,
    tags=["invoice-lines"],
    dependencies=[Depends(require_permission("billing:invoice:read"))],
)
def get_invoice_line(line_id: str, db: Session = Depends(get_db)):
    return billing_service.invoice_lines.get(db, line_id)


@router.get(
    "/invoice-lines",
    response_model=ListResponse[InvoiceLineRead],
    tags=["invoice-lines"],
    dependencies=[Depends(require_permission("billing:invoice:read"))],
)
def list_invoice_lines(
    invoice_id: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return billing_service.invoice_lines.list_response(
        db, invoice_id, is_active, order_by, order_dir, limit, offset
    )


@router.patch(
    "/invoice-lines/{line_id}",
    response_model=InvoiceLineRead,
    tags=["invoice-lines"],
    dependencies=[Depends(require_permission("billing:invoice:update"))],
)
def update_invoice_line(
    line_id: str, payload: InvoiceLineUpdate, db: Session = Depends(get_db)
):
    return billing_service.invoice_lines.update(db, line_id, payload)


@router.delete(
    "/invoice-lines/{line_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["invoice-lines"],
    dependencies=[Depends(require_permission("billing:invoice:update"))],
)
def delete_invoice_line(line_id: str, db: Session = Depends(get_db)):
    billing_service.invoice_lines.delete(db, line_id)


# --- Payment Methods ---


@router.post(
    "/payment-methods",
    response_model=PaymentMethodRead,
    status_code=status.HTTP_201_CREATED,
    tags=["payment-methods"],
    dependencies=[Depends(require_permission("billing:payment:create"))],
)
def create_payment_method(payload: PaymentMethodCreate, db: Session = Depends(get_db)):
    return billing_service.payment_methods.create(db, payload)


@router.get(
    "/payment-methods/{method_id}",
    response_model=PaymentMethodRead,
    tags=["payment-methods"],
    dependencies=[Depends(require_permission("billing:payment:read"))],
)
def get_payment_method(method_id: str, db: Session = Depends(get_db)):
    return billing_service.payment_methods.get(db, method_id)


@router.get(
    "/payment-methods",
    response_model=ListResponse[PaymentMethodRead],
    tags=["payment-methods"],
    dependencies=[Depends(require_permission("billing:payment:read"))],
)
def list_payment_methods(
    account_id: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return billing_service.payment_methods.list_response(
        db, account_id, is_active, order_by, order_dir, limit, offset
    )


@router.patch(
    "/payment-methods/{method_id}",
    response_model=PaymentMethodRead,
    tags=["payment-methods"],
    dependencies=[Depends(require_permission("billing:payment:update"))],
)
def update_payment_method(
    method_id: str, payload: PaymentMethodUpdate, db: Session = Depends(get_db)
):
    return billing_service.payment_methods.update(db, method_id, payload)


@router.delete(
    "/payment-methods/{method_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["payment-methods"],
    dependencies=[Depends(require_permission("billing:payment:delete"))],
)
def delete_payment_method(method_id: str, db: Session = Depends(get_db)):
    billing_service.payment_methods.delete(db, method_id)


# --- Payment Providers ---


@router.get(
    "/payment-providers/{provider_id}",
    response_model=PaymentProviderRead,
    tags=["payment-providers"],
    dependencies=[Depends(require_permission("billing:provider:read"))],
)
def get_payment_provider(provider_id: str, db: Session = Depends(get_db)):
    return billing_service.payment_providers.get(db, provider_id)


@router.get(
    "/payment-providers",
    response_model=ListResponse[PaymentProviderRead],
    tags=["payment-providers"],
    dependencies=[Depends(require_permission("billing:provider:read"))],
)
def list_payment_providers(
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return billing_service.payment_providers.list_response(
        db, is_active, order_by, order_dir, limit, offset
    )


# --- Payment Events ---


def _payment_provider_event_error(exc: DomainError) -> Never:
    suffix = exc.code.removeprefix("financial.payment_provider_events.")
    status_code = {
        "provider_not_found": status.HTTP_404_NOT_FOUND,
        "event_not_found": status.HTTP_404_NOT_FOUND,
        "invoice_not_found": status.HTTP_404_NOT_FOUND,
        "payment_not_found": status.HTTP_404_NOT_FOUND,
        "replay_conflict": status.HTTP_409_CONFLICT,
        "identity_collision": status.HTTP_409_CONFLICT,
        "untrusted_financial_effect": status.HTTP_409_CONFLICT,
        "untrusted_financial_observation": status.HTTP_409_CONFLICT,
        "active_caller_transaction": status.HTTP_409_CONFLICT,
        "nested_owner_command": status.HTTP_409_CONFLICT,
        "nested_transaction_completion": status.HTTP_409_CONFLICT,
        "command_contract_violation": status.HTTP_500_INTERNAL_SERVER_ERROR,
        "invalid_command_context": status.HTTP_500_INTERNAL_SERVER_ERROR,
    }.get(suffix, status.HTTP_422_UNPROCESSABLE_ENTITY)
    detail = (
        exc.message
        if exc.code.startswith("financial.payment_provider_events.")
        else "Payment provider event command failed."
    )
    raise HTTPException(status_code=status_code, detail=detail) from exc


@router.post(
    "/payment-events/ingest",
    response_model=PaymentProviderEventRead,
    tags=["payment-events"],
)
def ingest_payment_event(
    payload: PaymentProviderEventIngest,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_permission("billing:provider:write")),
):
    actor_type, actor_id = _financial_actor(principal)
    if actor_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Financial principal identity is incomplete.",
        )
    command = PaymentProviderEventCommand(
        provider_id=payload.provider_id,
        payment_id=payload.payment_id,
        invoice_id=payload.invoice_id,
        account_id=payload.account_id,
        billing_account_id=payload.billing_account_id,
        event_type=payload.event_type,
        external_id=payload.external_id,
        idempotency_key=payload.idempotency_key,
        amount=payload.amount,
        provider_fee=payload.provider_fee,
        net_amount=payload.net_amount,
        provider_reference=payload.provider_reference,
        topup_intent_id=payload.topup_intent_id,
        currency=payload.currency,
        financial_effect=payload.financial_effect,
        payload=payload.payload,
        observed_payment_status=payload.status_hint,
    )
    context = CommandContext.system(
        actor=f"{actor_type.value}:{actor_id}",
        scope=ADMINISTRATIVE_INGEST_SCOPE,
        reason="Record an administrative payment-provider observation",
        idempotency_key=payload.idempotency_key or payload.external_id,
    )
    try:
        db_session_adapter.release_read_transaction(db)
        return billing_service.payment_provider_events.ingest(
            db,
            command,
            context=context,
        )
    except DomainError as exc:
        _payment_provider_event_error(exc)


@router.get(
    "/payment-events/{event_id}",
    response_model=PaymentProviderEventRead,
    tags=["payment-events"],
    dependencies=[Depends(require_permission("billing:provider:read"))],
)
def get_payment_event(event_id: UUID, db: Session = Depends(get_db)):
    try:
        return billing_service.payment_provider_events.get(db, event_id)
    except PaymentProviderEventError as exc:
        _payment_provider_event_error(exc)


@router.get(
    "/payment-events",
    response_model=ListResponse[PaymentProviderEventRead],
    tags=["payment-events"],
    dependencies=[Depends(require_permission("billing:provider:read"))],
)
def list_payment_events(
    provider_id: UUID | None = None,
    payment_id: UUID | None = None,
    invoice_id: UUID | None = None,
    event_status: PaymentProviderEventStatus | None = Query(
        default=None, alias="status"
    ),
    order_by: ProviderEventOrderBy = Query(default=ProviderEventOrderBy.received_at),
    order_dir: ProviderEventOrderDirection = Query(
        default=ProviderEventOrderDirection.descending
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    try:
        items = billing_service.payment_provider_events.list(
            db,
            PaymentProviderEventQuery(
                provider_id=provider_id,
                payment_id=payment_id,
                invoice_id=invoice_id,
                status=event_status,
                order_by=order_by,
                order_direction=order_dir,
                limit=limit,
                offset=offset,
            ),
        )
    except PaymentProviderEventError as exc:
        _payment_provider_event_error(exc)
    return build_list_response(list(items), limit, offset)


@webhook_router.post(
    "/payment-events/paystack",
    tags=["payment-events"],
)
async def paystack_webhook(request: Request, db: Session = Depends(get_db)):
    """Handle Paystack payment webhook.

    Uses ``async def`` (exception to the sync-handler rule) because
    ``await request.body()`` is required to read the raw payload for
    HMAC signature verification before any JSON parsing occurs.
    """
    with webhook_observation(provider="paystack", event="payment"):
        body = await request.body()
        signature = request.headers.get("X-Paystack-Signature", "")
        return api_billing_webhooks_service.process_paystack_webhook(
            db=db,
            body=body,
            signature=signature,
        )


@webhook_router.post(
    "/payment-events/flutterwave",
    tags=["payment-events"],
)
async def flutterwave_webhook(request: Request, db: Session = Depends(get_db)):
    """Handle Flutterwave payment webhook.

    Uses ``async def`` (exception to the sync-handler rule) because
    ``await request.body()`` is required to read the raw payload for
    hash signature verification before any JSON parsing occurs.
    """
    with webhook_observation(provider="flutterwave", event="payment"):
        body = await request.body()
        signature = request.headers.get("verif-hash", "")
        return api_billing_webhooks_service.process_flutterwave_webhook(
            db=db,
            body=body,
            signature=signature,
        )


# --- Bank Accounts ---


@router.post(
    "/bank-accounts",
    response_model=BankAccountRead,
    status_code=status.HTTP_201_CREATED,
    tags=["bank-accounts"],
    dependencies=[Depends(require_permission("billing:provider:write"))],
)
def create_bank_account(payload: BankAccountCreate, db: Session = Depends(get_db)):
    return billing_service.bank_accounts.create(db, payload)


@router.get(
    "/bank-accounts/{bank_account_id}",
    response_model=BankAccountRead,
    tags=["bank-accounts"],
    dependencies=[Depends(require_permission("billing:provider:read"))],
)
def get_bank_account(bank_account_id: str, db: Session = Depends(get_db)):
    return billing_service.bank_accounts.get(db, bank_account_id)


@router.get(
    "/bank-accounts",
    response_model=ListResponse[BankAccountRead],
    tags=["bank-accounts"],
    dependencies=[Depends(require_permission("billing:provider:read"))],
)
def list_bank_accounts(
    account_id: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return billing_service.bank_accounts.list_response(
        db, account_id, is_active, order_by, order_dir, limit, offset
    )


@router.patch(
    "/bank-accounts/{bank_account_id}",
    response_model=BankAccountRead,
    tags=["bank-accounts"],
    dependencies=[Depends(require_permission("billing:provider:write"))],
)
def update_bank_account(
    bank_account_id: str, payload: BankAccountUpdate, db: Session = Depends(get_db)
):
    return billing_service.bank_accounts.update(db, bank_account_id, payload)


@router.delete(
    "/bank-accounts/{bank_account_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["bank-accounts"],
    dependencies=[Depends(require_permission("billing:provider:write"))],
)
def delete_bank_account(bank_account_id: str, db: Session = Depends(get_db)):
    billing_service.bank_accounts.delete(db, bank_account_id)


# --- Payments ---


@router.post(
    "/payments",
    response_model=PaymentRead,
    status_code=status.HTTP_201_CREATED,
    tags=["payments"],
    dependencies=[Depends(require_permission("billing:payment:create"))],
)
def create_payment(payload: PaymentCreate, db: Session = Depends(get_db)):
    if payload.status == PaymentStatus.succeeded:
        raise HTTPException(
            status_code=409,
            detail=(
                "Confirmed payment creation requires /payments/creation/preview "
                "and /payments/creation/confirm"
            ),
        )
    return billing_service.payments.create(db, payload)


@router.post(
    "/payments/creation/preview",
    response_model=PaymentCreationPreviewRead,
    tags=["payments"],
    dependencies=[Depends(require_permission("billing:payment:create"))],
)
def preview_payment_creation(
    payload: PaymentCreationPreviewRequest,
    db: Session = Depends(get_db),
):
    return finish_read_response(
        db, billing_service.payments.preview_creation(db, payload)
    )


@router.post(
    "/payments/creation/confirm",
    response_model=PaymentRead,
    status_code=status.HTTP_201_CREATED,
    tags=["payments"],
    dependencies=[Depends(require_permission("billing:payment:create"))],
)
def confirm_payment_creation(
    payload: PaymentCreationConfirm,
    db: Session = Depends(get_db),
):
    return billing_service.payments.confirm_creation(db, payload).payment


@router.get(
    "/payments/sync",
    response_model=ListResponse[PaymentSyncRead],
    tags=["payments"],
    dependencies=[Depends(require_permission("billing:payment:read"))],
)
def sync_payments(
    account_id: str | None = None,
    status: str | None = None,
    is_active: bool | None = None,
    updated_since: datetime | None = Query(
        default=None,
        description="Inclusive payment updated_at watermark for ERP sync.",
    ),
    limit: int = Query(
        default=SYNC_FEED_MAX_PAGE_SIZE, ge=1, le=SYNC_FEED_MAX_PAGE_SIZE
    ),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return finish_read_response(
        db,
        billing_service.payments.sync_list_response(
            db,
            account_id=account_id,
            status=status,
            is_active=is_active,
            updated_since=updated_since,
            limit=limit,
            offset=offset,
        ),
    )


@router.post(
    "/payments/{payment_id}/refund/preview",
    response_model=PaymentRefundPreviewRead | BillingAccountPaymentRefundPreviewRead,
    tags=["payments"],
    dependencies=[Depends(require_permission("billing:payment:update"))],
)
def preview_payment_refund(
    payment_id: str,
    payload: PaymentRefundPreviewRequest,
    db: Session = Depends(get_db),
):
    payment = billing_service.payments.get(db, payment_id)
    if payment.billing_account_id is not None:
        return billing_service.consolidated_payment_refunds.preview(
            db, payment_id, payload
        )
    return billing_service.refunds.preview(db, payment_id, payload)


@router.post(
    "/payments/{payment_id}/refund",
    response_model=PaymentRefundRead,
    status_code=status.HTTP_201_CREATED,
    tags=["payments"],
    dependencies=[Depends(require_permission("billing:payment:update"))],
)
def confirm_payment_refund(
    payment_id: str,
    payload: PaymentRefundRequest,
    db: Session = Depends(get_db),
):
    payment = billing_service.payments.get(db, payment_id)
    if payment.billing_account_id is not None:
        return billing_service.consolidated_payment_refunds.confirm(
            db,
            payment_id,
            BillingAccountPaymentRefundRequest(**payload.model_dump()),
        ).refund
    return billing_service.refunds.process_with_evidence(db, payment_id, payload).refund


@router.post(
    "/payments/{payment_id}/reversal/preview",
    response_model=(
        PaymentReversalPreviewRead | BillingAccountPaymentReversalPreviewRead
    ),
    tags=["payments"],
    dependencies=[Depends(require_permission("billing:payment:update"))],
)
def preview_payment_reversal(
    payment_id: str,
    payload: PaymentReversalPreviewRequest,
    db: Session = Depends(get_db),
):
    payment = billing_service.payments.get(db, payment_id)
    if payment.billing_account_id is not None:
        return billing_service.consolidated_payment_reversals.preview(
            db, payment_id, payload
        )
    return billing_service.reversals.preview(db, payment_id, payload)


@router.post(
    "/payments/{payment_id}/reversal",
    response_model=PaymentReversalRead,
    status_code=status.HTTP_201_CREATED,
    tags=["payments"],
    dependencies=[Depends(require_permission("billing:payment:update"))],
)
def confirm_payment_reversal(
    payment_id: str,
    payload: PaymentReversalRequest,
    db: Session = Depends(get_db),
):
    payment = billing_service.payments.get(db, payment_id)
    if payment.billing_account_id is not None:
        return billing_service.consolidated_payment_reversals.confirm(
            db,
            payment_id,
            BillingAccountPaymentReversalRequest(**payload.model_dump()),
        ).reversal
    return billing_service.reversals.process_with_evidence(
        db, payment_id, payload
    ).reversal


@router.post(
    "/payments/{payment_id}/settlement/preview",
    response_model=PaymentCreationPreviewRead,
    tags=["payments"],
    dependencies=[Depends(require_permission("billing:payment:update"))],
)
def preview_payment_settlement(payment_id: str, db: Session = Depends(get_db)):
    return finish_read_response(
        db, billing_service.payments.preview_settlement(db, payment_id)
    )


@router.post(
    "/payments/{payment_id}/settlement",
    response_model=PaymentRead,
    tags=["payments"],
    dependencies=[Depends(require_permission("billing:payment:update"))],
)
def confirm_payment_settlement(
    payment_id: str,
    payload: PaymentSettlementConfirm,
    db: Session = Depends(get_db),
):
    return billing_service.payments.settle(
        db,
        payment_id,
        preview_fingerprint=payload.preview_fingerprint,
        idempotency_key=payload.idempotency_key,
        origin=PaymentSettlementOrigin.manual,
    ).payment


@router.get(
    "/payments/{payment_id}/settlement/evidence",
    response_model=PaymentSettlementEvidenceInspectionRead,
    tags=["payments"],
    dependencies=[Depends(require_permission("billing:ledger:read"))],
)
def inspect_payment_settlement_evidence(
    payment_id: str,
    db: Session = Depends(get_db),
):
    return finish_read_response(
        db, billing_service.payments.inspect_settlement_evidence(db, payment_id)
    )


@router.post(
    "/payments/{payment_id}/settlement/evidence/reconcile",
    response_model=PaymentSettlementRead,
    tags=["payments"],
    dependencies=[Depends(require_permission("billing:ledger:write"))],
)
def reconcile_payment_settlement_evidence(
    payment_id: str,
    payload: PaymentSettlementReconciliationRequest,
    db: Session = Depends(get_db),
):
    return billing_service.payments.reconcile_settlement_evidence(
        db, payment_id, payload
    )


@router.get(
    "/consolidated-payments/{payment_id}/settlement/evidence",
    response_model=BillingAccountPaymentSettlementEvidenceInspectionRead,
    tags=["payments"],
    dependencies=[Depends(require_permission("billing:ledger:read"))],
)
def inspect_consolidated_payment_settlement_evidence(
    payment_id: str,
    db: Session = Depends(get_db),
):
    return finish_read_response(
        db,
        billing_service.consolidated_payment_settlements.inspect_reconciliation_evidence(
            db, payment_id
        ),
    )


@router.post(
    "/consolidated-payments/{payment_id}/settlement/evidence/preview",
    response_model=BillingAccountPaymentSettlementReconciliationPreviewRead,
    tags=["payments"],
    dependencies=[Depends(require_permission("billing:ledger:write"))],
)
def preview_consolidated_payment_settlement_evidence(
    payment_id: str,
    payload: BillingAccountPaymentSettlementReconciliationRequest,
    db: Session = Depends(get_db),
):
    return finish_read_response(
        db,
        billing_service.consolidated_payment_settlements.preview_reconciliation(
            db, payment_id, payload
        ),
    )


@router.post(
    "/consolidated-payments/{payment_id}/settlement/evidence/reconcile",
    response_model=BillingAccountPaymentSettlementReconciliationResultRead,
    status_code=status.HTTP_201_CREATED,
    tags=["payments"],
)
def reconcile_consolidated_payment_settlement_evidence(
    payment_id: str,
    payload: BillingAccountPaymentSettlementReconciliationConfirm,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_permission("billing:ledger:write")),
):
    actor_type, actor_id = _financial_actor(principal)
    result = (
        billing_service.consolidated_payment_settlements.reconcile_historical_evidence(
            db,
            payment_id,
            payload,
            actor_type=actor_type,
            actor_id=actor_id,
        )
    )
    evidence = result.evidence
    if evidence.provider_event_id is not None:
        provenance_type = "provider_event"
        provenance_id = evidence.provider_event_id
    elif evidence.payment_proof_id is not None:
        provenance_type = "payment_proof"
        provenance_id = evidence.payment_proof_id
    else:
        if evidence.topup_intent_id is None:
            raise HTTPException(
                status_code=500,
                detail="Consolidated settlement provenance evidence is incomplete",
            )
        provenance_type = "topup_intent"
        provenance_id = evidence.topup_intent_id
    return {
        "settlement": result.settlement,
        "reconciliation_evidence_id": evidence.id,
        "provenance_type": provenance_type,
        "provenance_id": provenance_id,
        "idempotent_replay": result.idempotent_replay,
    }


@router.get(
    "/billing-accounts/{billing_account_id}/credit-consumption/evidence",
    response_model=BillingAccountCreditConsumptionEvidenceInspectionRead,
    tags=["payments"],
    dependencies=[Depends(require_permission("billing:ledger:read"))],
)
def inspect_consolidated_credit_consumption_evidence(
    billing_account_id: str,
    db: Session = Depends(get_db),
):
    return finish_read_response(
        db,
        billing_service.consolidated_credit_allocations.inspect_reconciliation_evidence(
            db, billing_account_id
        ),
    )


@router.post(
    "/billing-accounts/{billing_account_id}/credit-consumption/evidence/preview",
    response_model=BillingAccountCreditConsumptionReconciliationPreviewRead,
    tags=["payments"],
    dependencies=[Depends(require_permission("billing:ledger:write"))],
)
def preview_consolidated_credit_consumption_evidence(
    billing_account_id: str,
    payload: BillingAccountCreditConsumptionReconciliationRequest,
    db: Session = Depends(get_db),
):
    return finish_read_response(
        db,
        billing_service.consolidated_credit_allocations.preview_reconciliation(
            db, billing_account_id, payload
        ),
    )


@router.post(
    "/billing-accounts/{billing_account_id}/credit-consumption/evidence/reconcile",
    response_model=BillingAccountCreditConsumptionReconciliationResultRead,
    status_code=status.HTTP_201_CREATED,
    tags=["payments"],
)
def reconcile_consolidated_credit_consumption_evidence(
    billing_account_id: str,
    payload: BillingAccountCreditConsumptionReconciliationConfirm,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_permission("billing:ledger:write")),
):
    actor_type, actor_id = _financial_actor(principal)
    return billing_service.consolidated_credit_allocations.reconcile_historical_consumption(
        db,
        billing_account_id,
        payload,
        actor_type=actor_type,
        actor_id=actor_id,
    )


@router.get(
    "/consolidated-payments/{payment_id}/returns/{return_type}/{return_id}/evidence",
    response_model=BillingAccountPaymentReturnEvidenceInspectionRead,
    tags=["payments"],
    dependencies=[Depends(require_permission("billing:ledger:read"))],
)
def inspect_consolidated_payment_return_evidence(
    payment_id: str,
    return_type: str,
    return_id: str,
    db: Session = Depends(get_db),
):
    return finish_read_response(
        db,
        billing_service.consolidated_payment_return_reconciliations.inspect_evidence(
            db, payment_id, return_type, return_id
        ),
    )


@router.post(
    "/consolidated-payments/{payment_id}/returns/{return_type}/{return_id}/evidence/preview",
    response_model=BillingAccountPaymentReturnReconciliationPreviewRead,
    tags=["payments"],
    dependencies=[Depends(require_permission("billing:ledger:write"))],
)
def preview_consolidated_payment_return_evidence(
    payment_id: str,
    return_type: str,
    return_id: str,
    payload: BillingAccountPaymentReturnReconciliationRequest,
    db: Session = Depends(get_db),
):
    return finish_read_response(
        db,
        billing_service.consolidated_payment_return_reconciliations.preview(
            db, payment_id, return_type, return_id, payload
        ),
    )


@router.post(
    "/consolidated-payments/{payment_id}/returns/{return_type}/{return_id}/evidence/reconcile",
    response_model=BillingAccountPaymentReturnReconciliationResultRead,
    status_code=status.HTTP_201_CREATED,
    tags=["payments"],
)
def reconcile_consolidated_payment_return_evidence(
    payment_id: str,
    return_type: str,
    return_id: str,
    payload: BillingAccountPaymentReturnReconciliationConfirm,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_permission("billing:ledger:write")),
):
    actor_type, actor_id = _financial_actor(principal)
    return billing_service.consolidated_payment_return_reconciliations.reconcile_historical_evidence(
        db,
        payment_id,
        return_type,
        return_id,
        payload,
        actor_type=actor_type,
        actor_id=actor_id,
    )


@router.get(
    "/consolidated-payments/{payment_id}/return-document-reconstruction/{return_type}/evidence",
    response_model=BillingAccountMissingPaymentReturnEvidenceInspectionRead,
    tags=["payments"],
    dependencies=[Depends(require_permission("billing:ledger:read"))],
)
def inspect_missing_consolidated_payment_return_document(
    payment_id: str,
    return_type: str,
    db: Session = Depends(get_db),
):
    return finish_read_response(
        db,
        billing_service.consolidated_payment_return_reconciliations.inspect_missing_document_evidence(
            db, payment_id, return_type
        ),
    )


@router.post(
    "/consolidated-payments/{payment_id}/return-document-reconstruction/{return_type}/preview",
    response_model=BillingAccountPaymentReturnDocumentReconstructionPreviewRead,
    tags=["payments"],
    dependencies=[Depends(require_permission("billing:ledger:write"))],
)
def preview_missing_consolidated_payment_return_document(
    payment_id: str,
    return_type: str,
    payload: BillingAccountPaymentReturnDocumentReconstructionRequest,
    db: Session = Depends(get_db),
):
    return finish_read_response(
        db,
        billing_service.consolidated_payment_return_reconciliations.preview_document_reconstruction(
            db, payment_id, return_type, payload
        ),
    )


@router.post(
    "/consolidated-payments/{payment_id}/return-document-reconstruction/{return_type}/reconstruct",
    response_model=BillingAccountPaymentReturnDocumentReconstructionResultRead,
    status_code=status.HTTP_201_CREATED,
    tags=["payments"],
)
def reconstruct_missing_consolidated_payment_return_document(
    payment_id: str,
    return_type: str,
    payload: BillingAccountPaymentReturnDocumentReconstructionConfirm,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_permission("billing:ledger:write")),
):
    actor_type, actor_id = _financial_actor(principal)
    return billing_service.consolidated_payment_return_reconciliations.reconstruct_missing_document(
        db,
        payment_id,
        return_type,
        payload,
        actor_type=actor_type,
        actor_id=actor_id,
    )


# --- Customer-initiated online payment (hosted checkout) ------------------
# Self-scoped to the authenticated subscriber; intentionally NOT gated by a
# billing:* permission so a customer can pay their own invoice from the mobile
# app / self-care SPA. Routes are declared before "/payments/{payment_id}" so
# the static "initiate"/"verify" segments are not captured as a payment id.


def _customer_from_principal(principal: dict) -> dict:
    """Adapt a bearer-auth principal to the `customer` dict the portal payment
    services expect. account_id == subscriber_id == Subscriber.id."""
    subscriber_id = principal.get("subscriber_id")
    return {
        "account_id": subscriber_id,
        "subscriber_id": subscriber_id,
        "username": "",
    }


def _require_subscriber(principal: dict) -> dict:
    if principal.get("principal_type") != "subscriber":
        raise HTTPException(status_code=403, detail="A customer account is required")
    return _customer_from_principal(principal)


@router.post(
    "/payments/initiate",
    response_model=PaymentInitiateResponse,
    tags=["payments"],
)
def initiate_payment(
    payload: PaymentInitiateRequest,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Begin a hosted-checkout payment for one of the caller's own invoices.

    Returns the provider public key + reference the client uses to complete
    checkout with the provider SDK, after which it calls ``/payments/verify``.
    """
    customer = _require_subscriber(principal)
    # Unified pay-with flow (mirrors top-up): a saved card is charged server-side
    # (charged=True → skip the webview), a gateway choice returns checkout
    # context, and the verified payment is allocated to this invoice.
    try:
        result = customer_payments.create_invoice_payment_intent(
            db,
            customer,
            str(payload.invoice_id),
            provider=payload.provider,
            payment_method_id=(
                str(payload.payment_method_id) if payload.payment_method_id else None
            ),
            idempotency_key=payload.idempotency_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("Customer API saved-card invoice charge failed", exc_info=True)
        raise HTTPException(
            status_code=400,
            detail=PAYMENT_CHARGE_ERROR_MESSAGE,
        ) from exc
    return PaymentInitiateResponse(
        invoice_id=payload.invoice_id,
        invoice_number=result.get("invoice_number"),
        amount=Decimal(str(result.get("amount") or 0)),
        currency=result.get("currency", "NGN"),
        provider_type=result["provider_type"],
        provider_public_key=result.get("provider_public_key"),
        payment_reference=result["reference"],
        customer_email=result.get("customer_email"),
        charged=result.get("charged", False),
        checkout_url=result.get("checkout_url"),
    )


@router.post(
    "/payments/verify",
    response_model=PaymentVerifyResponse,
    tags=["payments"],
)
def verify_payment(
    payload: PaymentVerifyRequest,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Verify a provider transaction and record the payment against the
    caller's invoice. Idempotent on the provider's external transaction id."""
    customer = _require_subscriber(principal)
    try:
        result = customer_payments.verify_and_record_payment(
            db, customer, payload.reference, provider=payload.provider
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    card_saved: bool | None = None
    card_save_message: str | None = None
    if payload.save_card:
        from app.services import (
            customer_portal_flow_payment_methods as customer_cards,
        )

        account_id = require_customer_account_id(db, customer)
        try:
            customer_cards.capture_card_after_payment(
                db, account_id, payload.reference, result["provider_type"]
            )
            card_saved = True
            card_save_message = CARD_SAVE_SUCCESS_MESSAGE
        except Exception:
            logger.warning("Customer API card capture failed", exc_info=True)
            card_saved = False
            card_save_message = CARD_SAVE_ERROR_MESSAGE

    payment = result["payment"]
    invoice = result.get("invoice")
    raw_status = getattr(payment, "status", "succeeded")
    return PaymentVerifyResponse(
        reference=payload.reference,
        payment_id=payment.id,
        invoice_id=getattr(invoice, "id", None),
        amount=Decimal(str(result.get("amount") or getattr(payment, "amount", 0) or 0)),
        currency=getattr(payment, "currency", "NGN"),
        status=getattr(raw_status, "value", str(raw_status)),
        already_recorded=result.get("already_recorded", False),
        card_saved=card_saved,
        card_save_message=card_save_message,
    )


@router.get(
    "/payments/{payment_id}",
    response_model=PaymentRead,
    tags=["payments"],
    dependencies=[Depends(require_permission("billing:payment:read"))],
)
def get_payment(payment_id: str, db: Session = Depends(get_db)):
    return finish_read_response(db, billing_service.payments.get(db, payment_id))


@router.get(
    "/payments",
    response_model=ListResponse[PaymentRead],
    tags=["payments"],
    dependencies=[Depends(require_permission("billing:payment:read"))],
)
def list_payments(
    account_id: str | None = None,
    invoice_id: str | None = None,
    status: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    updated_since: datetime | None = Query(
        default=None,
        description=(
            "ISO8601 cutoff; return only payments with updated_at >= this. "
            "For incremental (watermark) sync. Omit for full/default behavior."
        ),
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return finish_read_response(
        db,
        billing_service.payments.list_response(
            db,
            account_id,
            invoice_id,
            status,
            is_active,
            order_by,
            order_dir,
            limit=limit,
            offset=offset,
            updated_since=updated_since,
        ),
    )


@router.patch(
    "/payments/{payment_id}",
    response_model=PaymentRead,
    tags=["payments"],
    dependencies=[Depends(require_permission("billing:payment:update"))],
)
def update_payment(
    payment_id: str, payload: PaymentUpdate, db: Session = Depends(get_db)
):
    return billing_service.payments.update(db, payment_id, payload)


@router.post(
    "/payments/{payment_id}/mark-succeeded",
    response_model=PaymentRead,
    tags=["payments"],
    dependencies=[Depends(require_permission("billing:payment:update"))],
)
def mark_payment_succeeded(payment_id: str, db: Session = Depends(get_db)):
    raise HTTPException(
        status_code=409,
        detail=(
            "Payment settlement requires /payments/{payment_id}/settlement/preview "
            "and /payments/{payment_id}/settlement"
        ),
    )


@router.post(
    "/payments/{payment_id}/mark-failed",
    response_model=PaymentRead,
    tags=["payments"],
    dependencies=[Depends(require_permission("billing:payment:update"))],
)
def mark_payment_failed(payment_id: str, db: Session = Depends(get_db)):
    return billing_service.payments.mark_status(db, payment_id, status="failed")


@router.delete(
    "/payments/{payment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["payments"],
    dependencies=[Depends(require_permission("billing:payment:delete"))],
)
def delete_payment(payment_id: str, db: Session = Depends(get_db)):
    billing_service.payments.delete(db, payment_id)


# --- Ledger Entries ---


def _financial_actor(principal: dict) -> tuple[AuditActorType, str | None]:
    actor_type = (
        AuditActorType.api_key
        if principal.get("principal_type") == "api_key"
        else AuditActorType.user
    )
    return actor_type, str(principal.get("principal_id") or "") or None


def _account_adjustment_error(exc: DomainError) -> Never:
    suffix = exc.code.removeprefix("financial.account_adjustments.")
    status_code = {
        "invalid_command": status.HTTP_422_UNPROCESSABLE_ENTITY,
        "invalid_configuration": status.HTTP_503_SERVICE_UNAVAILABLE,
        "account_not_found": status.HTTP_404_NOT_FOUND,
        "adjustment_not_found": status.HTTP_404_NOT_FOUND,
        "insufficient_funding": status.HTTP_402_PAYMENT_REQUIRED,
        "idempotency_conflict": status.HTTP_409_CONFLICT,
        "stale_preview": status.HTTP_409_CONFLICT,
        "already_reversed": status.HTTP_409_CONFLICT,
        "incomplete_evidence": status.HTTP_409_CONFLICT,
        "write_conflict": status.HTTP_409_CONFLICT,
        "active_caller_transaction": status.HTTP_409_CONFLICT,
        "nested_owner_command": status.HTTP_409_CONFLICT,
        "nested_transaction_completion": status.HTTP_409_CONFLICT,
        "command_contract_violation": status.HTTP_500_INTERNAL_SERVER_ERROR,
        "invalid_command_context": status.HTTP_500_INTERNAL_SERVER_ERROR,
    }.get(suffix, status.HTTP_500_INTERNAL_SERVER_ERROR)
    detail = (
        exc.message
        if exc.code.startswith("financial.account_adjustments.")
        else "Account-adjustment command failed."
    )
    raise HTTPException(status_code=status_code, detail=detail) from exc


def _account_adjustment_context(
    principal: dict,
    *,
    reason: str,
    idempotency_key: str,
) -> CommandContext:
    actor_type, actor_id = _financial_actor(principal)
    if actor_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Financial principal identity is incomplete.",
        )
    return CommandContext.system(
        actor=f"{actor_type.value}:{actor_id}",
        scope=account_adjustment_service.ACCOUNT_ADJUSTMENT_SCOPE,
        reason=reason,
        idempotency_key=idempotency_key,
    )


@router.post(
    "/account-adjustments/preview",
    response_model=AccountAdjustmentPreviewRead,
    tags=["ledger-entries"],
)
def preview_account_adjustment(
    payload: AccountAdjustmentPreviewRequest,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_permission("billing:ledger:write")),
):
    del principal
    try:
        return account_adjustment_service.preview_account_adjustment(
            db,
            account_adjustment_service.PreviewAccountAdjustmentQuery(request=payload),
        ).as_dict()
    except DomainError as exc:
        _account_adjustment_error(exc)


@router.post(
    "/account-adjustments",
    response_model=AccountAdjustmentRead,
    status_code=status.HTTP_201_CREATED,
    tags=["ledger-entries"],
)
def confirm_account_adjustment(
    payload: AccountAdjustmentConfirm,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_permission("billing:ledger:write")),
):
    context = _account_adjustment_context(
        principal,
        reason="Operator confirmed an account debit preview",
        idempotency_key=payload.idempotency_key,
    )
    try:
        db_session_adapter.release_read_transaction(db)
        return account_adjustment_service.confirm_account_adjustment(
            db,
            account_adjustment_service.ConfirmAccountAdjustmentCommand(
                context=context,
                confirmation=payload,
            ),
        ).adjustment
    except DomainError as exc:
        _account_adjustment_error(exc)


@router.post(
    "/account-adjustments/{adjustment_id}/reversal/preview",
    response_model=AccountAdjustmentReversalPreviewRead,
    tags=["ledger-entries"],
)
def preview_account_adjustment_reversal(
    adjustment_id: str,
    payload: AccountAdjustmentReversalPreviewRequest,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_permission("billing:ledger:write")),
):
    del principal
    try:
        return account_adjustment_service.preview_account_adjustment_reversal(
            db,
            account_adjustment_service.PreviewAccountAdjustmentReversalQuery(
                adjustment_id=UUID(adjustment_id),
                request=payload,
            ),
        ).as_dict()
    except (ValueError, DomainError) as exc:
        if isinstance(exc, DomainError):
            _account_adjustment_error(exc)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid account-adjustment ID.",
        ) from exc


@router.post(
    "/account-adjustments/{adjustment_id}/reversal",
    response_model=AccountAdjustmentRead,
    tags=["ledger-entries"],
)
def confirm_account_adjustment_reversal(
    adjustment_id: str,
    payload: AccountAdjustmentReversalConfirm,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_permission("billing:ledger:write")),
):
    context = _account_adjustment_context(
        principal,
        reason="Operator confirmed an account-debit reversal preview",
        idempotency_key=payload.idempotency_key,
    )
    try:
        resolved_adjustment_id = UUID(adjustment_id)
        db_session_adapter.release_read_transaction(db)
        return account_adjustment_service.reverse_account_adjustment(
            db,
            account_adjustment_service.ReverseAccountAdjustmentCommand(
                context=context,
                adjustment_id=resolved_adjustment_id,
                confirmation=payload,
            ),
        ).adjustment
    except (ValueError, DomainError) as exc:
        if isinstance(exc, DomainError):
            _account_adjustment_error(exc)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid account-adjustment ID.",
        ) from exc


@router.post(
    "/ledger-entries",
    response_model=LedgerEntryRead,
    status_code=status.HTTP_201_CREATED,
    tags=["ledger-entries"],
    dependencies=[Depends(require_permission("billing:ledger:write"))],
)
def create_ledger_entry(payload: LedgerEntryCreate, db: Session = Depends(get_db)):
    del payload, db
    raise HTTPException(
        status_code=409,
        detail=(
            "Direct ledger posting is disabled. Debit adjustments require "
            "/account-adjustments/preview then /account-adjustments; customer "
            "credits require the credit-note owner."
        ),
    )


@router.get(
    "/ledger-entries/{entry_id}",
    response_model=LedgerEntryRead,
    tags=["ledger-entries"],
    dependencies=[Depends(require_permission("billing:ledger:read"))],
)
def get_ledger_entry(entry_id: str, db: Session = Depends(get_db)):
    return finish_read_response(db, billing_service.ledger_entries.get(db, entry_id))


@router.get(
    "/ledger-entries",
    response_model=ListResponse[LedgerEntryRead],
    tags=["ledger-entries"],
    dependencies=[Depends(require_permission("billing:ledger:read"))],
)
def list_ledger_entries(
    account_id: str | None = None,
    entry_type: str | None = None,
    source: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return finish_read_response(
        db,
        billing_service.ledger_entries.list_response(
            db,
            account_id,
            entry_type,
            source,
            is_active,
            order_by,
            order_dir,
            limit,
            offset,
        ),
    )


@router.post(
    "/ledger-entries/{entry_id}/reverse",
    response_model=LedgerEntryRead,
    tags=["ledger-entries"],
    dependencies=[Depends(require_permission("billing:ledger:write"))],
)
def reverse_ledger_entry(
    entry_id: str, memo: str | None = None, db: Session = Depends(get_db)
):
    del entry_id, memo, db
    raise HTTPException(
        status_code=409,
        detail=(
            "Direct ledger reversal is disabled. Reverse the owning financial "
            "document or use the account-adjustment reversal preview/confirmation."
        ),
    )


# DELETE /ledger-entries/{entry_id} is deliberately absent. The ledger is
# append-only: deleting an entry silently moved the account balance with no
# record of why. Undo a posted entry through its owning financial document or
# the previewed account-adjustment reversal contract.


# --- Tax Rates ---


@router.post(
    "/tax-rates",
    response_model=TaxRateRead,
    status_code=status.HTTP_201_CREATED,
    tags=["tax-rates"],
    dependencies=[Depends(require_permission("billing:tax:write"))],
)
def create_tax_rate(payload: TaxRateCreate, db: Session = Depends(get_db)):
    return billing_service.tax_rates.create(db, payload)


@router.get(
    "/tax-rates/sync",
    response_model=ListResponse[TaxRateRead],
    tags=["tax-rates"],
    dependencies=[Depends(require_permission("billing:tax:read"))],
)
def sync_tax_rates(
    is_active: bool | None = None,
    updated_since: datetime | None = Query(default=None),
    limit: int = Query(
        default=SYNC_FEED_MAX_PAGE_SIZE, ge=1, le=SYNC_FEED_MAX_PAGE_SIZE
    ),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return finish_read_response(
        db,
        billing_service.tax_rates.sync_list_response(
            db,
            is_active=is_active,
            updated_since=updated_since,
            limit=limit,
            offset=offset,
        ),
    )


@router.get(
    "/tax-rates/{rate_id}",
    response_model=TaxRateRead,
    tags=["tax-rates"],
    dependencies=[Depends(require_permission("billing:tax:read"))],
)
def get_tax_rate(rate_id: str, db: Session = Depends(get_db)):
    return billing_service.tax_rates.get(db, rate_id)


@router.get(
    "/tax-rates",
    response_model=ListResponse[TaxRateRead],
    tags=["tax-rates"],
    dependencies=[Depends(require_permission("billing:tax:read"))],
)
def list_tax_rates(
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return billing_service.tax_rates.list_response(
        db, is_active, order_by, order_dir, limit, offset
    )


@router.patch(
    "/tax-rates/{rate_id}",
    response_model=TaxRateRead,
    tags=["tax-rates"],
    dependencies=[Depends(require_permission("billing:tax:write"))],
)
def update_tax_rate(
    rate_id: str, payload: TaxRateUpdate, db: Session = Depends(get_db)
):
    return billing_service.tax_rates.update(db, rate_id, payload)


@router.delete(
    "/tax-rates/{rate_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["tax-rates"],
    dependencies=[Depends(require_permission("billing:tax:write"))],
)
def delete_tax_rate(rate_id: str, db: Session = Depends(get_db)):
    billing_service.tax_rates.delete(db, rate_id)


# --- Billing accounts (consolidated reseller billing) ---


@router.get(
    "/billing-accounts/sync",
    response_model=ListResponse[BillingAccountRead],
    tags=["billing-accounts"],
    dependencies=[Depends(require_permission("billing_account:read"))],
)
def sync_billing_accounts(
    reseller_id: str | None = None,
    is_active: bool | None = None,
    updated_since: datetime | None = Query(default=None),
    limit: int = Query(
        default=SYNC_FEED_MAX_PAGE_SIZE, ge=1, le=SYNC_FEED_MAX_PAGE_SIZE
    ),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return finish_read_response(
        db,
        billing_service.billing_accounts.sync_list_response(
            db,
            reseller_id=reseller_id,
            is_active=is_active,
            updated_since=updated_since,
            limit=limit,
            offset=offset,
        ),
    )


@router.get(
    "/billing-accounts",
    response_model=ListResponse[BillingAccountRead],
    tags=["billing-accounts"],
    dependencies=[Depends(require_permission("billing_account:read"))],
)
def list_billing_accounts(
    reseller_id: str | None = None,
    is_active: bool | None = True,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    items = billing_service.billing_accounts.list(
        db,
        reseller_id=reseller_id,
        is_active=is_active,
        order_by=order_by,
        order_dir=order_dir,
        limit=limit,
        offset=offset,
    )
    return finish_read_response(
        db,
        ListResponse[BillingAccountRead](
            items=[BillingAccountRead.model_validate(i) for i in items],
            count=len(items),
            limit=limit,
            offset=offset,
        ),
    )


@router.post(
    "/billing-accounts",
    response_model=BillingAccountRead,
    status_code=status.HTTP_201_CREATED,
    tags=["billing-accounts"],
    dependencies=[Depends(require_permission("billing_account:write"))],
)
def create_billing_account(
    payload: BillingAccountCreate, db: Session = Depends(get_db)
):
    return billing_service.billing_accounts.create(db, payload)


@router.get(
    "/billing-accounts/{billing_account_id}",
    response_model=BillingAccountRead,
    tags=["billing-accounts"],
    dependencies=[Depends(require_permission("billing_account:read"))],
)
def get_billing_account(billing_account_id: str, db: Session = Depends(get_db)):
    return finish_read_response(
        db, billing_service.billing_accounts.get(db, billing_account_id)
    )


@router.patch(
    "/billing-accounts/{billing_account_id}",
    response_model=BillingAccountRead,
    tags=["billing-accounts"],
    dependencies=[Depends(require_permission("billing_account:write"))],
)
def update_billing_account(
    billing_account_id: str,
    payload: BillingAccountUpdate,
    db: Session = Depends(get_db),
):
    return billing_service.billing_accounts.update(db, billing_account_id, payload)


@router.get(
    "/billing-accounts/{billing_account_id}/statement",
    response_model=BillingAccountStatement,
    tags=["billing-accounts"],
    dependencies=[Depends(require_permission("billing_account:read"))],
)
def get_billing_account_statement(
    billing_account_id: str,
    subscribers_limit: int = Query(default=25, ge=1, le=200),
    subscribers_offset: int = Query(default=0, ge=0),
    payments_limit: int = Query(default=25, ge=1, le=200),
    payments_offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return finish_read_response(
        db,
        billing_service.billing_accounts.statement(
            db,
            billing_account_id,
            subscribers_limit=subscribers_limit,
            subscribers_offset=subscribers_offset,
            payments_limit=payments_limit,
            payments_offset=payments_offset,
        ),
    )


@router.post(
    "/billing-accounts/{billing_account_id}/payments/preview",
    response_model=BillingAccountPaymentPreviewRead,
    tags=["billing-accounts"],
    dependencies=[Depends(require_permission("billing_account:distribute"))],
)
def preview_billing_account_payment(
    billing_account_id: str,
    payload: BillingAccountPaymentPreviewRequest,
    db: Session = Depends(get_db),
):
    return finish_read_response(
        db,
        billing_service.consolidated_payment_settlements.preview(
            db, billing_account_id, payload
        ),
    )


@router.post(
    "/billing-accounts/{billing_account_id}/payments",
    response_model=PaymentRead,
    status_code=status.HTTP_201_CREATED,
    tags=["billing-accounts"],
    dependencies=[Depends(require_permission("billing_account:distribute"))],
)
def create_billing_account_payment(
    billing_account_id: str,
    payload: BillingAccountPaymentConfirm,
    db: Session = Depends(get_db),
):
    """Confirm a previewed consolidated payment settlement."""
    return billing_service.consolidated_payment_settlements.confirm(
        db,
        billing_account_id,
        payload,
        origin=PaymentSettlementOrigin.manual,
    ).payment
