"""Reseller portal routes."""

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_reseller_billing as web_reseller_billing_service
from app.services import web_reseller_contacts as web_reseller_contacts_service
from app.services import web_reseller_routes as web_reseller_routes_service


def _reseller_auth_guard(request: Request, db: Session = Depends(get_db)):
    # Thin wrapper so the guard resolves at request time, avoiding the
    # import cycle that referencing the service attribute at module load
    # (in dependencies=) would trigger via the shared branding/app.web chain.
    return web_reseller_routes_service.require_reseller_context(request, db)


router = APIRouter(
    prefix="/reseller",
    tags=["web-reseller"],
    dependencies=[Depends(_reseller_auth_guard)],
)


@router.get("", response_class=HTMLResponse)
def reseller_home(request: Request, db: Session = Depends(get_db)):
    return web_reseller_routes_service.reseller_home(request, db)


@router.get("/dashboard", response_class=HTMLResponse)
def reseller_dashboard(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(10, ge=5, le=50),
    db: Session = Depends(get_db),
):
    return web_reseller_routes_service.reseller_dashboard(request, db, page, per_page)


@router.get("/work-orders", response_class=HTMLResponse)
def reseller_work_orders(request: Request, db: Session = Depends(get_db)):
    return web_reseller_routes_service.reseller_work_orders_page(request, db)


