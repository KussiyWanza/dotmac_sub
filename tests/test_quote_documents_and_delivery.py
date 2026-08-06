"""Focused behavior tests for branded Quote documents and delivery."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import pytest

from app.models.audit import AuditEvent
from app.models.billing import CollectionAccount, CollectionAccountType
from app.models.notification import (
    CommunicationIntentRecord,
    Notification,
    NotificationChannel,
    NotificationStatus,
)
from app.models.party import (
    Party,
    PartyContactPoint,
    PartyContactPointType,
    PartyIdentityStatus,
    PartyType,
)
from app.models.sales import (
    Lead,
    Quote,
    QuoteDeliveryRequest,
    QuoteDeliveryRequestStatus,
    QuoteLineItem,
    QuotePdfExport,
    QuoteStatus,
)
from app.models.stored_file import StoredFile
from app.services import document_delivery, quote_deposits
from app.services.billing.collection_accounts import CollectionAccounts
from app.services.brand_profiles import ResolvedBrand
from app.services.brand_theme import contrast_ratio
from app.services.communication_intents import CommunicationIntentResult
from app.services.email_template import html_to_text
from app.services.owner_commands import CommandContext
from app.services.sales import quote_activity, quote_delivery, quote_documents


def _brand() -> ResolvedBrand:
    return ResolvedBrand(
        name="Dotmac Fiber",
        product_name="Dotmac Fiber",
        legal_name="Dotmac Technologies Ltd",
        tagline="Reliable connectivity",
        primary_color="#008000",
        secondary_color="#FF0000",
        semantic_colors={},
        logo_url="",
        dark_logo_url="",
        favicon_url="",
        support_email="support@example.com",
        support_phone="+2348000000000",
        from_email="sales@example.com",
        from_name="Dotmac Sales",
        app_url="https://selfcare.example.com",
        portal_domain="selfcare.example.com",
        legal_address={"city": "Abuja", "country": "Nigeria"},
        source_scope="platform",
        source_scope_id=None,
    )


def _quote(
    db_session, subscriber, *, with_transfer_account: bool = True
) -> tuple[Quote, PartyContactPoint, UUID]:
    party = Party(
        party_type=PartyType.person.value,
        display_name="Amina Bello",
        status=PartyIdentityStatus.active.value,
    )
    db_session.add(party)
    db_session.flush()
    secondary = PartyContactPoint(
        party_id=party.id,
        channel_type=PartyContactPointType.email.value,
        normalized_value="other@example.com",
        is_primary=False,
        is_active=True,
    )
    primary = PartyContactPoint(
        party_id=party.id,
        channel_type=PartyContactPointType.email.value,
        normalized_value="amina@example.com",
        is_primary=True,
        is_active=True,
    )
    lead = Lead(
        party_id=party.id,
        party_bound_at=datetime.now(UTC),
        party_binding_source="pytest",
        party_binding_reason="Quote delivery test identity",
        title="Fiber installation",
        is_active=True,
    )
    db_session.add_all([secondary, primary, lead])
    db_session.flush()
    quote = Quote(
        subscriber_id=subscriber.id,
        lead_id=lead.id,
        status=QuoteStatus.draft.value,
        project_type="fiber_optics_installation",
        currency="NGN",
        subtotal=Decimal("100000.00"),
        tax_total=Decimal("7500.00"),
        total=Decimal("107500.00"),
        is_active=True,
    )
    db_session.add(quote)
    db_session.flush()
    db_session.add(
        QuoteLineItem(
            quote_id=quote.id,
            description="Business fiber installation",
            quantity=Decimal("1.000"),
            unit_price=Decimal("100000.00"),
            discount_percent=Decimal("0.00"),
            amount=Decimal("100000.00"),
        )
    )
    if with_transfer_account:
        db_session.add(
            CollectionAccount(
                name="Primary quotation transfer account",
                account_type=CollectionAccountType.bank,
                bank_name="Zenith Bank",
                account_name="Dotmac Technologies Ltd.",
                account_number="1016946461",
                account_last4="6461",
                sort_code="057150013",
                currency="NGN",
                presentment_priority=100,
                is_active=True,
            )
        )
    quote_id = quote.id
    db_session.commit()
    return quote, primary, quote_id


def _context(*, key: str | None = None) -> CommandContext:
    return CommandContext.system(
        actor=str(uuid4()),
        scope="sales:quote-delivery-test",
        reason="Focused Quote delivery behavior test",
        idempotency_key=key,
    )


def _stub_pdf_storage(monkeypatch) -> None:
    monkeypatch.setattr(
        quote_documents,
        "resolve_brand",
        lambda *_args, **_kwargs: _brand(),
    )
    monkeypatch.setattr(quote_documents, "_render_pdf", lambda *_args: b"%PDF-test")

    def stage_upload(**kwargs):
        record = StoredFile(
            owner_subscriber_id=kwargs["owner_subscriber_id"],
            entity_type=kwargs["entity_type"],
            entity_id=kwargs["entity_id"],
            original_filename=kwargs["original_filename"],
            storage_key_or_relative_path=f"pytest/{kwargs['entity_id']}.pdf",
            file_size=len(kwargs["data"]),
            content_type="application/pdf",
            checksum="a" * 64,
            storage_provider="s3",
        )
        kwargs["db"].add(record)
        kwargs["db"].flush()
        return record

    monkeypatch.setattr(quote_documents.file_uploads, "stage_upload", stage_upload)
    monkeypatch.setattr(quote_documents, "emit_event", lambda *_args, **_kwargs: None)


def _stub_payment_eligibility(monkeypatch, quote: Quote):
    observed: list[quote_deposits.QuotePaymentQuery] = []

    def resolve(_db, query: quote_deposits.QuotePaymentQuery):
        observed.append(query)
        assert quote.subscriber_id is not None
        return quote_deposits.QuotePaymentPage(
            quote_id=quote.id,
            subscriber_id=quote.subscriber_id,
            status=QuoteStatus(quote.status),
            currency=quote.currency,
            payable_amount=Decimal("53750.00"),
            expires_at=quote.expires_at,
            provider_type="paystack",
        )

    monkeypatch.setattr(quote_deposits, "quote_payment_page", resolve)
    return observed


def test_recipient_uses_primary_active_party_email(db_session, subscriber):
    quote, primary, _quote_id = _quote(db_session, subscriber)
    recipient = quote_documents.resolve_quote_recipient(db_session, quote)

    assert recipient is not None
    assert recipient.contact_point_id == primary.id
    assert recipient.email == "amina@example.com"
    assert recipient.display_name == "Amina Bello"


def test_pdf_export_is_content_addressed_and_audited_once(
    db_session, subscriber, monkeypatch
):
    _quote_record, _primary, quote_id = _quote(db_session, subscriber)
    _stub_pdf_storage(monkeypatch)

    first = quote_documents.generate_quote_pdf(
        db_session,
        quote_documents.GenerateQuotePdfCommand(
            context=_context(),
            quote_id=quote_id,
        ),
    )
    replay = quote_documents.generate_quote_pdf(
        db_session,
        quote_documents.GenerateQuotePdfCommand(
            context=_context(),
            quote_id=quote_id,
        ),
    )

    assert replay.export_id == first.export_id
    assert replay.snapshot_fingerprint == first.snapshot_fingerprint
    assert replay.replayed is True
    export = db_session.get(QuotePdfExport, first.export_id)
    assert export.snapshot["brand"]["legal_name"] == "Dotmac Technologies Ltd"
    assert export.snapshot["customer_name"] == "Amina Bello"
    assert export.snapshot["payment"] == {
        "bank_transfer": {
            "collection_account_id": str(db_session.query(CollectionAccount).one().id),
            "bank_name": "Zenith Bank",
            "account_number": "1016946461",
            "account_name": "Dotmac Technologies Ltd.",
        },
        "paystack_url": f"https://selfcare.example.com/portal/quotes/{quote_id}/pay",
    }
    assert (
        db_session.query(AuditEvent)
        .filter_by(
            action="quote.pdf_exported",
            entity_type="quote",
            entity_id=str(quote_id),
        )
        .count()
        == 1
    )


def test_send_email_queues_one_pdf_intent_and_replays(
    db_session, subscriber, monkeypatch
):
    quote, primary, quote_id = _quote(db_session, subscriber)
    _stub_pdf_storage(monkeypatch)
    payment_queries = _stub_payment_eligibility(monkeypatch, quote)
    captured = []

    def submit(db, intent):
        captured.append(intent)
        record = CommunicationIntentRecord(
            subscriber_id=None,
            event_type=intent.event_type,
            category=intent.category,
            communication_class=intent.communication_class.value,
            template_code=intent.template_code,
            subject=intent.subject,
            body=intent.body,
            channels=[NotificationChannel.email.value],
            include_reseller=False,
            status="expanded",
            suppression_reasons=[],
            dedupe_key=intent.dedupe_key,
            metadata_=dict(intent.metadata),
        )
        db.add(record)
        db.flush()
        notification = Notification(
            communication_intent_id=record.id,
            audience_type="subscriber",
            channel=NotificationChannel.email,
            event_type=intent.event_type,
            category=intent.category,
            recipient=intent.recipients[NotificationChannel.email],
            subject=intent.subject,
            body=intent.body,
            metadata_=dict(intent.metadata),
            status=NotificationStatus.queued,
        )
        db.add(notification)
        db.flush()
        return CommunicationIntentResult(
            intent_id=record.id,
            deliveries=(notification,),
            queued=(notification,),
            suppressed=(),
        )

    monkeypatch.setattr(document_delivery, "submit", submit)
    monkeypatch.setattr(
        document_delivery,
        "emit_event",
        lambda *_args, **_kwargs: None,
    )
    downloaded = quote_documents.generate_quote_pdf(
        db_session,
        quote_documents.GenerateQuotePdfCommand(
            context=_context(),
            quote_id=quote_id,
        ),
    )
    key = f"pytest-quote-email:{uuid4()}"
    command = quote_delivery.SendQuoteEmailCommand(
        context=_context(key=key),
        quote_id=quote_id,
    )

    first = quote_delivery.send_quote_email(db_session, command)
    replay = quote_delivery.send_quote_email(db_session, command)

    assert first.queued is True
    assert first.pdf_export_id == downloaded.export_id
    assert replay.replayed is True
    assert replay.delivery_request_id == first.delivery_request_id
    assert len(captured) == 1
    assert captured[0].recipients == {
        NotificationChannel.email: primary.normalized_value
    }
    assert len(captured[0].attachments) == 1
    assert captured[0].attachments[0].kind.value == "quote_pdf"
    assert captured[0].attachments[0].entity_id == downloaded.export_id
    assert len(payment_queries) == 1
    assert payment_queries[0].quote_id == quote_id
    assert payment_queries[0].authorized_subscriber_ids == (subscriber.id,)

    intent = captured[0]
    payment_url = f"https://selfcare.example.com/portal/quotes/{quote_id}/pay"
    parsed_payment_url = urlsplit(payment_url)
    assert intent.subject == "Quote from Dotmac Technologies Ltd"
    assert intent.metadata["quote_payment_url"] == payment_url
    assert intent.metadata["body_text"] == intent.body
    assert payment_url in intent.metadata["body_html"]
    assert payment_url in intent.metadata["body_text"]
    assert parsed_payment_url.scheme == "https"
    assert parsed_payment_url.netloc == "selfcare.example.com"
    assert parsed_payment_url.path == f"/portal/quotes/{quote_id}/pay"
    assert parsed_payment_url.query == ""
    assert "paystack.com" not in payment_url

    body_html = intent.metadata["body_html"]
    body_text = intent.metadata["body_text"]
    for content in (
        "Your quotation is ready",
        "Dear Amina Bello,",
        "Please find attached your quotation for",
        "NGN 107,500.00",
        f"Quote reference:</strong> {quote_id}",
        "Make payment for this quotation",
        "make the required payment for this quotation through Paystack",
        "Pay Now",
        "bank-transfer details in the attached quotation PDF",
        "Please reply to this email if you have any questions.",
    ):
        assert content in body_html
    assert f'href="{payment_url}"' in body_html
    assert "background-color:#008000" in body_html
    assert contrast_ratio("#008000", "#ffffff") >= 4.5
    assert "Dotmac Technologies Ltd logo" in body_html
    assert "&copy; Dotmac Technologies Ltd" in body_html
    assert "support@example.com" in body_html
    assert "1016946461" not in body_html
    assert "Zenith Bank" not in body_html
    assert "Pay Now" in html_to_text(body_html)

    for content in (
        "Your quotation is ready",
        "Dear Amina Bello,",
        "Please find attached your quotation for NGN 107,500.00.",
        f"Quote reference: {quote_id}",
        "Make payment for this quotation",
        "make the required payment through Paystack",
        f"Pay Now: {payment_url}",
        "bank-transfer details in the attached quotation PDF",
        "Please reply to this email if you have any questions.",
    ):
        assert content in body_text
    assert "1016946461" not in body_text
    assert "Zenith Bank" not in body_text

    persisted_quote = db_session.get(Quote, quote_id)
    assert persisted_quote.status == QuoteStatus.sent.value
    assert persisted_quote.sent_at is not None

    notification = db_session.get(Notification, first.notification_ids[0])
    notification.status = NotificationStatus.delivered
    notification.sent_at = datetime.now(UTC)
    db_session.commit()
    activity = quote_activity.list_quote_activity(
        db_session,
        quote=persisted_quote,
    )
    delivered = next(item for item in activity if item["title"] == "Email delivered")
    assert "accepted by the configured mail transport" in delivered["description"]


def test_suppressed_email_does_not_mark_quote_sent(db_session, subscriber, monkeypatch):
    quote, _primary, quote_id = _quote(db_session, subscriber)
    _stub_pdf_storage(monkeypatch)
    _stub_payment_eligibility(monkeypatch, quote)

    def submit_suppressed(db, intent):
        record = CommunicationIntentRecord(
            subscriber_id=None,
            event_type=intent.event_type,
            category=intent.category,
            communication_class=intent.communication_class.value,
            template_code=intent.template_code,
            subject=intent.subject,
            body=intent.body,
            channels=[NotificationChannel.email.value],
            include_reseller=False,
            status="suppressed",
            suppression_reasons=["customer_policy"],
            dedupe_key=intent.dedupe_key,
            metadata_=dict(intent.metadata),
        )
        db.add(record)
        db.flush()
        return CommunicationIntentResult(
            intent_id=record.id,
            deliveries=(),
            queued=(),
            suppressed=("customer_policy",),
        )

    monkeypatch.setattr(document_delivery, "submit", submit_suppressed)
    monkeypatch.setattr(
        document_delivery,
        "emit_event",
        lambda *_args, **_kwargs: None,
    )
    outcome = quote_delivery.send_quote_email(
        db_session,
        quote_delivery.SendQuoteEmailCommand(
            context=_context(key=f"pytest-quote-suppressed:{uuid4()}"),
            quote_id=quote_id,
        ),
    )

    assert outcome.queued is False
    assert db_session.get(Quote, quote_id).status == QuoteStatus.draft.value
    request = db_session.get(QuoteDeliveryRequest, outcome.delivery_request_id)
    assert request.request_status == QuoteDeliveryRequestStatus.suppressed.value


def test_ineligible_payment_link_prevents_quote_delivery_queue(
    db_session, subscriber, monkeypatch
):
    _quote_record, _primary, quote_id = _quote(db_session, subscriber)

    def reject(_db, _query):
        raise quote_deposits.QuoteDepositError(
            code="sales.quote_deposits.paystack_unavailable",
            message="Paystack payment is unavailable for this Quote",
        )

    monkeypatch.setattr(quote_deposits, "quote_payment_page", reject)
    monkeypatch.setattr(
        document_delivery,
        "deliver",
        lambda *_args, **_kwargs: pytest.fail("delivery must not be queued"),
    )

    with pytest.raises(quote_delivery.QuoteDeliveryError) as exc:
        quote_delivery.send_quote_email(
            db_session,
            quote_delivery.SendQuoteEmailCommand(
                context=_context(key=f"pytest-quote-ineligible:{uuid4()}"),
                quote_id=quote_id,
            ),
        )

    assert exc.value.code == "sales.quote_delivery.payment_link_unavailable"
    assert exc.value.details["reason"] == ("sales.quote_deposits.paystack_unavailable")
    assert db_session.query(QuoteDeliveryRequest).count() == 0
    assert db_session.query(CommunicationIntentRecord).count() == 0
    assert db_session.get(Quote, quote_id).status == QuoteStatus.draft.value


def test_quote_email_requires_authoritative_legal_name(db_session, subscriber):
    quote, _primary, _quote_id = _quote(db_session, subscriber)
    recipient = quote_documents.resolve_quote_recipient(db_session, quote)
    snapshot, _logo_src = quote_documents._snapshot(db_session, quote)
    assert recipient is not None

    with pytest.raises(quote_delivery.QuoteDeliveryError) as exc:
        quote_delivery.render_quote_email(
            snapshot=replace(snapshot, brand=replace(snapshot.brand, legal_name="")),
            recipient=recipient,
        )

    assert exc.value.code == "sales.quote_delivery.brand_legal_name_required"


def test_stored_quote_payment_url_must_match_secure_company_route(
    db_session, subscriber
):
    quote, _primary, _quote_id = _quote(db_session, subscriber)
    snapshot, _logo_src = quote_documents._snapshot(db_session, quote)
    stored = snapshot.to_storage()
    stored["payment"]["paystack_url"] = (
        f"https://checkout.paystack.com/portal/quotes/{quote.id}/pay"
    )

    with pytest.raises(quote_documents.QuoteDocumentError) as exc:
        quote_documents.load_quote_document_snapshot(stored)

    assert exc.value.code == "sales.quote_documents.invalid_snapshot"


def test_payment_section_follows_totals_and_precedes_footer(db_session, subscriber):
    quote, _primary, _quote_id = _quote(db_session, subscriber)
    snapshot, logo_src = quote_documents._snapshot(db_session, quote)

    rendered = quote_documents._render_html(snapshot, logo_src)

    assert rendered.index("class='totals'") < rendered.index("Ways to make payment")
    assert rendered.index("Ways to make payment") < rendered.index("class='footer'")
    assert "Bank Transfer" in rendered
    assert "Bank</span>" in rendered
    assert "Account Number</span>" in rendered
    assert "Account Name</span>" in rendered
    assert "Zenith Bank" in rendered
    assert "1016946461" in rendered
    assert "Dotmac Technologies Ltd." in rendered
    assert "Sort code" not in rendered
    assert "Payment reference" not in rendered
    assert "Currency</span>" not in rendered
    assert "collection_account_id" not in rendered
    assert snapshot.payment.paystack_url.startswith("https://")
    assert snapshot.payment.paystack_url.endswith(f"/portal/quotes/{quote.id}/pay")
    assert f"href='{snapshot.payment.paystack_url}'" in rendered
    assert ">Pay Now</a>" in rendered
    assert "break-inside:avoid" in rendered
    assert "overflow-wrap:anywhere" in rendered


def test_quote_document_fails_closed_without_eligible_bank_details(
    db_session, subscriber
):
    quote, _primary, _quote_id = _quote(
        db_session,
        subscriber,
        with_transfer_account=False,
    )

    try:
        quote_documents._snapshot(db_session, quote)
    except quote_documents.QuoteDocumentError as exc:
        assert exc.code == "sales.quote_documents.bank_details_unavailable"
    else:  # pragma: no cover - assertion guard
        raise AssertionError("missing bank details must fail closed")


def test_quote_document_fails_closed_without_customer_portal_identity(
    db_session, subscriber
):
    quote, _primary, _quote_id = _quote(db_session, subscriber)
    quote.subscriber_id = None
    db_session.flush()

    try:
        quote_documents._snapshot(db_session, quote)
    except quote_documents.QuoteDocumentError as exc:
        assert exc.code == "sales.quote_documents.payment_identity_required"
    else:  # pragma: no cover - assertion guard
        raise AssertionError("missing portal identity must fail closed")


def test_primary_presentment_skips_disabled_incomplete_and_other_currency(
    db_session,
):
    accounts = (
        CollectionAccount(
            name="Disabled higher priority",
            account_type=CollectionAccountType.bank,
            bank_name="Disabled Bank",
            account_name="Dotmac Technologies Ltd.",
            account_number="0000000001",
            currency="NGN",
            presentment_priority=999,
            is_active=False,
        ),
        CollectionAccount(
            name="Incomplete higher priority",
            account_type=CollectionAccountType.bank,
            bank_name="Incomplete Bank",
            account_name="   ",
            account_number="0000000002",
            currency="NGN",
            presentment_priority=998,
            is_active=True,
        ),
        CollectionAccount(
            name="USD higher priority",
            account_type=CollectionAccountType.bank,
            bank_name="Dollar Bank",
            account_name="Dotmac Technologies Ltd.",
            account_number="0000000003",
            currency="USD",
            presentment_priority=997,
            is_active=True,
        ),
        CollectionAccount(
            name="Eligible NGN account",
            account_type=CollectionAccountType.bank,
            bank_name="Zenith Bank",
            account_name="Dotmac Technologies Ltd.",
            account_number="1016946461",
            currency="NGN",
            presentment_priority=10,
            is_active=True,
        ),
    )
    db_session.add_all(accounts)
    db_session.commit()

    selected = CollectionAccounts.primary_presentment_account_projection(
        db_session,
        currency="NGN",
    )

    assert selected is not None
    assert selected.account_id == accounts[-1].id
    assert selected.bank_name == "Zenith Bank"
    assert selected.account_number == "1016946461"
    assert selected.account_name == "Dotmac Technologies Ltd."


def test_payment_section_renders_long_values_and_page_boundary(db_session, subscriber):
    quote, _primary, _quote_id = _quote(db_session, subscriber)
    account = db_session.query(CollectionAccount).one()
    account.account_name = (
        "Dotmac Technologies Limited Enterprise Connectivity and Infrastructure "
        "Settlement Account"
    )
    for index in range(35):
        db_session.add(
            QuoteLineItem(
                quote_id=quote.id,
                description=f"Additional installation item {index + 1}",
                quantity=Decimal("1.000"),
                unit_price=Decimal("1000.00"),
                discount_percent=Decimal("0.00"),
                amount=Decimal("1000.00"),
            )
        )
    db_session.commit()
    db_session.refresh(quote)

    snapshot, logo_src = quote_documents._snapshot(db_session, quote)
    rendered = quote_documents._render_html(snapshot, logo_src)
    try:
        pdf = quote_documents._render_pdf(snapshot, logo_src)
    except quote_documents.QuoteDocumentError as exc:
        if exc.code == "sales.quote_documents.renderer_unavailable":
            pytest.skip("WeasyPrint native libraries are unavailable")
        raise

    assert account.account_name in rendered
    assert "page-break-inside:avoid" in rendered
    assert pdf.startswith(b"%PDF-")
    assert len(pdf) > 2000
