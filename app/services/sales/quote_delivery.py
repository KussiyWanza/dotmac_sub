"""Typed, idempotent customer email delivery for branded Quotes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.audit import AuditActorType
from app.models.notification import NotificationChannel
from app.models.sales import (
    Quote,
    QuoteDeliveryRequest,
    QuoteDeliveryRequestStatus,
    QuoteStatus,
)
from app.services import quote_deposits
from app.services.audit_adapter import stage_audit_event
from app.services.brand_theme import DEFAULT_HEX, contrast_ratio
from app.services.communication_intents import (
    CommunicationAttachment,
    CommunicationAttachmentKind,
    CommunicationIntent,
    submit,
)
from app.services.domain_errors import DomainError
from app.services.email_template import render_email_bodies
from app.services.events import EventType, emit_event
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.sales import quote_documents

_SEND_QUOTE_EMAIL = OwnerCommandDefinition(
    owner="sales.quote_delivery",
    concern="idempotent branded Quote email request",
    name="send_quote_email",
)


class QuoteDeliveryError(DomainError):
    """Stable failure raised by the Quote delivery owner."""


@dataclass(frozen=True)
class SendQuoteEmailCommand:
    context: CommandContext
    quote_id: UUID


@dataclass(frozen=True)
class SendQuoteEmailOutcome:
    delivery_request_id: UUID
    quote_id: UUID
    pdf_export_id: UUID
    communication_intent_id: UUID
    notification_ids: tuple[UUID, ...]
    recipient_masked: str
    queued: bool
    replayed: bool
    suppression_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class QuoteEmailContent:
    subject: str
    body_html: str
    body_text: str
    payment_url: str


_HEX_COLOR = re.compile(r"#[0-9a-fA-F]{6}")


def _error(suffix: str, message: str, **details: object) -> QuoteDeliveryError:
    return QuoteDeliveryError(
        code=f"sales.quote_delivery.{suffix}",
        message=message,
        details=details,
    )


def _uuid_or_none(value: str | None) -> UUID | None:
    try:
        return UUID(str(value)) if value else None
    except ValueError:
        return None


def mask_email(value: str) -> str:
    local, separator, domain = value.partition("@")
    if not separator:
        return "hidden recipient"
    visible = local[:1]
    return f"{visible}{'*' * max(3, len(local) - 1)}@{domain}"


def _button_colors(primary_color: str) -> tuple[str, str]:
    background = (
        primary_color if _HEX_COLOR.fullmatch(primary_color.strip()) else DEFAULT_HEX
    )
    candidates = ("#111827", "#ffffff")
    foreground = max(
        candidates,
        key=lambda candidate: contrast_ratio(background, candidate),
    )
    return background, foreground


def render_quote_email(
    *,
    snapshot: quote_documents.QuoteDocumentSnapshot,
    recipient: quote_documents.QuoteRecipient,
) -> QuoteEmailContent:
    """Render the branded HTML/text alternatives from one immutable snapshot."""

    brand = snapshot.brand
    legal_name = brand.legal_name.strip()
    if not legal_name:
        raise _error(
            "brand_legal_name_required",
            "The company legal name is unavailable for Quote delivery",
        )
    payment_url = snapshot.payment.paystack_url
    background, foreground = _button_colors(brand.primary_color)
    total = f"{snapshot.currency} {snapshot.total:,.2f}"
    reference = str(snapshot.quote_id)
    safe_name = escape(recipient.display_name)
    safe_total = escape(total)
    safe_reference = escape(reference)
    safe_payment_url = escape(payment_url, quote=True)
    body = f"""
