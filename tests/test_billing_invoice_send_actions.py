from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.models.billing import (
    Invoice,
    InvoicePdfExport,
    InvoicePdfExportStatus,
    InvoiceStatus,
)
from app.models.notification import NotificationChannel, NotificationStatus
from app.services.communication_intents import (
    CommunicationAttachment,
    CommunicationAttachmentKind,
    CommunicationIntent,
    submit,
)
from app.services.web_billing_invoice_bulk import (
    BulkInvoiceActionResult,
    bulk_mark_paid,
    bulk_queue_pdf_exports,
    bulk_send,
    list_invoices_by_ids,
)
from app.services.web_billing_invoices import maybe_send_invoice_notification
from app.web.admin import billing_invoice_bulk as bulk_routes


def test_bulk_send_calls_invoice_notification_helper(
    db_session, subscriber, monkeypatch
):
    invoice = Invoice(
        account_id=subscriber.id,
        status=InvoiceStatus.issued,
        subtotal=Decimal("100.00"),
        tax_total=Decimal("0.00"),
        total=Decimal("100.00"),
        balance_due=Decimal("100.00"),
    )
    db_session.add(invoice)
    db_session.commit()

    called = {"count": 0}

    def _fake_send(db, *, invoice, send_notification):
        called["count"] += 1

    monkeypatch.setattr(
        "app.services.web_billing_invoice_bulk.web_billing_invoices_service.maybe_send_invoice_notification",
        _fake_send,
    )

    queued = bulk_send(db_session, str(invoice.id))

    assert queued == [str(invoice.id)]
    assert called["count"] == 1


def test_invoice_notification_delegates_once_to_canonical_event_owner(
    db_session, subscriber, monkeypatch
):
    invoice = Invoice(
        account_id=subscriber.id,
        invoice_number="INV-1001",
        status=InvoiceStatus.issued,
        currency="NGN",
        subtotal=Decimal("15000.00"),
        tax_total=Decimal("0.00"),
        total=Decimal("15000.00"),
        balance_due=Decimal("15000.00"),
        due_at=datetime(2026, 6, 24, tzinfo=UTC),
    )
    db_session.add(invoice)
    db_session.commit()
    db_session.refresh(invoice)

    captured: list[dict[str, object]] = []

    def _fake_announce(db, invoice_id, *, reason, commit):
        captured.append({"invoice_id": invoice_id, "reason": reason, "commit": commit})
        return invoice

    monkeypatch.setattr(
        "app.services.web_billing_invoices.billing_service.invoices.announce_issued",
        _fake_announce,
    )

    maybe_send_invoice_notification(db_session, invoice=invoice, send_notification="1")

    assert captured == [
        {
            "invoice_id": str(invoice.id),
            "reason": "admin_invoice_send",
            "commit": True,
        }
    ]


def test_invoice_pdf_attachment_reference_is_durable_on_email_delivery(
    db_session, subscriber
):
    invoice = Invoice(
        account_id=subscriber.id,
        invoice_number="INV-ATTACH-1",
        status=InvoiceStatus.issued,
        subtotal=Decimal("15000.00"),
        tax_total=Decimal("0.00"),
        total=Decimal("15000.00"),
        balance_due=Decimal("15000.00"),
    )
    db_session.add(invoice)
    db_session.commit()

    result = submit(
        db_session,
        CommunicationIntent(
            subscriber_id=subscriber.id,
            event_type="invoice_sent",
            category="billing",
            subject="Invoice attached",
            body="Your invoice is attached.",
            channels=(NotificationChannel.email,),
            recipients={NotificationChannel.email: subscriber.email},
            include_reseller=False,
            persist_policy_suppressions=False,
            attachments=(
                CommunicationAttachment(
                    kind=CommunicationAttachmentKind.invoice_pdf,
                    entity_id=invoice.id,
                    filename="invoice-INV-ATTACH-1.pdf",
                ),
            ),
        ),
    )

    assert len(result.deliveries) == 1
    delivery = result.deliveries[0]
    assert delivery.status == NotificationStatus.queued
    assert delivery.metadata_["attachments"] == [
        {
            "kind": "invoice_pdf",
            "entity_id": str(invoice.id),
            "filename": "invoice-INV-ATTACH-1.pdf",
            "content_type": "application/pdf",
        }
    ]


def test_list_invoices_by_ids_preserves_order_and_deduplicates(db_session, subscriber):
    now = datetime.now(UTC)
    inv_a = Invoice(
        account_id=subscriber.id,
        invoice_number="INV-BULK-A",
        status=InvoiceStatus.issued,
        subtotal=Decimal("50.00"),
        tax_total=Decimal("0.00"),
        total=Decimal("50.00"),
        balance_due=Decimal("50.00"),
        created_at=now,
    )
    inv_b = Invoice(
        account_id=subscriber.id,
        invoice_number="INV-BULK-B",
        status=InvoiceStatus.issued,
        subtotal=Decimal("75.00"),
        tax_total=Decimal("0.00"),
        total=Decimal("75.00"),
        balance_due=Decimal("75.00"),
        created_at=now,
    )
    db_session.add_all([inv_a, inv_b])
    db_session.commit()

    ids_csv = f"{inv_b.id},missing-id,{inv_a.id},{inv_b.id}"
    invoices = list_invoices_by_ids(db_session, ids_csv)

    assert [invoice.id for invoice in invoices] == [inv_b.id, inv_a.id]


