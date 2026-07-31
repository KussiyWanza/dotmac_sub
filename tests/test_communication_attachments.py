from decimal import Decimal

import pytest

from app.models.billing import (
    Invoice,
    InvoicePdfExport,
    InvoicePdfExportStatus,
    InvoiceStatus,
)
from app.models.notification import Notification, NotificationChannel
from app.models.subscriber import Subscriber
from app.services import communication_attachments


def _notification(subscriber, invoice) -> Notification:
    return Notification(
        subscriber_id=subscriber.id,
        channel=NotificationChannel.email,
        recipient=subscriber.email,
        metadata_={
            "attachments": [
                {
                    "kind": "invoice_pdf",
                    "entity_id": str(invoice.id),
                    "filename": "../invoice INV-1.pdf",
                    "content_type": "application/pdf",
                }
            ]
        },
    )


def test_resolve_invoice_pdf_uses_canonical_export(db_session, subscriber, monkeypatch):
    invoice = Invoice(
        account_id=subscriber.id,
        invoice_number="INV-1",
        status=InvoiceStatus.issued,
        subtotal=Decimal("100.00"),
        tax_total=Decimal("0.00"),
        total=Decimal("100.00"),
        balance_due=Decimal("100.00"),
    )
    db_session.add(invoice)
    db_session.commit()
    export = InvoicePdfExport(
        invoice_id=invoice.id,
        status=InvoicePdfExportStatus.completed,
        file_path="invoice.pdf",
    )
    monkeypatch.setattr(
        communication_attachments.billing_invoice_pdf,
        "generate_export_now",
        lambda *args, **kwargs: export,
    )
    monkeypatch.setattr(
        communication_attachments.billing_invoice_pdf,
        "stream_export",
        lambda *args, **kwargs: type(
            "Stream", (), {"chunks": iter((b"%PDF-1.4 ", b"invoice"))}
        )(),
    )

    resolved = communication_attachments.resolve_email_attachments(
        db_session, _notification(subscriber, invoice)
    )

    assert resolved[0].filename == "invoice-INV-1.pdf"
    assert resolved[0].content == b"%PDF-1.4 invoice"


def test_resolve_invoice_pdf_fails_closed_on_account_mismatch(db_session, subscriber):
    other_subscriber = Subscriber(
        first_name="Other",
        last_name="Customer",
        email="other-invoice-customer@example.com",
        reseller_id=subscriber.reseller_id,
    )
    db_session.add(other_subscriber)
    db_session.flush()
    invoice = Invoice(
        account_id=other_subscriber.id,
        invoice_number="INV-OTHER",
        status=InvoiceStatus.issued,
        subtotal=Decimal("100.00"),
        tax_total=Decimal("0.00"),
        total=Decimal("100.00"),
        balance_due=Decimal("100.00"),
    )
    db_session.add(invoice)
    db_session.commit()

    with pytest.raises(
        communication_attachments.CommunicationAttachmentError,
        match="invoice_attachment_scope_mismatch",
    ):
        communication_attachments.resolve_email_attachments(
            db_session, _notification(subscriber, invoice)
        )