<h1 style="margin:0 0 20px;font-size:24px;line-height:1.25;color:#111827;">Your quotation is ready</h1>
<p style="margin:0 0 16px;font-size:14px;line-height:1.6;">Dear {safe_name},</p>
<p style="margin:0 0 16px;font-size:14px;line-height:1.6;">Please find attached your quotation for <strong>{safe_total}</strong>.</p>
<p style="margin:0 0 24px;font-size:14px;line-height:1.6;"><strong>Quote reference:</strong> {safe_reference}</p>
<h2 style="margin:0 0 12px;font-size:18px;line-height:1.35;color:#111827;">Make payment for this quotation</h2>
<p style="margin:0 0 20px;font-size:14px;line-height:1.6;">Use the secure payment button below to make the required payment for this quotation through Paystack.</p>
<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:0 auto 22px;"><tr><td style="border-radius:8px;background-color:{background};text-align:center;"><a href="{safe_payment_url}" aria-label="Pay Now for quotation {safe_reference}" style="display:inline-block;min-width:120px;padding:14px 24px;border-radius:8px;background-color:{background};color:{foreground} !important;font-size:15px;font-weight:700;line-height:1;text-align:center;text-decoration:none;">Pay Now</a></td></tr></table>
<p style="margin:0 0 16px;font-size:14px;line-height:1.6;">Alternatively, you can find the available bank-transfer details in the attached quotation PDF.</p>
<p style="margin:0;font-size:14px;line-height:1.6;">Please reply to this email if you have any questions.</p>
""".strip()
    subject = f"Quote from {legal_name}"
    body_html, _generated_text = render_email_bodies(
        body,
        subject=subject,
        base_url=brand.app_url,
        brand=brand.to_dict(),
    )
    body_text = (
        "Your quotation is ready\n\n"
        f"Dear {recipient.display_name},\n\n"
        f"Please find attached your quotation for {total}.\n\n"
        f"Quote reference: {reference}\n\n"
        "Make payment for this quotation\n\n"
        "Use the secure link below to make the required payment through Paystack:\n\n"
        f"Pay Now: {payment_url}\n\n"
        "Alternatively, you can find the available bank-transfer details in the "
        "attached quotation PDF.\n\n"
        "Please reply to this email if you have any questions."
    )
    return QuoteEmailContent(
        subject=subject,
        body_html=body_html,
        body_text=body_text,
        payment_url=payment_url,
    )


def _payment_eligibility(
    db: Session,
    quote: Quote,
) -> quote_deposits.QuotePaymentPage:
    if quote.subscriber_id is None:
        raise _error(
            "payment_link_unavailable",
            "A secure Paystack payment link is unavailable for this Quote",
            reason="sales.quote_deposits.quote_not_found",
        )
    try:
        return quote_deposits.quote_payment_page(
            db,
            quote_deposits.QuotePaymentQuery(
                quote_id=quote.id,
                authorized_subscriber_ids=(quote.subscriber_id,),
                observed_at=datetime.now(UTC),
            ),
        )
    except quote_deposits.QuoteDepositError as exc:
        raise _error(
            "payment_link_unavailable",
            "A secure Paystack payment link is unavailable for this Quote",
            reason=exc.code,
        ) from exc


def _locked_quote(db: Session, quote_id: UUID) -> Quote:
    quote = db.scalars(
        select(Quote)
        .where(Quote.id == quote_id, Quote.is_active.is_(True))
        .options(
            selectinload(Quote.line_items),
            selectinload(Quote.lead),
        )
        .with_for_update()
    ).one_or_none()
    if quote is None:
        raise _error("quote_not_found", "Quote not found")
    if not quote.line_items:
        raise _error(
            "line_items_required",
            "Add at least one line item before emailing this Quote",
        )
    if quote.status in {QuoteStatus.rejected.value, QuoteStatus.expired.value}:
        raise _error(
            "status_not_sendable",
            "Rejected or expired Quotes cannot be emailed",
            status=quote.status,
        )
    expires_at = quote.expires_at
    if expires_at is not None:
        expiry = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=UTC)
        if expiry <= datetime.now(UTC):
            raise _error(
                "quote_expired",
                "This Quote has expired and cannot be emailed",
                expires_at=expiry.isoformat(),
            )
    return quote


def _replay_outcome(request: QuoteDeliveryRequest) -> SendQuoteEmailOutcome:
    notifications = tuple(request.communication_intent.notifications)
    return SendQuoteEmailOutcome(
        delivery_request_id=request.id,
        quote_id=request.quote_id,
        pdf_export_id=request.pdf_export_id,
        communication_intent_id=request.communication_intent_id,
        notification_ids=tuple(item.id for item in notifications),
        recipient_masked=mask_email(request.recipient_contact_point.normalized_value),
        queued=request.request_status == QuoteDeliveryRequestStatus.queued.value,
        replayed=True,
        suppression_reasons=tuple(
            request.communication_intent.suppression_reasons or ()
        ),
    )


def send_quote_email(
    db: Session, command: SendQuoteEmailCommand
) -> SendQuoteEmailOutcome:
    if not command.context.idempotency_key:
        raise _error(
            "idempotency_key_required",
            "Quote email delivery requires an idempotency key",
        )

    def operation() -> SendQuoteEmailOutcome:
        existing = db.scalars(
            select(QuoteDeliveryRequest).where(
                QuoteDeliveryRequest.idempotency_key == command.context.idempotency_key
            )
        ).one_or_none()
        if existing is not None:
            if existing.quote_id != command.quote_id:
                raise _error(
                    "idempotency_conflict",
                    "This Quote email request key was used for another Quote",
                )
            return _replay_outcome(existing)

        quote = _locked_quote(db, command.quote_id)
        recipient = quote_documents.resolve_quote_recipient(db, quote)
        if recipient is None:
            raise _error(
                "recipient_email_required",
                "The Quote customer needs an active email address before delivery",
            )

        payment = _payment_eligibility(db, quote)

        actor_id = _uuid_or_none(command.context.actor)
        export, _ = quote_documents.stage_quote_pdf_export(
            db,
            quote=quote,
            requested_by_id=actor_id,
        )
        snapshot = quote_documents.load_quote_document_snapshot(export.snapshot)
        if (
            payment.quote_id != snapshot.quote_id
            or payment.subscriber_id != quote.subscriber_id
            or payment.currency != snapshot.currency
            or payment.provider_type != "paystack"
        ):
            raise _error(
                "payment_link_unavailable",
                "A secure Paystack payment link is unavailable for this Quote",
                reason="eligibility_snapshot_mismatch",
            )
        content = render_quote_email(
            snapshot=snapshot,
            recipient=recipient,
        )
        request_id = uuid4()
        intent_result = submit(
            db,
            CommunicationIntent(
                subscriber_id=quote.subscriber_id,
                event_type="quote.delivery_requested",
                category="sales",
                template_code="quote_sent",
                subject=content.subject,
                body=content.body_text,
                channels=(NotificationChannel.email,),
                include_reseller=False,
                resolve_subscriber_identity=False,
                recipients={NotificationChannel.email: recipient.email},
                attachments=(
                    CommunicationAttachment(
                        kind=CommunicationAttachmentKind.quote_pdf,
                        entity_id=export.id,
                        filename=quote_documents.download_filename(export),
                    ),
                ),
                metadata={
                    "quote_id": str(quote.id),
                    "quote_delivery_request_id": str(request_id),
                    "pdf_export_id": str(export.id),
                    "body_html": content.body_html,
                    "body_text": content.body_text,
                    "quote_payment_url": content.payment_url,
                    "activity": "sales_quote",
                },
                dedupe_key=f"quote-email:{command.context.idempotency_key}",
            ),
        )
        queued = bool(intent_result.queued)
        request_status = (
            QuoteDeliveryRequestStatus.queued.value
            if queued
            else QuoteDeliveryRequestStatus.suppressed.value
        )
        delivery_request = QuoteDeliveryRequest(
            id=request_id,
            quote_id=quote.id,
            pdf_export_id=export.id,
            recipient_contact_point_id=recipient.contact_point_id,
            communication_intent_id=intent_result.intent_id,
            requested_by_id=actor_id,
            idempotency_key=command.context.idempotency_key,
            request_status=request_status,
        )
        db.add(delivery_request)
        if queued and quote.status == QuoteStatus.draft.value:
            quote.status = QuoteStatus.sent.value
            quote.sent_at = datetime.now(UTC)

        audit_action = "quote.email_queued" if queued else "quote.email_suppressed"
        stage_audit_event(
            db,
            action=audit_action,
            entity_type="quote",
            entity_id=str(quote.id),
            actor_type=AuditActorType.user,
            actor_id=command.context.actor,
            request_id=str(command.context.command_id),
            metadata={
                "delivery_request_id": str(request_id),
                "communication_intent_id": str(intent_result.intent_id),
                "pdf_export_id": str(export.id),
                "recipient_contact_point_id": str(recipient.contact_point_id),
                "recipient_masked": mask_email(recipient.email),
                "suppression_reasons": list(intent_result.suppressed),
            },
        )
        emit_event(
            db,
            EventType.quote_delivery_requested,
            {
                "quote_id": str(quote.id),
                "delivery_request_id": str(request_id),
                "communication_intent_id": str(intent_result.intent_id),
                "pdf_export_id": str(export.id),
                "queued": queued,
            },
            actor=command.context.actor,
        )
        db.flush()
        return SendQuoteEmailOutcome(
            delivery_request_id=request_id,
            quote_id=quote.id,
            pdf_export_id=export.id,
            communication_intent_id=intent_result.intent_id,
            notification_ids=tuple(item.id for item in intent_result.deliveries),
            recipient_masked=mask_email(recipient.email),
            queued=queued,
            replayed=False,
            suppression_reasons=tuple(intent_result.suppressed),
        )

    return execute_owner_command(
        db,
        definition=_SEND_QUOTE_EMAIL,
        context=command.context,
        operation=operation,
    )