def test_bulk_mark_paid_updates_only_eligible_statuses(db_session, subscriber):
    now = datetime.now(UTC)
    inv_issued = Invoice(
        account_id=subscriber.id,
        invoice_number="INV-MP-ISSUED",
        status=InvoiceStatus.issued,
        subtotal=Decimal("100.00"),
        tax_total=Decimal("0.00"),
        total=Decimal("100.00"),
        balance_due=Decimal("100.00"),
        created_at=now,
    )
    inv_overdue = Invoice(
        account_id=subscriber.id,
        invoice_number="INV-MP-OVERDUE",
        status=InvoiceStatus.overdue,
        subtotal=Decimal("80.00"),
        tax_total=Decimal("0.00"),
        total=Decimal("80.00"),
        balance_due=Decimal("80.00"),
        created_at=now,
    )
    inv_draft = Invoice(
        account_id=subscriber.id,
        invoice_number="INV-MP-DRAFT",
        status=InvoiceStatus.draft,
        subtotal=Decimal("40.00"),
        tax_total=Decimal("0.00"),
        total=Decimal("40.00"),
        balance_due=Decimal("40.00"),
        created_at=now,
    )
    db_session.add_all([inv_issued, inv_overdue, inv_draft])
    db_session.commit()

    updated = bulk_mark_paid(
        db_session,
        f"{inv_issued.id},{inv_overdue.id},{inv_draft.id}",
    )
    db_session.refresh(inv_issued)
    db_session.refresh(inv_overdue)
    db_session.refresh(inv_draft)

    assert updated == [str(inv_issued.id), str(inv_overdue.id)]
    assert inv_issued.status == InvoiceStatus.paid
    assert inv_issued.balance_due == Decimal("0")
    assert inv_issued.paid_at is not None
    assert inv_overdue.status == InvoiceStatus.paid
    assert inv_overdue.balance_due == Decimal("0")
    assert inv_draft.status == InvoiceStatus.draft


def test_bulk_mark_paid_confirm_redirect_reports_skipped_count(db_session, monkeypatch):
    def _fake_execute_audited_bulk_action_result(
        db, request, *, action, invoice_ids_csv
    ):
        assert action == "mark_paid"
        assert invoice_ids_csv == "inv-1,inv-2,missing"
        return BulkInvoiceActionResult(
            selected_ids=["inv-1", "inv-2", "missing"],
            processed_ids=["inv-1"],
            skipped_ids=["inv-2", "missing"],
        )

    monkeypatch.setattr(
        bulk_routes.web_billing_invoice_bulk_service,
        "execute_audited_bulk_action_result",
        _fake_execute_audited_bulk_action_result,
    )
    monkeypatch.setattr(
        bulk_routes,
        "_require_confirmed_invoice_scope",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        bulk_routes,
        "_require_action_permission",
        lambda *args, **kwargs: None,
    )

    response = bulk_routes.invoice_bulk_confirm(
        request=None,
        action="mark-paid",
        invoice_ids="inv-1,inv-2,missing",
        confirmed="yes",
        expected_count=3,
        expected_scope_token="preview-token",
        auth={},
        db=db_session,
    )

    assert response.status_code == 303
    assert "2%20skipped" in response.headers["location"]


def test_bulk_queue_pdf_exports_reports_ready_and_queued(
    db_session, subscriber, monkeypatch
):
    now = datetime.now(UTC)
    inv_ready = Invoice(
        account_id=subscriber.id,
        invoice_number="INV-PDF-READY",
        status=InvoiceStatus.issued,
        subtotal=Decimal("20.00"),
        tax_total=Decimal("0.00"),
        total=Decimal("20.00"),
        balance_due=Decimal("20.00"),
        created_at=now,
    )
    inv_queue = Invoice(
        account_id=subscriber.id,
        invoice_number="INV-PDF-QUEUE",
        status=InvoiceStatus.issued,
        subtotal=Decimal("30.00"),
        tax_total=Decimal("0.00"),
        total=Decimal("30.00"),
        balance_due=Decimal("30.00"),
        created_at=now,
    )
    db_session.add_all([inv_ready, inv_queue])
    db_session.commit()

    ready_export = InvoicePdfExport(
        invoice_id=inv_ready.id,
        status=InvoicePdfExportStatus.completed,
        file_path="fake/path/ready.pdf",
    )
    db_session.add(ready_export)
    db_session.commit()

    monkeypatch.setattr(
        "app.services.web_billing_invoice_bulk.billing_invoice_pdf_service.export_file_exists",
        lambda db, export: bool(export and export.invoice_id == inv_ready.id),
    )
    queued_calls: list[str] = []

    def _fake_queue_export(db, invoice_id: str, requested_by_id: str | None = None):
        queued_calls.append(invoice_id)
        return None

    monkeypatch.setattr(
        "app.services.web_billing_invoice_bulk.billing_invoice_pdf_service.queue_export",
        _fake_queue_export,
    )

    result = bulk_queue_pdf_exports(
        db_session,
        f"{inv_ready.id},{inv_queue.id},missing-id",
        requested_by_id=str(subscriber.id),
    )

    assert result["ready"] == [str(inv_ready.id)]
    assert result["queued"] == [str(inv_queue.id)]
    assert "missing-id" in result["missing"]
    assert queued_calls == [str(inv_queue.id)]
