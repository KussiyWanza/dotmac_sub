"""Customer self-care endpoints — scoped to the authenticated subscriber.

Unlike the staff-facing billing/catalog/usage list endpoints (which are gated by
`billing:read`/`catalog:read`/... permissions and take an explicit account_id),
these require ONLY authentication and force scoping to the caller's own
`subscriber_id`. They are what the customer mobile app / self-care SPA uses so a
subscriber can read their own data without holding staff permissions.

Mounted at /api/v1/me with router-level require_user_auth (see main.py).
"""

import logging
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.subscriber import Subscriber
from app.models.support import TicketChannel
from app.schemas.billing import (
    AccountBalanceResponse,
    AutopayEnableRequest,
    AutopayStatusResponse,
    BankTransferAccount,
    DirectBankTransferConfig,
    InvoiceRead,
    LedgerEntryRead,
    MyPaymentMethodRead,
    PaymentProviderOption,
    PaymentRead,
    TopupInitiateRequest,
    TopupInitiateResponse,
    TopupPageResponse,
    TopupPreviewRequest,
    TopupPreviewResponse,
    TopupVerifyRequest,
    TopupVerifyResponse,
)
from app.schemas.branding import ResolvedBrandRead
from app.schemas.catalog import (
    AddonPurchaseRequest,
    AddonPurchaseResponse,
    AddonQuoteResponse,
    AddonsAvailableResponse,
    PlanChangePageResponse,
    PlanChangeSubmitRequest,
    PlanChangeSubmitResponse,
    PlanOfferSummary,
    SubscriptionRead,
)
from app.schemas.chat import ChatSessionResponse
from app.schemas.common import ListResponse
from app.schemas.customer_device_commands import (
    CustomerDeviceCommandOutcomeRead,
    CustomerWifiUpdateRequest,
)
from app.schemas.gis import (
    MyLocationRead,
    MyLocationRequestCreate,
    MyLocationRequestRead,
)
from app.schemas.notification import (
    CustomerInboxNotificationRead,
    CustomerNotificationReadRequest,
    CustomerNotificationReadResponse,
    PushTokenRead,
    PushTokenRegister,
)
from app.schemas.portal import (
    CustomerFieldJobChatMessage,
    CustomerFieldJobChatMessageCreate,
    CustomerFieldJobChatThread,
    MyProjectsResponse,
    MyQuotesResponse,
    MyReferralsResponse,
    MyWorkOrdersResponse,
    PortalSessionResponse,
    ProjectItem,
    QuoteDepositInitiateRequest,
    QuoteDepositInitiateResponse,
    QuoteDepositVerifyRequest,
    QuoteDepositVerifyResponse,
    QuoteItem,
    QuoteRequestCreate,
    ReferAFriendRequest,
    ReferAFriendResponse,
    TechnicianLocation,
    TechnicianRatingRequest,
    TechnicianRatingResponse,
    WorkOrderItem,
)
from app.schemas.portal_account_health import PortalAccountHealthRead
from app.schemas.subscriber import (
    AccountDeletionRequest,
    AccountDeletionResponse,
    SubscriberContactCreate,
    SubscriberContactRead,
    SubscriberContactUpdate,
    SubscriberContactWriteResponse,
)
from app.schemas.support import (
    AttachmentMeta,
    MySupportCommentCreate,
    MySupportTicketCreate,
    TicketCommentCreate,
    TicketCommentRead,
    TicketCreate,
    TicketRead,
    TicketResolutionDisputeRequest,
    TicketSatisfactionRequest,
)
from app.schemas.usage import (
    DailyUsageHistoryResponse,
    QuotaBucketRead,
    RadiusAccountingSessionRead,
    UsageSummaryResponse,
)
from app.services import account_deletion as account_deletion_service
from app.services import autopay as autopay_service
from app.services import billing as billing_service
from app.services import catalog as catalog_service
from app.services import chat_session as chat_session_service
from app.services import (
    customer_experience_lifecycle,
    customer_field_job_chat,
    customer_work_order_selfcare,
    quote_deposits,
    quotes_mirror,
    team_inbox_widget,
    web_support_tickets,
)
from app.services import customer_location_requests as location_service
from app.services import customer_portal_contacts as contacts_service
from app.services import customer_portal_flow_addons as customer_addons
from app.services import customer_portal_flow_changes as customer_changes
from app.services import customer_portal_flow_payment_methods as customer_cards
from app.services import customer_portal_flow_payments as customer_payments
from app.services import customer_portal_notifications as customer_notifications_service
from app.services import geocoding as geocoding_service
from app.services import notification as notification_service
from app.services import portal_session as portal_session_service
from app.services import push as push_service
from app.services import referrals as referrals_service
from app.services import support as support_service
from app.services import usage as usage_service
from app.services import usage_summary as usage_summary_service
from app.services.auth_dependencies import require_user_auth
from app.services.bandwidth import (
    add_directions_to_series,
    bandwidth_samples,
    with_subscriber_directions,
)
from app.services.customer_context import require_customer_account_id
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext
from app.services.sales import selfserve as selfserve_service

router = APIRouter(prefix="/me", tags=["me"])
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


def _subscriber_id(principal: dict) -> str:
    """Return the caller's subscriber id, or 403 for non-subscriber principals."""
    if principal.get("principal_type") != "subscriber":
        raise HTTPException(status_code=403, detail="A customer account is required")
    return str(principal["subscriber_id"])


def _customer(db: Session, principal: dict) -> dict:
    """Adapt the bearer principal to the `customer` dict the portal top-up flow
    expects (account_id == subscriber_id; username carries the email)."""
    sid = _subscriber_id(principal)
    subscriber = db.get(Subscriber, sid)
    return {
        "account_id": sid,
        "subscriber_id": sid,
        "username": getattr(subscriber, "email", "") or "",
    }


