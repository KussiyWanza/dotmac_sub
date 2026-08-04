"""Focused behavior tests for branded Quote documents and delivery."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from app.models.audit import AuditEvent
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
from app.services.brand_profiles import ResolvedBrand
from app.services.communication_intents import CommunicationIntentResult
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


def _quote(db_session) -> tuple[Quote, PartyContactPoint, UUID]:
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


def test_recipient_uses_primary_active_party_email(db_session):
    quote, primary, _quote_id = _quote(db_session)
    recipient = quote_documents.resolve_quote_recipient(db_session, quote)

    assert recipient is not None
    assert recipient.contact_point_id == primary.id
    assert recipient.email == "amina@example.com"
    assert recipient.display_name == "Amina Bello"


def test_pdf_export_is_content_addressed_and_audited_once(db_session, monkeypatch):
    _quote_record, _primary, quote_id = _quote(db_session)
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


def test_send_email_queues_one_pdf_intent_and_replays(db_session, monkeypatch):
    _quote_record, primary, quote_id = _quote(db_session)
    _stub_pdf_storage(monkeypatch)
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

    monkeypatch.setattr(quote_delivery, "submit", submit)
    monkeypatch.setattr(
        quote_delivery,
        "emit_event",
        lambda *_args, **_kwargs: None,
    )
    key = f"pytest-quote-email:{uuid4()}"
    command = quote_delivery.SendQuoteEmailCommand(
        context=_context(key=key),
        quote_id=quote_id,
    )

    first = quote_delivery.send_quote_email(db_session, command)
    replay = quote_delivery.send_quote_email(db_session, command)

    assert first.queued is True
    assert replay.replayed is True
    assert replay.delivery_request_id == first.delivery_request_id
    assert len(captured) == 1
    assert captured[0].recipients == {
        NotificationChannel.email: primary.normalized_value
    }
    assert len(captured[0].attachments) == 1
    assert captured[0].attachments[0].kind.value == "quote_pdf"
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


def test_suppressed_email_does_not_mark_quote_sent(db_session, monkeypatch):
    _quote_record, _primary, quote_id = _quote(db_session)
    _stub_pdf_storage(monkeypatch)

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

    monkeypatch.setattr(quote_delivery, "submit", submit_suppressed)
    monkeypatch.setattr(
        quote_delivery,
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
