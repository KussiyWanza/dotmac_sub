import hashlib
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from app.models.billing import (
    Invoice,
    InvoicePdfExport,
    InvoicePdfExportStatus,
    InvoiceStatus,
)
from app.models.ncc_reporting import NccWeeklyReportRun, NccWeeklyReportRunStatus
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


def test_resolve_ncc_xlsx_verifies_scope_and_digest(db_session):
    content = b"PK\x03\x04ncc-workbook"
    notification = Notification(
        channel=NotificationChannel.email,
        recipient="compliance@example.test",
        metadata_={},
    )
    db_session.add(notification)
    db_session.flush()
    run = NccWeeklyReportRun(
        schedule_key="ncc_complaints",
        scheduled_local_date=date(2026, 7, 21),
        schedule_timezone="Africa/Lagos",
        scheduled_local_time="08:00",
        window_start=datetime(2026, 7, 14, 7, 0, tzinfo=UTC),
        window_end=datetime(2026, 7, 21, 7, 0, tzinfo=UTC),
        configuration_fingerprint="a" * 64,
        status=NccWeeklyReportRunStatus.queued,
        artifact_filename="ncc-weekly.xlsx",
        artifact_content_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        artifact_content=content,
        artifact_sha256=hashlib.sha256(content).hexdigest(),
        row_count=0,
        not_filable_count=0,
        notification_id=notification.id,
        command_id=uuid4(),
        correlation_id=uuid4(),
    )
    db_session.add(run)
    db_session.flush()
    notification.metadata_ = {
        "ncc_weekly_report_run_id": str(run.id),
        "attachments": [
            {
                "kind": "ncc_weekly_xlsx",
                "entity_id": str(run.id),
                "filename": "../ncc-weekly.xlsx",
                "content_type": run.artifact_content_type,
            }
        ],
    }
    db_session.flush()

    resolved = communication_attachments.resolve_email_attachments(
        db_session, notification
    )

    assert resolved[0].filename == "ncc-weekly.xlsx"
    assert resolved[0].content == content

    run.artifact_sha256 = "0" * 64
    db_session.flush()
    with pytest.raises(
        communication_attachments.CommunicationAttachmentError,
        match="ncc_xlsx_integrity_failed",
    ):
        communication_attachments.resolve_email_attachments(db_session, notification)