@router.get("/branding", response_model=ResolvedBrandRead)
def my_branding(
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    from app.services.brand_profiles import resolve_brand

    return resolve_brand(db, subscriber_id=_subscriber_id(principal))


@router.get("/invoices", response_model=ListResponse[InvoiceRead])
def my_invoices(
    status: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    account_id = _subscriber_id(principal)
    return billing_service.invoices.list_response(
        db, account_id, status, None, order_by, order_dir, limit, offset
    )


@router.get("/invoices/{invoice_id}", response_model=InvoiceRead)
def my_invoice(
    invoice_id: str,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    account_id = _subscriber_id(principal)
    invoice = billing_service.invoices.get(db=db, invoice_id=invoice_id)
    if not invoice or str(getattr(invoice, "account_id", "")) != account_id:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return invoice


@router.get("/payments", response_model=ListResponse[PaymentRead])
def my_payments(
    status: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    account_id = _subscriber_id(principal)
    return billing_service.payments.list_response(
        db, account_id, None, status, None, order_by, order_dir, limit, offset
    )


@router.get("/autopay", response_model=AutopayStatusResponse)
def my_autopay_status(
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Whether the caller has autopay enabled, and on which saved card."""
    return autopay_service.get_status(db, _subscriber_id(principal))


@router.post("/autopay", response_model=AutopayStatusResponse)
def my_autopay_enable(
    payload: AutopayEnableRequest,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Enable autopay against a saved card (the default card if unspecified)."""
    account_id = _subscriber_id(principal)
    try:
        autopay_service.enable(
            db,
            account_id,
            str(payload.payment_method_id) if payload.payment_method_id else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return autopay_service.get_status(db, account_id)


@router.delete("/autopay", response_model=AutopayStatusResponse)
def my_autopay_disable(
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Turn off autopay for the caller."""
    account_id = _subscriber_id(principal)
    autopay_service.disable(db, account_id)
    return autopay_service.get_status(db, account_id)


@router.get("/payment-methods", response_model=list[MyPaymentMethodRead])
def my_payment_methods(
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """The caller's saved cards (tokens never exposed)."""
    account_id = _subscriber_id(principal)
    return customer_cards.list_for_account(db, account_id)


@router.patch(
    "/payment-methods/{method_id}/default", response_model=MyPaymentMethodRead
)
def my_set_default_card(
    method_id: str,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Make one of the caller's own saved cards the default."""
    account_id = _subscriber_id(principal)
    method = customer_cards.set_default(db, account_id, method_id)
    if method is None:
        raise HTTPException(status_code=404, detail="Payment method not found")
    return method


@router.delete("/payment-methods/{method_id}", status_code=204)
def my_remove_card(
    method_id: str,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Remove one of the caller's own saved cards."""
    account_id = _subscriber_id(principal)
    if not customer_cards.remove(db, account_id, method_id):
        raise HTTPException(status_code=404, detail="Payment method not found")


@router.get("/balance", response_model=AccountBalanceResponse)
def my_balance(
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """The caller's available customer balance (positive = credit on file)."""
    from app.services.collections import get_available_balance

    account_id = _subscriber_id(principal)
    return AccountBalanceResponse(credit_balance=get_available_balance(db, account_id))


@router.get("/ledger", response_model=ListResponse[LedgerEntryRead])
def my_ledger(
    entry_type: str | None = None,
    source: str | None = None,
    order_by: str = Query(default="effective_date"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """The caller's account ledger — charges, payments, credits, adjustments."""
    account_id = _subscriber_id(principal)
    return billing_service.ledger_entries.list_response(
        db, account_id, entry_type, source, True, order_by, order_dir, limit, offset
    )


@router.get("/subscriptions", response_model=ListResponse[SubscriptionRead])
def my_subscriptions(
    status: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    subscriber_id = _subscriber_id(principal)
    return catalog_service.subscriptions.list_response(
        db, subscriber_id, None, status, order_by, order_dir, limit, offset
    )


@router.post(
    "/subscriptions/{subscription_id}/device/reboot",
    response_model=CustomerDeviceCommandOutcomeRead,
)
def reboot_my_subscription_device(
    subscription_id: UUID,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Reboot the exact device currently assigned to the caller's service."""
    from app.services.customer_device_commands import (
        CustomerDeviceCommandError,
        reboot_subscription_device,
    )

    try:
        return reboot_subscription_device(
            db,
            subscriber_id=UUID(_subscriber_id(principal)),
            subscription_id=subscription_id,
            actor_id=str(principal.get("id") or _subscriber_id(principal)),
        )
    except CustomerDeviceCommandError as exc:
        status_code = 404 if exc.code == "subscription_not_found" else 409
        raise HTTPException(
            status_code=status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc


@router.post(
    "/subscriptions/{subscription_id}/device/wifi",
    response_model=CustomerDeviceCommandOutcomeRead,
)
def update_my_subscription_wifi(
    subscription_id: UUID,
    payload: CustomerWifiUpdateRequest,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Update Wi-Fi on the exact device assigned to the caller's service."""
    from app.services.customer_device_commands import (
        CustomerDeviceCommandError,
        update_subscription_wifi,
    )

    try:
        return update_subscription_wifi(
            db,
            subscriber_id=UUID(_subscriber_id(principal)),
            subscription_id=subscription_id,
            actor_id=str(principal.get("id") or _subscriber_id(principal)),
            ssid=payload.ssid,
            password=payload.password,
        )
    except CustomerDeviceCommandError as exc:
        status_code = 404 if exc.code == "subscription_not_found" else 409
        raise HTTPException(
            status_code=status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc


@router.get("/account-health", response_model=PortalAccountHealthRead)
def my_account_health(
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Canonical financial, access, session, connection, and outage health."""
    from app.services.portal_account_health import build_portal_account_health

    return PortalAccountHealthRead.model_validate(
        build_portal_account_health(db, UUID(_subscriber_id(principal)))
    )


@router.get("/quota-buckets", response_model=ListResponse[QuotaBucketRead])
def my_quota_buckets(
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """All quota buckets across the caller's subscriptions (single round-trip)."""
    subscriber_id = _subscriber_id(principal)
    return usage_service.quota_buckets.list_response_for_subscriber(
        db, subscriber_id, limit, offset
    )


def _offer_summary(offer, summary) -> PlanOfferSummary | None:
    if offer is None:
        return None
    return PlanOfferSummary(
        id=offer.id,
        name=getattr(offer, "name", "") or "Plan",
        amount=float(getattr(summary, "amount", 0) or 0),
        currency=getattr(summary, "currency", "NGN"),
        period_label=getattr(summary, "period_label", "/cycle"),
    )


@router.get(
    "/subscriptions/{subscription_id}/service-change",
    response_model=PlanChangePageResponse,
)
def my_plan_change_options(
    subscription_id: str,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Plans the caller can switch their service to (no upfront proration)."""
    ctx = customer_changes.get_change_plan_page(
        db, _customer(db, principal), subscription_id
    )
    if ctx is None:
        raise HTTPException(status_code=404, detail="Service not found")
    summaries = ctx.get("available_offer_summaries", {})
    available = [
        _offer_summary(offer, summaries.get(str(offer.id)))
        for offer in ctx.get("available_offers", [])
    ]
    return PlanChangePageResponse(
        current_offer=_offer_summary(
            ctx.get("current_offer"), ctx.get("current_offer_summary")
        ),
        available_offers=[o for o in available if o is not None],
        prepaid_funding=ctx.get("prepaid_funding"),
        postpaid_receivables=ctx.get("postpaid_receivables", Decimal("0.00")),
        collection_blocking_balance=ctx.get(
            "collection_blocking_balance", Decimal("0.00")
        ),
        next_billing_date=ctx.get("next_billing_date"),
        billing_message=ctx.get("billing_message"),
        service_addresses=ctx.get("service_addresses", []),
        current_service_address_id=ctx.get("current_service_address_id"),
    )


@router.get("/subscriptions/{subscription_id}/service-change/quote")
def my_plan_change_quote(
    subscription_id: str,
    offer_id: str,
    target_service_address_id: str | None = None,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
) -> dict:
    """Prorated quote for switching this service to a single target offer."""
    quote = customer_changes.get_plan_change_quote(
        db,
        _customer(db, principal),
        subscription_id,
        offer_id,
        target_service_address_id=target_service_address_id,
    )
    if quote is None:
        raise HTTPException(status_code=404, detail="Plan not available")
    return quote


@router.post(
    "/subscriptions/{subscription_id}/service-change",
    response_model=PlanChangeSubmitResponse,
)
def my_plan_change_submit(
    subscription_id: str,
    payload: PlanChangeSubmitRequest,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Apply a plan change for the caller's own service.

    Commercial-only changes apply immediately. Remote reprovisioning and field
    migrations persist a reviewed change intent until the appropriate delivery
    owner records verification. Plan family never decides the branch and this
    endpoint never creates a support ticket.
    """
    customer = _customer(db, principal)
    account_id = require_customer_account_id(db, customer)
    subscription = catalog_service.subscriptions.get(
        db=db, subscription_id=subscription_id
    )
    if not subscription or str(subscription.subscriber_id) != str(account_id):
        raise HTTPException(status_code=404, detail="Service not found")
    try:
        result = customer_changes.confirm_service_change(
            db=db,
            customer=customer,
            subscription_id=subscription_id,
            offer_id=str(payload.offer_id),
            target_service_address_id=(
                str(payload.target_service_address_id)
                if payload.target_service_address_id
                else None
            ),
            notes=payload.notes,
            preview_fingerprint=payload.preview_fingerprint or "",
            field_quote_fingerprint=payload.field_quote_fingerprint,
            preview_effective_at=payload.preview_effective_at,
            idempotency_key=payload.idempotency_key or "",
            confirmation_origin="customer_api",
        )
    except ValueError as exc:
        # Arrears / validation errors → 400 with the clear message.
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not result.get("success", False):
        # Insufficient prepaid funding for the prorated upgrade.
        shortfall = result.get("shortfall")
        raise HTTPException(
            status_code=402,
            detail=(
                f"Insufficient prepaid funding — top up {shortfall} to apply this "
                "upgrade."
                if shortfall is not None
                else "Insufficient prepaid funding to apply this upgrade."
            ),
        )
    delivery_mode = str(result.get("delivery_mode") or "commercial_only")
    status = (
        "applied"
        if result.get("status") in {"applied", "skipped"}
        else f"pending_{delivery_mode}"
    )
    return PlanChangeSubmitResponse(
        success=True,
        status=status,
        message=str(result.get("message") or "Your service change is confirmed."),
        change_request_id=result.get("change_request_id"),
        account_adjustment_id=result.get("account_adjustment_id"),
        credit_note_id=result.get("credit_note_id"),
        ledger_entry_id=result.get("ledger_entry_id"),
    )


@router.get(
    "/subscriptions/{subscription_id}/add-ons",
    response_model=AddonsAvailableResponse,
)
def my_addons(
    subscription_id: str,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Add-ons the caller can buy for this service, plus their active ones."""
    result = customer_addons.list_available_addons(
        db, _customer(db, principal), subscription_id
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Service not found")
    return result


@router.get(
    "/subscriptions/{subscription_id}/add-ons/quote",
    response_model=AddonQuoteResponse,
)
def my_addon_quote(
    subscription_id: str,
    add_on_id: str,
    quantity: int = Query(default=1, ge=1),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Preview an add-on, distinct funding/receivables, and exact debit."""
    try:
        quote = customer_addons.get_addon_quote(
            db, _customer(db, principal), subscription_id, add_on_id, quantity
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if quote is None:
        raise HTTPException(status_code=404, detail="Service not found")
    return quote


@router.post(
    "/subscriptions/{subscription_id}/add-ons",
    response_model=AddonPurchaseResponse,
)
def my_addon_purchase(
    subscription_id: str,
    payload: AddonPurchaseRequest,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Confirm a previewed add-on and its exact account adjustment."""
    try:
        return customer_addons.purchase_addon(
            db,
            _customer(db, principal),
            subscription_id,
            str(payload.add_on_id),
            payload.quantity,
            preview_fingerprint=payload.preview_fingerprint,
            idempotency_key=payload.idempotency_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete(
    "/subscriptions/{subscription_id}/add-ons/{sub_add_on_id}",
    status_code=204,
)
def my_addon_cancel(
    subscription_id: str,
    sub_add_on_id: str,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Cancel one of the caller's add-ons (stops billing from the next cycle)."""
    if not customer_addons.cancel_addon(
        db, _customer(db, principal), subscription_id, sub_add_on_id
    ):
        raise HTTPException(status_code=404, detail="Add-on not found")


@router.get("/topup", response_model=TopupPageResponse)
def my_topup_page(
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Deposit Account Credit context, eligibility, limits, and payment options.

    ``payment_options`` mirrors the web chooser (online gateways + a direct
    bank-transfer option) and ``direct_bank_transfer`` carries the admin bank
    account(s) so the customer can transfer and upload a receipt in-app.
    """
    ctx = customer_payments.get_topup_page(db, _customer(db, principal))
    options = [
        PaymentProviderOption(provider_type=opt["provider_type"], label=opt["label"])
        for opt in ctx.get("payment_options", [])
        if opt.get("provider_type") != "direct_bank_transfer"
    ]
    accounts = [
        BankTransferAccount(
            bank_name=acct["bank_name"],
            account_name=acct["account_name"],
            account_number=acct["account_number"],
            sort_code=acct.get("sort_code") or None,
        )
        for acct in customer_payments.enabled_direct_bank_transfer_accounts(db)
    ]
    transfer_settings = customer_payments.direct_bank_transfer_settings(db)
    direct_transfer = DirectBankTransferConfig(
        enabled=customer_payments.direct_bank_transfer_enabled(db),
        instructions=(
            transfer_settings.get("direct_bank_transfer_instructions") or None
        ),
        accounts=accounts,
    )
    return TopupPageResponse(
        provider_type=ctx["provider_type"],
        provider_public_key=ctx.get("provider_public_key"),
        prepaid_balance=ctx.get("prepaid_balance"),
        account_credit=ctx.get("account_credit"),
        deposit_allowed=ctx.get("deposit_allowed", True),
        eligible_unpaid_total=ctx.get("eligible_unpaid_total", Decimal("0.00")),
        eligible_unpaid_invoices=ctx.get("eligible_unpaid_invoices", []),
        min_amount=ctx["min_amount"],
        max_amount=ctx["max_amount"],
        preset_amounts=ctx.get("preset_amounts", []),
        customer_email=ctx.get("customer_email"),
        payment_options=options,
        direct_bank_transfer=direct_transfer,
    )


@router.post("/topup/preview", response_model=TopupPreviewResponse)
def my_topup_preview(
    payload: TopupPreviewRequest,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Preview a Deposit Account Credit allocation for the caller's amount."""
    customer = _customer(db, principal)
    try:
        preview = customer_payments.preview_topup(db, customer, payload.amount)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return TopupPreviewResponse.model_validate(preview)


@router.post("/topup/initiate", response_model=TopupInitiateResponse)
def my_topup_initiate(
    payload: TopupInitiateRequest,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Create a Deposit Account Credit intent for the caller's account."""
    customer = _customer(db, principal)
    try:
        result = customer_payments.create_topup_intent(
            db,
            customer,
            payload.amount,
            provider=payload.provider,
            payment_method_id=(
                str(payload.payment_method_id) if payload.payment_method_id else None
            ),
            preview_fingerprint=payload.preview_fingerprint,
            idempotency_key=payload.idempotency_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("Customer API saved-card top-up charge failed", exc_info=True)
        raise HTTPException(
            status_code=400,
            detail=PAYMENT_CHARGE_ERROR_MESSAGE,
        ) from exc
    return TopupInitiateResponse(
        intent_id=result["intent_id"],
        provider_type=result["provider_type"],
        provider_public_key=result.get("provider_public_key"),
        payment_reference=result["reference"],
        amount=result["requested_amount"],
        currency=result.get("currency", "NGN"),
        customer_email=customer["username"] or None,
        charged=result.get("charged", False),
        checkout_url=result.get("checkout_url"),
        preview_fingerprint=result["preview_fingerprint"],
    )


@router.post("/topup/verify", response_model=TopupVerifyResponse)
def my_topup_verify(
    payload: TopupVerifyRequest,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Verify and settle a Deposit Account Credit transaction."""
    customer = _customer(db, principal)
    account_id = require_customer_account_id(db, customer)
    try:
        result = customer_payments.verify_and_record_topup(
            db, customer, payload.reference
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    card_saved: bool | None = None
    card_save_message: str | None = None
    if payload.save_card:
        try:
            customer_cards.capture_card_after_payment(
                db, account_id, payload.reference, result["provider_type"]
            )
            card_saved = True
            card_save_message = CARD_SAVE_SUCCESS_MESSAGE
        except Exception:
            logger.warning("Customer API top-up card capture failed", exc_info=True)
            card_saved = False
            card_save_message = CARD_SAVE_ERROR_MESSAGE
    return TopupVerifyResponse(
        reference=payload.reference,
        amount=Decimal(str(result.get("amount") or "0")),
        already_recorded=result.get("already_recorded", False),
        available_balance=result.get("available_balance"),
        amount_credited=result.get("amount_credited"),
        prepaid_amount_applied=result.get("prepaid_amount_applied", Decimal("0.00")),
        credit_added=result.get("credit_added"),
        allocated_total=result.get("allocated_total", Decimal("0.00")),
        allocated_to_invoices=result.get("allocated_to_invoices", []),
        renewed_services=result.get("renewed_services", []),
        card_saved=card_saved,
        card_save_message=card_save_message,
    )


@router.post("/account/deletion-request", response_model=AccountDeletionResponse)
def my_account_deletion_request(
    payload: AccountDeletionRequest | None = None,
    request: Request = None,  # type: ignore[assignment]
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Request deletion of the caller's account (in-app, App Store 5.1.1(v)).

    Records the request and notifies the customer; operations end service and
    delete personal data per the privacy policy (statutory billing/tax records
    are retained where required). The client signs the user out afterwards.
    """
    subscriber_id = _subscriber_id(principal)
    result = account_deletion_service.request_deletion(
        db,
        subscriber_id,
        reason=payload.reason if payload else None,
        request=request,
    )
    return AccountDeletionResponse(
        status=result["status"],
        requested_at=result["requested_at"],
        already_requested=result["already_requested"],
    )


@router.post("/chat/session", response_model=ChatSessionResponse)
def my_chat_session(
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
    ticket_id: str | None = None,
    project_id: str | None = None,
):
    """Open (or resume) a live-chat session with support.

    The sub asserts the authenticated subscriber's identity to the CRM and
    returns an opaque visitor token plus the URLs the client uses to talk to the
    CRM chat widget directly (WebSocket for real-time, REST for send/history).

    Pass ``ticket_id`` or ``project_id`` to start the chat about that record —
    the reference rides in the session so the agent has context.
    """
    subscriber_id = _subscriber_id(principal)
    try:
        return chat_session_service.broker_customer_session(
            db, subscriber_id, ticket_id=ticket_id, project_id=project_id
        )
    except team_inbox_widget.TeamInboxWidgetError as exc:
        status_code = 404 if exc.code.endswith("_not_found") else 503
        raise HTTPException(status_code=status_code, detail=exc.message) from exc


@router.post("/portal/session", response_model=PortalSessionResponse)
def my_portal_session(
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Mint a scoped customer Portal API token (RFC #73).

    The sub asserts the authenticated subscriber's identity to the CRM and
    returns a short-lived, scoped token plus the absolute base URL the client
    uses to call the CRM Portal API directly (e.g. Refer & Earn).
    """
    subscriber_id = _subscriber_id(principal)
    return portal_session_service.broker_customer_portal_session(db, subscriber_id)


@router.get("/referrals", response_model=MyReferralsResponse)
def my_referrals(
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """The caller's native Sub Refer & Earn summary."""
    subscriber_id = _subscriber_id(principal)
    try:
        resolved_subscriber_id = UUID(subscriber_id)
        db_session_adapter.release_read_transaction(db)
        referrals_service.ensure_referral_code(
            db,
            referrals_service.EnsureReferralCodeCommand(
                context=CommandContext.system(
                    actor=f"customer_api:{resolved_subscriber_id}",
                    scope=referrals_service.REFERRAL_PROGRAM_SCOPE,
                    reason="Customer requested their referral summary",
                    idempotency_key=f"referral-code:{resolved_subscriber_id}",
                ),
                subscriber_id=resolved_subscriber_id,
            ),
        )
        return referrals_service.referrals.read_for_subscriber(db, subscriber_id)
    except DomainError as exc:
        status_code = (
            503
            if exc.code.endswith(("program_disabled", "invalid_configuration"))
            else 409
        )
        raise HTTPException(status_code=status_code, detail=exc.message) from exc


@router.get("/projects", response_model=MyProjectsResponse)
def my_projects(
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """The caller's typed native project/task/field/support lifecycle."""
    subscriber_id = _subscriber_id(principal)
    return customer_experience_lifecycle.projects_for_subscriber(db, subscriber_id)


@router.get("/projects/{project_id}", response_model=ProjectItem)
def my_project(
    project_id: UUID,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    project = customer_experience_lifecycle.project_for_subscriber(
        db, _subscriber_id(principal), project_id
    )
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.get("/work-orders", response_model=MyWorkOrdersResponse)
def my_work_orders(
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """The caller's Sub-owned field visits and native lifecycle links."""
    subscriber_id = _subscriber_id(principal)
    return customer_experience_lifecycle.work_orders_for_subscriber(db, subscriber_id)


@router.get("/work-orders/{work_order_id}", response_model=WorkOrderItem)
def my_work_order(
    work_order_id: str,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    work_order = customer_experience_lifecycle.work_order_for_subscriber(
        db, _subscriber_id(principal), work_order_id
    )
    if work_order is None:
        raise HTTPException(status_code=404, detail="Work order not found")
    return work_order


@router.get(
    "/work-orders/{work_order_id}/technician-location",
    response_model=TechnicianLocation,
)
def my_work_order_technician_location(
    work_order_id: str,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Live technician position for an in-progress work order (poll for the
    'where's my technician' map). Returns available=False when the map should
    be hidden."""
    subscriber_id = _subscriber_id(principal)
    return customer_work_order_selfcare.technician_location(
        db, subscriber_id, work_order_id
    )


@router.get(
    "/work-orders/{work_order_id}/chat",
    response_model=CustomerFieldJobChatThread,
)
def my_work_order_chat(
    work_order_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """The chat with the technician on their way to this visit.

    ``available=False`` with ``reason="not_departed"`` until the technician
    sets off — a technician holds several assigned jobs at once, and the chat
    belongs to the one they are actually travelling to.
    """
    subscriber_id = _subscriber_id(principal)
    return customer_field_job_chat.get_thread(
        db, subscriber_id, work_order_id, limit=limit
    )


@router.post(
    "/work-orders/{work_order_id}/chat/messages",
    response_model=CustomerFieldJobChatMessage,
    status_code=status.HTTP_201_CREATED,
)
def send_my_work_order_chat_message(
    work_order_id: str,
    payload: CustomerFieldJobChatMessageCreate,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Send a message to the technician travelling to this visit."""
    subscriber_id = _subscriber_id(principal)
    try:
        return customer_field_job_chat.send_message(
            db, subscriber_id, work_order_id, body=payload.body
        )
    except customer_field_job_chat.FieldJobChatError as exc:
        status_code = 404 if exc.code == "not_found" else 409
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@router.post(
    "/work-orders/{work_order_id}/rate-technician",
    response_model=TechnicianRatingResponse,
)
def my_rate_technician(
    work_order_id: str,
    payload: TechnicianRatingRequest,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Rate the technician after a completed work order (1-5 + optional comment)."""
    subscriber_id = _subscriber_id(principal)
    try:
        return customer_work_order_selfcare.rate_technician(
            db,
            subscriber_id,
            work_order_id,
            rating=payload.rating,
            comment=payload.comment,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Work order not found") from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=409, detail="Work order is not completed"
        ) from exc


@router.get("/quotes", response_model=MyQuotesResponse)
def my_quotes(
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """The caller's self-serve installation quotes — feasibility, estimate,
    deposit, status. Behind the ``quotes_native_read_enabled``
    read-flip flag: OFF serves the local CRM mirror (refreshed lazily),
    ON serves sub's native ``quotes`` table — same shape either way."""
    subscriber_id = _subscriber_id(principal)
    if selfserve_service.native_read_enabled(db):
        return selfserve_service.selfserve_quotes.read_for_subscriber(db, subscriber_id)
    return quotes_mirror.read_for_subscriber(db, subscriber_id)


@router.post("/quote-request", response_model=QuoteItem, status_code=201)
def my_quote_request(
    payload: QuoteRequestCreate,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Request a map-pinned installation quote. The dropped pin drives the
    feasibility check (proximity to fiber) + estimate + deposit. Behind the
    ``quotes_native_write_enabled`` write-flip flag: OFF writes through
    to the CRM and returns the mirrored item; ON creates the quote in sub's
    native ``quotes`` table (no CRM link required, so native-only subscribers
    can quote too) — same payload shape either way."""
    subscriber_id = _subscriber_id(principal)
    if selfserve_service.native_write_enabled(db):
        quote = selfserve_service.selfserve_quotes.request_quote(
            db,
            subscriber_id,
            latitude=payload.latitude,
            longitude=payload.longitude,
            address=payload.address,
            region=payload.region,
            note=payload.note,
        )
        return selfserve_service.build_portal_quote_payload(db, quote)
    return quotes_mirror.request_quote(
        db,
        subscriber_id,
        latitude=payload.latitude,
        longitude=payload.longitude,
        address=payload.address,
        region=payload.region,
        note=payload.note,
    )


@router.post(
    "/quotes/{quote_id}/deposit/initiate", response_model=QuoteDepositInitiateResponse
)
def my_quote_deposit_initiate(
    quote_id: str,
    payload: QuoteDepositInitiateRequest,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Start paying a quote's installation deposit. Raises a deposit invoice and
    returns a checkout intent via the customer's existing pay flow (any provider)."""
    subscriber_id = _subscriber_id(principal)
    customer = _customer(db, principal)
    return quote_deposits.initiate_deposit(
        db,
        customer,
        subscriber_id,
        quote_id,
        provider=payload.provider,
        redirect_url=payload.redirect_url,
    )


@router.post(
    "/quotes/{quote_id}/deposit/verify", response_model=QuoteDepositVerifyResponse
)
def my_quote_deposit_verify(
    quote_id: str,
    payload: QuoteDepositVerifyRequest,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Verify the deposit payment; on full settlement the quote is accepted in the
    CRM (which triggers the sales order + install project)."""
    subscriber_id = _subscriber_id(principal)
    customer = _customer(db, principal)
    return quote_deposits.verify_deposit(
        db,
        customer,
        subscriber_id,
        quote_id,
        reference=payload.reference,
        provider=payload.provider,
    )


@router.post("/referrals", response_model=ReferAFriendResponse, status_code=201)
def my_refer_a_friend(
    payload: ReferAFriendRequest,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Refer a friend through Sub's Party-first referral owner."""
    subscriber_id = _subscriber_id(principal)
    try:
        resolved_subscriber_id = UUID(subscriber_id)
        db_session_adapter.release_read_transaction(db)
        result = referrals_service.refer_friend(
            db,
            referrals_service.ReferFriendCommand(
                context=CommandContext.system(
                    actor=f"customer_api:{resolved_subscriber_id}",
                    scope=referrals_service.REFERRAL_PROGRAM_SCOPE,
                    reason="Customer submitted a Refer & Earn prospect",
                ),
                referrer_subscriber_id=resolved_subscriber_id,
                name=payload.name,
                email=payload.email,
                phone=payload.phone,
                note=payload.note,
            ),
        )
        return {
            "id": str(result.referral_id),
            "status": result.status,
            "message": "Referral submitted",
        }
    except DomainError as exc:
        status_code = {
            "referrals.program.contact_required": 422,
            "referrals.program.program_disabled": 503,
            "referrals.program.invalid_configuration": 503,
            "referrals.program.subscriber_not_found": 404,
        }.get(exc.code, 409)
        raise HTTPException(status_code=status_code, detail=exc.message) from exc


@router.get(
    "/notifications", response_model=ListResponse[CustomerInboxNotificationRead]
)
def my_notifications(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """The subscriber's own notifications (in-app inbox), newest first."""
    subscriber_id = _subscriber_id(principal)
    response = notification_service.notifications.list_response_for_subscriber(
        db, subscriber_id, limit, offset
    )
    customer_notifications_service.apply_notification_read_state(
        db,
        subscriber_id=subscriber_id,
        notifications=response["items"],
    )
    return response


@router.post(
    "/notifications/read",
    response_model=CustomerNotificationReadResponse,
)
def my_notifications_mark_read(
    payload: CustomerNotificationReadRequest,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Mark selected or all visible notifications for the calling subscriber."""
    subscriber_id = _subscriber_id(principal)
    marked = customer_notifications_service.mark_api_notifications_read(
        db,
        subscriber_id=subscriber_id,
        notification_ids=payload.notification_ids,
        all_visible=payload.all_visible,
    )
    return CustomerNotificationReadResponse(marked=marked)


@router.post("/push-tokens", response_model=PushTokenRead, status_code=201)
def my_register_push_token(
    payload: PushTokenRegister,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Register (or refresh) this device's push token for the calling subscriber."""
    subscriber_id = _subscriber_id(principal)
    return push_service.register_token(
        db, subscriber_id, payload.token, payload.platform
    )


@router.delete("/push-tokens/{token}", status_code=204)
def my_unregister_push_token(
    token: str,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Unregister a device's push token (e.g. on logout). Idempotent."""
    subscriber_id = _subscriber_id(principal)
    push_service.unregister_token(db, subscriber_id, token)
    return None


@router.get(
    "/radius-accounting-sessions",
    response_model=ListResponse[RadiusAccountingSessionRead],
)
def my_accounting_sessions(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """RADIUS accounting (data usage) sessions across the caller's own
    subscriptions, newest first."""
    subscriber_id = _subscriber_id(principal)
    return usage_service.radius_accounting_sessions.list_response_for_subscriber(
        db, subscriber_id, limit, offset
    )


@router.get("/usage-summary", response_model=UsageSummaryResponse)
async def my_usage_summary(
    period: str = Query(
        default="today", pattern="^(hour|today|yesterday|week|cycle|all)$"
    ),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Time-windowed data-usage total + bucketed series for the caller.

    period: hour | today | yesterday | week | cycle | all. The total is
    billing-grade for cycle (session octets) and all (daily history plus session
    octets), and throughput-integrated for sub-day windows — see total_source /
    is_authoritative on the response. Zero is a valid authoritative value, not
    a missing-data sentinel. The window has a defined start/end (unlike the
    legacy "last 50 sessions" sum) and counts the live session's current octets.
    """
    subscriber_id = _subscriber_id(principal)
    summary = await usage_summary_service.get_usage_summary(db, subscriber_id, period)
    summary["fup"] = await usage_summary_service.fup_summary(db, subscriber_id)
    return summary


@router.get("/usage-history", response_model=DailyUsageHistoryResponse)
def my_usage_history(
    days: int = Query(default=365, ge=1, le=3660),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Long-history daily upload/download series for the caller.

    Sourced from the historical daily rollup (back to 2018 for migrated
    accounts), which reaches years further than per-session accounting. Default
    window is the last 365 days; raise ``days`` for the full archive.
    """
    subscriber_id = _subscriber_id(principal)
    return usage_summary_service.get_daily_usage_history(db, subscriber_id, days=days)


# --- Live bandwidth (self-scoped, Bearer) --------------------------------------
#
# Bearer-authenticated mirrors of the cookie-only /bandwidth/my/* web routes,
# which the mobile app (Bearer) could never reach (403). Scoped to the caller's
# active subscription; throughput is returned in subscriber perspective
# (download/upload) so the app's live-bandwidth section can read it directly.


@router.get("/bandwidth/stats")
async def my_bandwidth_stats(
    period: str = Query(default="24h", pattern="^(1h|24h|7d|30d)$"),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Current + peak throughput for the caller's active subscription.

    Drives the live-bandwidth section's real-time reading and the Peak figure.
    """
    subscription = bandwidth_samples.get_user_active_subscription(db, principal)
    stats = await bandwidth_samples.get_bandwidth_stats(db, subscription.id, period)
    return with_subscriber_directions(stats)


@router.get("/bandwidth/series")
async def my_bandwidth_series(
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    interval: str = Query(default="auto", pattern="^(auto|1m|5m|1h)$"),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Throughput time series for the caller's active subscription (chart)."""
    subscription = bandwidth_samples.get_user_active_subscription(db, principal)
    result = await bandwidth_samples.get_bandwidth_series(
        db, subscription.id, start_at, end_at, interval
    )
    return add_directions_to_series(result)


# --- Support tickets (self-scoped) ---------------------------------------------
#
# The staff endpoints in app/api/support.py are gated by
# require_permission("support:ticket:*"), which a subscriber token (no scopes)
# can never satisfy — so the customer app got 403 on every Support call. These
# mirror the /me pattern: auth-only, forced to the caller's own subscriber_id,
# with ownership checks (no IDOR) and internal staff notes filtered out.


def _owned_ticket(db: Session, subscriber_id: str, ticket_id: str):
    """Fetch a ticket and 404 unless it belongs to the calling subscriber.

    Returns 404 (not 403) for someone else's ticket so a customer can't probe
    which ticket ids exist.
    """
    ticket = support_service.tickets.get(db, ticket_id)
    if str(ticket.subscriber_id or "") != subscriber_id:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return ticket


@router.get("/support/tickets", response_model=ListResponse[TicketRead])
def my_tickets(
    status: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """The caller's own support tickets, newest first."""
    subscriber_id = _subscriber_id(principal)
    return support_service.tickets.list_response(
        db,
        status=status,
        subscriber_id=subscriber_id,
        order_by=order_by,
        order_dir=order_dir,
        limit=limit,
        offset=offset,
    )


@router.get("/support/tickets/{ticket_id}", response_model=TicketRead)
def my_ticket(
    ticket_id: str,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    return _owned_ticket(db, _subscriber_id(principal), ticket_id)


MAX_TICKET_ATTACHMENTS = 5


def _validate_attachment_count(attachments: list[UploadFile]) -> list[UploadFile]:
    files = [a for a in (attachments or []) if getattr(a, "filename", "")]
    if len(files) > MAX_TICKET_ATTACHMENTS:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "too_many_attachments",
                "message": f"At most {MAX_TICKET_ATTACHMENTS} files may be attached.",
            },
        )
    return files


@router.post("/support/tickets", response_model=TicketRead, status_code=201)
def my_create_ticket(
    request: Request,
    title: str = Form(..., min_length=1, max_length=255),
    description: str | None = Form(default=None),
    priority: str = Form(default="normal"),
    ticket_type: str | None = Form(default=None),
    attachments: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Raise a ticket on the caller's own account. Identity/assignment fields
    are not accepted from the client — subscriber_id is forced to the caller.

    Accepts multipart/form-data: the JSON-equivalent fields (title, description,
    priority, ticket_type) as form fields plus a repeatable ``attachments`` file
    field (images + PDF, <=5 MB each, <=5 files). Attachments are stored locally
    on the ticket and may not sync to the CRM (the CRM push omits them)."""
    payload = MySupportTicketCreate(
        title=title,
        description=description,
        priority=priority,
        ticket_type=ticket_type,
    )
    files = _validate_attachment_count(attachments)
    subscriber_id = _subscriber_id(principal)
    ticket_payload = TicketCreate(
        title=payload.title,
        description=payload.description,
        priority=payload.priority,
        ticket_type=payload.ticket_type,
        channel=TicketChannel.web,
        subscriber_id=UUID(subscriber_id),
    )
    db_session_adapter.release_read_transaction(db)
    ticket = support_service.tickets.create(
        db, ticket_payload, actor_id=subscriber_id, request=request
    )
    if files:
        try:
            uploaded = web_support_tickets.upload_ticket_attachments(
                db,
                ticket_id=str(ticket.id),
                attachments=files,
                entity_type="support_ticket_attachment",
                actor_id=subscriber_id,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_attachment", "message": str(exc)},
            ) from exc
        support_service.tickets.add_attachments(db, str(ticket.id), uploaded)
        db.refresh(ticket)
    return ticket


@router.get(
    "/support/tickets/{ticket_id}/comments",
    response_model=ListResponse[TicketCommentRead],
)
def my_ticket_comments(
    ticket_id: str,
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Replies on the caller's own ticket. Staff-internal notes are stripped."""
    _owned_ticket(db, _subscriber_id(principal), ticket_id)
    items = [
        c
        for c in support_service.ticket_comments.list(
            db, ticket_id, limit=limit, offset=offset
        )
        if not c.is_internal
    ]
    return {"items": items, "count": len(items), "limit": limit, "offset": offset}


@router.post(
    "/support/tickets/{ticket_id}/comments",
    response_model=TicketCommentRead,
    status_code=201,
)
def my_add_ticket_comment(
    ticket_id: str,
    request: Request,
    body: str = Form(..., min_length=1),
    attachments: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Reply to the caller's own ticket. is_internal is forced False so a
    customer can never create a staff-only note.

    Accepts multipart/form-data: the ``body`` form field plus a repeatable
    ``attachments`` file field (images + PDF, <=5 MB each, <=5 files).
    Attachments are stored locally on the comment and may not sync to the CRM
    (the CRM push omits them)."""
    payload = MySupportCommentCreate(body=body)
    files = _validate_attachment_count(attachments)
    subscriber_id = _subscriber_id(principal)
    _owned_ticket(db, subscriber_id, ticket_id)
    uploaded: list[dict] = []
    if files:
        try:
            db_session_adapter.release_read_transaction(db)
            uploaded = web_support_tickets.upload_ticket_attachments(
                db,
                ticket_id=ticket_id,
                attachments=files,
                entity_type="support_ticket_comment_attachment",
                actor_id=subscriber_id,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_attachment", "message": str(exc)},
            ) from exc
    db_session_adapter.release_read_transaction(db)
    comment = support_service.tickets.create_comment(
        db,
        ticket_id,
        TicketCommentCreate(
            body=payload.body,
            is_internal=False,
            attachments=[AttachmentMeta(**item) for item in uploaded],
        ),
        actor_id=subscriber_id,
        request=request,
    )
    return comment


@router.post("/support/tickets/{ticket_id}/rate", response_model=TicketRead)
def my_rate_ticket(
    ticket_id: str,
    payload: TicketSatisfactionRequest,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Rate the support experience on the caller's own resolved/closed ticket
    (CSAT, 1-5 + optional comment). Re-rating overwrites the previous score."""
    ticket = _owned_ticket(db, _subscriber_id(principal), ticket_id)
    db_session_adapter.release_read_transaction(db)
    return support_service.tickets.set_satisfaction(
        db, ticket, rating=payload.rating, comment=payload.comment
    )


@router.post(
    "/support/tickets/{ticket_id}/confirm-resolution", response_model=TicketRead
)
def my_confirm_ticket_resolution(
    ticket_id: str,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Confirm that the owner-resolved incident is fixed."""
    ticket = _owned_ticket(db, _subscriber_id(principal), ticket_id)
    db_session_adapter.release_read_transaction(db)
    return support_service.tickets.respond_to_resolution_for_customer(
        db, ticket, confirm=True
    )


@router.post(
    "/support/tickets/{ticket_id}/dispute-resolution", response_model=TicketRead
)
def my_dispute_ticket_resolution(
    ticket_id: str,
    payload: TicketResolutionDisputeRequest,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Reopen the incident when the reported resolution did not fix it."""
    ticket = _owned_ticket(db, _subscriber_id(principal), ticket_id)
    db_session_adapter.release_read_transaction(db)
    return support_service.tickets.respond_to_resolution_for_customer(
        db,
        ticket,
        confirm=False,
        reason=payload.reason,
    )


# --- Geocoding (self-care helpers) ----------------------------------------------


@router.get("/geocode/reverse")
def my_reverse_geocode(
    lat: float = Query(ge=-90, le=90),
    lon: float = Query(ge=-180, le=180),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Resolve device coordinates to the nearest known address, for the
    opt-in "attach my location" flow. Returns display_name=None when the
    point is unknown or geocoding is disabled."""
    result = geocoding_service.reverse_geocode(db, lat, lon)
    if not result:
        return {"display_name": None}
    return {
        "display_name": result.get("display_name"),
        "latitude": result.get("latitude"),
        "longitude": result.get("longitude"),
    }


# --- Service location (pin validation) -------------------------------------------


@router.get("/location", response_model=MyLocationRead)
def my_location(
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """The caller's service-location pin plus any correction requests."""
    subscriber_id = _subscriber_id(principal)
    context = location_service.get_customer_location_page_context(
        db, {"subscriber_id": subscriber_id}
    )
    return MyLocationRead(
        address_label=context.get("location_address_label"),
        current_latitude=context.get("current_latitude"),
        current_longitude=context.get("current_longitude"),
        can_submit_request=bool(context.get("can_submit_request")),
        has_address_anchor=bool(context.get("has_address_anchor")),
        pending_request=context.get("pending_request"),
        history=context.get("request_history") or [],
    )


@router.post(
    "/location-requests", response_model=MyLocationRequestRead, status_code=201
)
def my_location_request_create(
    payload: MyLocationRequestCreate,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Submit a pin correction for the caller's own service address. One
    pending request at a time; an admin reviews before anything changes."""
    subscriber_id = _subscriber_id(principal)
    return location_service.submit_request(
        db,
        subscriber_id=subscriber_id,
        latitude=payload.latitude,
        longitude=payload.longitude,
        customer_note=payload.note,
        actor_id=subscriber_id,
        actor_name=None,
        submitted_from_ip=request.client.host if request.client else None,
    )


@router.post(
    "/location-requests/{request_id}/cancel", response_model=MyLocationRequestRead
)
def my_location_request_cancel(
    request_id: str,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    subscriber_id = _subscriber_id(principal)
    return location_service.cancel_request(
        db,
        request_id=request_id,
        subscriber_id=subscriber_id,
        actor_id=subscriber_id,
    )


# --- Subscriber contacts (self-scoped) ------------------------------------------
#
# Customer-facing parity with the web portal's /portal/contacts feature. The
# mobile app manages a subscriber's additional contacts (people, not accounts).
# All ops reuse the web service core (app/services/customer_portal_contacts.py)
# so normalization, the "at least one contact channel" rule, duplicate
# detection, and subscriber-id scoping are identical. Ownership is enforced by
# the service (allowed subscriber ids only); a contact that isn't the caller's
# returns 404 (not 403), matching the self-scoped pattern used elsewhere.


def _contact_form(
    payload: SubscriberContactCreate | SubscriberContactUpdate,
) -> contacts_service.ContactForm:
    """Normalize an API payload into the web service's ContactForm."""
    return contacts_service.normalize_contact_form(
        full_name=payload.full_name,
        phone=payload.phone,
        email=payload.email,
        whatsapp=payload.whatsapp,
        facebook=payload.facebook,
        instagram=payload.instagram,
        x_handle=payload.x_handle,
        telegram=payload.telegram,
        linkedin=payload.linkedin,
        other_social=payload.other_social,
        relationship=payload.relationship,
        contact_type=payload.contact_type,
        is_authorized=payload.is_authorized,
        receives_notifications=payload.receives_notifications,
        is_billing_contact=payload.is_billing_contact,
        notes=payload.notes,
    )


@router.get("/contacts", response_model=list[SubscriberContactRead])
def my_contacts(
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """The caller's subscriber contacts, newest first."""
    return contacts_service.list_contacts(db, _customer(db, principal))


@router.post(
    "/contacts", response_model=SubscriberContactWriteResponse, status_code=201
)
def my_create_contact(
    payload: SubscriberContactCreate,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Add a contact to the caller's account. Requires at least one contact
    channel (phone, email, or a social handle). Returns the saved contact plus
    any duplicate-use warnings (advisory; the save still succeeds)."""
    try:
        contact, warnings = contacts_service.create_contact_returning(
            db, _customer(db, principal), _contact_form(payload)
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return SubscriberContactWriteResponse(
        contact=SubscriberContactRead.model_validate(contact), warnings=warnings
    )


@router.patch("/contacts/{contact_id}", response_model=SubscriberContactWriteResponse)
def my_update_contact(
    contact_id: str,
    payload: SubscriberContactUpdate,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Update one of the caller's own contacts (full replace). Requires at least
    one contact channel. 404 if the contact isn't the caller's."""
    customer = _customer(db, principal)
    if contacts_service.get_owned_contact(db, customer, contact_id) is None:
        raise HTTPException(status_code=404, detail="Contact not found")
    try:
        contact, warnings = contacts_service.update_contact_returning(
            db, customer, contact_id, _contact_form(payload)
        )
    except ValueError as exc:
        message = str(exc)
        if message == "Contact not found.":
            raise HTTPException(status_code=404, detail="Contact not found") from exc
        raise HTTPException(status_code=400, detail=message) from exc
    return SubscriberContactWriteResponse(
        contact=SubscriberContactRead.model_validate(contact), warnings=warnings
    )


@router.delete("/contacts/{contact_id}", status_code=204)
def my_delete_contact(
    contact_id: str,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Delete one of the caller's own contacts. 404 if it isn't the caller's."""
    customer = _customer(db, principal)
    try:
        contacts_service.delete_contact(db, customer, contact_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Contact not found") from exc