@router.get("/work-orders/{account_id}/{work_order_id}/technician-location")
def reseller_work_order_technician_location(
    account_id: str,
    work_order_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    return web_reseller_routes_service.reseller_technician_location(
        request, db, account_id, work_order_id
    )


@router.post("/work-orders/{account_id}/{work_order_id}/rate-technician")
def reseller_rate_technician_route(
    account_id: str,
    work_order_id: str,
    request: Request,
    rating: int = Form(...),
    comment: str = Form(""),
    db: Session = Depends(get_db),
):
    return web_reseller_routes_service.reseller_rate_technician(
        request, db, account_id, work_order_id, rating, comment
    )


@router.get("/quotes", response_class=HTMLResponse)
def reseller_quotes(request: Request, db: Session = Depends(get_db)):
    return web_reseller_routes_service.reseller_quotes_page(request, db)


@router.get("/service-requests", response_class=HTMLResponse)
def reseller_service_requests(request: Request, db: Session = Depends(get_db)):
    return web_reseller_routes_service.reseller_service_requests_page(request, db)


@router.post("/service-requests", response_class=HTMLResponse)
def reseller_service_request_create(
    request: Request,
    contact_name: str = Form(""),
    contact_phone: str = Form(""),
    contact_email: str = Form(""),
    address: str = Form(""),
    latitude: str = Form(""),
    longitude: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    return web_reseller_routes_service.reseller_service_request_create(
        request,
        db,
        contact_name=contact_name,
        contact_phone=contact_phone,
        contact_email=contact_email,
        address=address,
        latitude=latitude,
        longitude=longitude,
        notes=notes,
    )


@router.get("/accounts", response_class=HTMLResponse)
def reseller_accounts(
    request: Request,
    page: int = Query(1),
    per_page: int = Query(20),
    search: str = Query(""),
    status_filter: str = Query(""),
    sort_by: str | None = Query(None, alias="sort"),
    sort_dir: str | None = Query(None, alias="dir"),
    db: Session = Depends(get_db),
):
    return web_reseller_routes_service.reseller_accounts(
        request,
        db,
        page,
        per_page,
        search=search or None,
        status_filter=status_filter or None,
        sort_by=sort_by,
        sort_dir=sort_dir,
    )


@router.get("/accounts/{account_id}", response_class=HTMLResponse)
def reseller_account_detail(
    request: Request,
    account_id: str,
    db: Session = Depends(get_db),
):
    return web_reseller_routes_service.reseller_account_detail(request, db, account_id)


@router.get(
    "/accounts/{account_id}/subscriptions/{subscription_id}/service-change",
    response_class=HTMLResponse,
)
def reseller_service_change_page(
    request: Request,
    account_id: str,
    subscription_id: str,
    db: Session = Depends(get_db),
):
    return web_reseller_routes_service.reseller_service_change_page(
        request, db, account_id, subscription_id
    )


@router.get(
    "/accounts/{account_id}/subscriptions/{subscription_id}/service-change/quote"
)
def reseller_service_change_quote(
    request: Request,
    account_id: str,
    subscription_id: str,
    offer_id: str,
    target_service_address_id: str | None = None,
    db: Session = Depends(get_db),
):
    return web_reseller_routes_service.reseller_service_change_quote(
        request,
        db,
        account_id,
        subscription_id,
        offer_id,
        target_service_address_id,
    )


@router.post("/accounts/{account_id}/subscriptions/{subscription_id}/service-change")
def reseller_service_change_confirm(
    request: Request,
    account_id: str,
    subscription_id: str,
    offer_id: str = Form(...),
    target_service_address_id: str | None = Form(None),
    preview_fingerprint: str = Form(...),
    field_quote_fingerprint: str | None = Form(None),
    preview_effective_at: str = Form(...),
    idempotency_key: str = Form(...),
    notes: str | None = Form(None),
    db: Session = Depends(get_db),
):
    return web_reseller_routes_service.reseller_service_change_confirm(
        request,
        db,
        account_id,
        subscription_id,
        offer_id=offer_id,
        target_service_address_id=target_service_address_id,
        preview_fingerprint=preview_fingerprint,
        field_quote_fingerprint=field_quote_fingerprint,
        preview_effective_at=preview_effective_at,
        idempotency_key=idempotency_key,
        notes=notes,
    )


@router.get("/accounts/{account_id}/invoices", response_class=HTMLResponse)
def reseller_account_invoices(
    request: Request,
    account_id: str,
    page: int = Query(1),
    per_page: int = Query(25),
    sort_by: str | None = Query(None, alias="sort"),
    sort_dir: str | None = Query(None, alias="dir"),
    db: Session = Depends(get_db),
):
    return web_reseller_routes_service.reseller_account_invoices(
        request, db, account_id, page, per_page, sort_by=sort_by, sort_dir=sort_dir
    )


@router.post("/accounts/{account_id}/status", response_class=HTMLResponse)
def reseller_account_status_update(
    request: Request,
    account_id: str,
    action: str = Form(...),
    preview_fingerprint: str = Form(...),
    db: Session = Depends(get_db),
):
    return web_reseller_routes_service.reseller_account_status_update(
        request, db, account_id, action, preview_fingerprint
    )


@router.post("/accounts/{account_id}/status/confirm", response_class=HTMLResponse)
def reseller_account_status_confirm(
    request: Request,
    account_id: str,
    action: str = Form(...),
    preview_fingerprint: str = Form(...),
    idempotency_key: str = Form(...),
    db: Session = Depends(get_db),
):
    return web_reseller_routes_service.reseller_account_status_confirm(
        request,
        db,
        account_id,
        action,
        preview_fingerprint,
        idempotency_key,
    )


@router.get("/accounts/{account_id}/invoices/{invoice_id}", response_class=HTMLResponse)
def reseller_invoice_detail(
    request: Request,
    account_id: str,
    invoice_id: str,
    db: Session = Depends(get_db),
):
    return web_reseller_routes_service.reseller_invoice_detail(
        request, db, account_id, invoice_id
    )


@router.post("/accounts/{account_id}/view", response_class=HTMLResponse)
def reseller_account_view(
    request: Request,
    account_id: str,
    db: Session = Depends(get_db),
):
    return web_reseller_routes_service.reseller_account_view(request, db, account_id)


@router.get("/reports/revenue", response_class=HTMLResponse)
def reseller_revenue_report(request: Request, db: Session = Depends(get_db)):
    return web_reseller_routes_service.reseller_revenue_report(request, db)


@router.get("/profile", response_class=HTMLResponse)
def reseller_profile(request: Request, db: Session = Depends(get_db)):
    return web_reseller_routes_service.reseller_profile(request, db)


@router.post("/profile/sessions/sign-out-others")
def reseller_profile_sign_out_other_sessions(
    request: Request, db: Session = Depends(get_db)
):
    return web_reseller_routes_service.reseller_profile_sign_out_other_sessions(
        request, db
    )


@router.post("/profile", response_class=HTMLResponse)
def reseller_profile_update(
    request: Request,
    contact_email: str = Form(""),
    contact_phone: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    return web_reseller_routes_service.reseller_profile_update(
        request,
        db,
        contact_email=contact_email or None,
        contact_phone=contact_phone or None,
        notes=notes or None,
    )


@router.post("/profile/verify-email/resend")
def reseller_resend_email_verification(request: Request, db: Session = Depends(get_db)):
    return web_reseller_routes_service.reseller_resend_email_verification(request, db)


@router.post("/profile/mfa/setup", response_class=HTMLResponse)
def reseller_mfa_setup(request: Request, db: Session = Depends(get_db)):
    return web_reseller_routes_service.reseller_mfa_setup(request, db)


@router.get("/profile/mfa/setup", response_class=HTMLResponse)
def reseller_mfa_setup_page(request: Request, db: Session = Depends(get_db)):
    return web_reseller_routes_service.reseller_mfa_setup(request, db)


@router.post("/profile/mfa/confirm", response_class=HTMLResponse)
def reseller_mfa_confirm(
    request: Request,
    method_id: str = Form(...),
    code: str = Form(...),
    db: Session = Depends(get_db),
):
    return web_reseller_routes_service.reseller_mfa_confirm(
        request, db, method_id, code
    )


@router.get("/accounts/{account_id}/tickets", response_class=HTMLResponse)
def reseller_account_tickets(
    request: Request,
    account_id: str,
    db: Session = Depends(get_db),
):
    return web_reseller_routes_service.reseller_account_tickets(request, db, account_id)


@router.get("/fiber-map", response_class=HTMLResponse)
def reseller_fiber_map(request: Request, db: Session = Depends(get_db)):
    return web_reseller_routes_service.reseller_fiber_map(request, db)


@router.get("/billing", response_class=HTMLResponse)
def reseller_billing(
    request: Request,
    allocated: str | None = Query(None),
    error: str | None = Query(None),
    subscriber_search: str = Query(""),
    db: Session = Depends(get_db),
):
    return web_reseller_billing_service.billing_overview(
        request,
        db,
        allocated=allocated,
        error=error,
        subscriber_search=subscriber_search or None,
    )


@router.post(
    "/billing/subscribers/{subscriber_id}/allocate/preview",
    response_class=HTMLResponse,
)
def reseller_billing_allocate_subscriber_preview(
    request: Request,
    subscriber_id: str,
    amount: str = Form(...),
    db: Session = Depends(get_db),
):
    return web_reseller_billing_service.preview_subscriber_funds_allocation(
        request, db, subscriber_id, amount
    )


@router.post(
    "/billing/subscribers/{subscriber_id}/allocate/confirm",
    response_class=HTMLResponse,
)
def reseller_billing_allocate_subscriber_confirm(
    request: Request,
    subscriber_id: str,
    amount: str = Form(...),
    preview_fingerprint: str = Form(...),
    idempotency_key: str = Form(...),
    db: Session = Depends(get_db),
):
    return web_reseller_billing_service.confirm_subscriber_funds_allocation(
        request,
        db,
        subscriber_id,
        amount,
        preview_fingerprint,
        idempotency_key,
    )


@router.post("/billing/pay/intent", response_class=HTMLResponse)
def reseller_billing_pay_intent(
    request: Request,
    amount: str = Form(...),
    provider: str = Form(""),
    payment_method_id: str = Form(""),
    save_card: bool = Form(False),
    db: Session = Depends(get_db),
):
    return web_reseller_billing_service.billing_pay_intent(
        request,
        db,
        amount,
        provider=provider or None,
        payment_method_id=payment_method_id or None,
        save_card=save_card,
    )


@router.post("/billing/pay/transfer", response_class=HTMLResponse)
async def reseller_billing_pay_transfer(
    request: Request,
    amount: str = Form(...),
    bank_name: str = Form(""),
    reference: str = Form(""),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Reseller bulk bank-transfer receipt for unallocated credit."""
    return await web_reseller_billing_service.billing_submit_transfer_proof(
        request,
        db,
        file=file,
        amount=amount,
        bank_name=bank_name or None,
        reference=reference or None,
    )


@router.get("/billing/pay/verify", response_class=HTMLResponse)
def reseller_billing_pay_verify(
    request: Request,
    reference: str = Query(...),
    provider: str | None = Query(None),
    db: Session = Depends(get_db),
):
    return web_reseller_billing_service.billing_pay_verify(
        request, db, reference, provider=provider
    )


@router.get("/billing/payment-methods", response_class=HTMLResponse)
def reseller_payment_methods(
    request: Request,
    saved: str | None = Query(None),
    error: str | None = Query(None),
    db: Session = Depends(get_db),
):
    return web_reseller_billing_service.payment_methods(
        request, db, saved=saved, error=error
    )


@router.post(
    "/billing/payment-methods/{method_id}/default", response_class=HTMLResponse
)
def reseller_payment_method_default(
    request: Request,
    method_id: str,
    db: Session = Depends(get_db),
):
    return web_reseller_billing_service.payment_method_set_default(
        request, db, method_id
    )


@router.post("/billing/payment-methods/{method_id}/remove", response_class=HTMLResponse)
def reseller_payment_method_remove(
    request: Request,
    method_id: str,
    db: Session = Depends(get_db),
):
    return web_reseller_billing_service.payment_method_remove(request, db, method_id)


@router.get("/contacts", response_class=HTMLResponse)
def reseller_contacts(request: Request, db: Session = Depends(get_db)):
    return web_reseller_contacts_service.reseller_contacts(request, db)


@router.post("/contacts", response_class=HTMLResponse)
def reseller_contacts_create(
    request: Request,
    full_name: str | None = Form(None),
    phone: str | None = Form(None),
    email: str | None = Form(None),
    whatsapp: str | None = Form(None),
    facebook: str | None = Form(None),
    instagram: str | None = Form(None),
    x_handle: str | None = Form(None),
    telegram: str | None = Form(None),
    linkedin: str | None = Form(None),
    other_social: str | None = Form(None),
    relationship: str | None = Form(None),
    contact_type: str | None = Form("general"),
    is_authorized: bool = Form(False),
    receives_notifications: bool = Form(False),
    is_billing_contact: bool = Form(False),
    notes: str | None = Form(None),
    db: Session = Depends(get_db),
):
    return web_reseller_contacts_service.reseller_contacts_create(
        request,
        db,
        full_name=full_name,
        phone=phone,
        email=email,
        whatsapp=whatsapp,
        facebook=facebook,
        instagram=instagram,
        x_handle=x_handle,
        telegram=telegram,
        linkedin=linkedin,
        other_social=other_social,
        relationship=relationship,
        contact_type=contact_type,
        is_authorized=is_authorized,
        receives_notifications=receives_notifications,
        is_billing_contact=is_billing_contact,
        notes=notes,
    )


@router.post("/contacts/{contact_id}", response_class=HTMLResponse)
def reseller_contacts_update(
    request: Request,
    contact_id: str,
    intent: str | None = Form(None),
    full_name: str | None = Form(None),
    phone: str | None = Form(None),
    email: str | None = Form(None),
    whatsapp: str | None = Form(None),
    facebook: str | None = Form(None),
    instagram: str | None = Form(None),
    x_handle: str | None = Form(None),
    telegram: str | None = Form(None),
    linkedin: str | None = Form(None),
    other_social: str | None = Form(None),
    relationship: str | None = Form(None),
    contact_type: str | None = Form("general"),
    is_authorized: bool = Form(False),
    receives_notifications: bool = Form(False),
    is_billing_contact: bool = Form(False),
    notes: str | None = Form(None),
    db: Session = Depends(get_db),
):
    return web_reseller_contacts_service.reseller_contacts_update(
        request,
        db,
        contact_id,
        intent,
        full_name=full_name,
        phone=phone,
        email=email,
        whatsapp=whatsapp,
        facebook=facebook,
        instagram=instagram,
        x_handle=x_handle,
        telegram=telegram,
        linkedin=linkedin,
        other_social=other_social,
        relationship=relationship,
        contact_type=contact_type,
        is_authorized=is_authorized,
        receives_notifications=receives_notifications,
        is_billing_contact=is_billing_contact,
        notes=notes,
    )


@router.post("/contacts/{contact_id}/delete", response_class=HTMLResponse)
def reseller_contacts_delete(
    request: Request,
    contact_id: str,
    db: Session = Depends(get_db),
):
    return web_reseller_contacts_service.reseller_contacts_update(
        request, db, contact_id, "delete"
    )
