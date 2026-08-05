"""Architecture guards for Quote document, delivery, and activity ownership."""

from pathlib import Path

from app.services.sot_registry.registry import dependencies_for, service_relationship

ROOT = Path(__file__).resolve().parents[2]


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_quote_document_and_delivery_are_registered_owner_commands():
    documents = _source("app/services/sales/quote_documents.py")
    delivery = _source("app/services/sales/quote_delivery.py")

    assert 'owner="sales.quote_documents"' in documents
    assert "execute_owner_command(" in documents
    assert "owner_command_active(" in documents
    assert "EventType.quote_pdf_exported" in documents
    assert "db.commit(" not in documents
    assert "db.rollback(" not in documents
    assert 'owner="sales.quote_delivery"' in delivery
    assert "execute_owner_command(" in delivery
    assert "CommunicationIntent(" in delivery
    assert "EventType.quote_delivery_requested" in delivery
    assert "db.commit(" not in delivery
    assert "db.rollback(" not in delivery
    assert service_relationship("sales.quote_documents").module == (
        "app.services.sales.quote_documents"
    )
    assert service_relationship("sales.quote_delivery").module == (
        "app.services.sales.quote_delivery"
    )
    assert service_relationship("sales.quote_payment_eligibility").module == (
        "app.services.quote_deposits"
    )
    assert "sales.quote_payment_eligibility" in dependencies_for("sales.quote_delivery")
    assert service_relationship("ui.quote_detail_projection").module == (
        "app.services.web_sales"
    )


def test_quote_email_attachment_resolves_only_the_owned_pdf_artifact():
    attachments = _source("app/services/communication_attachments.py")
    delivery = _source("app/services/sales/quote_delivery.py")

    assert "CommunicationAttachmentKind.quote_pdf" in attachments
    assert "quote_documents.stream_export" in attachments
    assert "expected_quote_id" in attachments
    assert "export.quote_id" in attachments
    assert "quote_attachment_scope_mismatch" in attachments
    assert "resolve_quote_recipient" in delivery
    assert "PartyContactPoint" not in delivery
    assert "send_email(" not in delivery
    assert "smtp" not in delivery.lower()


def test_quote_detail_actions_are_csrf_and_permission_gated():
    route = _source("app/web/admin/sales.py")
    template = _source("templates/admin/sales/quotes/detail.html")

    assert '"/quotes/{quote_id}/pdf"' in route
    assert '"/quotes/{quote_id}/send-email"' in route
    assert 'require_permission("crm:quote:send")' in route
    assert 'can(request, "crm:quote:send")' in template
    assert template.count('action="/admin/sales/quotes/{{ quote.id }}/pdf"') == 1
    assert template.count("components/forms/csrf_input.html") >= 4
    assert "Recent Activity" in template
    assert "timeline_item(" in template


def test_quote_activity_does_not_overstate_mailbox_delivery():
    activity = _source("app/services/sales/quote_activity.py")

    assert "accepted by the configured mail transport" in activity
    assert "mailbox" not in activity.lower()


def test_quote_payment_details_keep_their_authoritative_owners():
    documents = _source("app/services/sales/quote_documents.py")
    accounts = _source("app/services/billing/collection_accounts.py")
    models = _source("app/models/sales.py")
    billing_models = _source("app/models/billing.py")
    deposits = _source("app/services/quote_deposits.py")
    routes = _source("app/web/customer/quotes.py")
    template = _source("templates/customer/quotes/pay.html")

    assert "class QuoteDocumentSnapshot" in documents
    assert "class QuotePaymentSnapshot" in documents
    assert "_validate_quote_payment_url" in documents
    assert 'company.scheme != "https"' in documents
    assert "primary_presentment_account_projection" in documents
    assert "CollectionAccountPresentment" in accounts
    assert "class QuoteDepositInvoiceLink" in models
    assert "invoice_id: Mapped" in billing_models
    assert "QuoteDepositInvoiceLink.quote_id" in deposits
    assert "TopupIntent.invoice_id" in deposits
    assert "1016946461" not in documents
    assert "Zenith Bank" not in documents
    assert "Dotmac Technologies Ltd." not in documents
    assert '"/quotes/{quote_id}/pay"' in routes
    assert '"/quotes/{quote_id}/pay/intent"' in routes
    assert "quote_payment_page(" in routes
    assert "initiate_quote_deposit(" in routes
    assert "verify_quote_deposit(" in routes
    assert ".with_for_update()" in _source("app/services/quote_deposits.py")
    assert "Number(intent.amount)" in template
    assert "body: { idempotency_key: key }" in template
    assert (
        "amount:"
        not in template.split("body: { idempotency_key: key }")[0].split(
            "submitPaymentIntent({"
        )[-1]
    )


def test_quote_email_reuses_typed_payment_eligibility_and_pdf_url():
    delivery = _source("app/services/sales/quote_delivery.py")

    assert "quote_deposits.QuotePaymentQuery(" in delivery
    assert "quote_deposits.quote_payment_page(" in delivery
    assert "snapshot.payment.paystack_url" in delivery
    assert 'f"/portal/quotes/{' not in delivery
    assert "render_quote_email(" in delivery
    assert '"quote_payment_url": content.payment_url' in delivery
    assert "CommunicationAttachmentKind.quote_pdf" in delivery


def test_quote_payment_get_is_not_a_financial_writer():
    routes = _source("app/web/customer/quotes.py")
    get_start = routes.index("def customer_quote_payment(")
    post_start = routes.index("def customer_quote_payment_intent(")
    get_body = routes[get_start:post_start]

    assert "quote_payment_page(" in get_body
    assert "initiate_quote_deposit(" not in get_body
    assert "verify_quote_deposit(" not in get_body
    assert "db.commit(" not in get_body
    assert "db.flush(" not in get_body
    assert "Invoice(" not in get_body
