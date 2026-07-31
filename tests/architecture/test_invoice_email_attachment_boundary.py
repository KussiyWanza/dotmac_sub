from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_invoice_email_uses_canonical_pdf_and_durable_attachment_reference() -> None:
    handler = _source("app/services/events/handlers/notification.py")
    resolver = _source("app/services/communication_attachments.py")
    intents = _source("app/services/communication_intents.py")

    assert "CommunicationAttachmentKind.invoice_pdf" in handler
    assert "billing_invoice_pdf.generate_export_now" in resolver
    assert "billing_invoice_pdf.stream_export" in resolver
    assert '"attachments": attachment_metadata' in intents


def test_invoice_email_attachment_fails_closed_and_routes_bytes_only_to_smtp() -> None:
    resolver = _source("app/services/communication_attachments.py")
    worker = _source("app/tasks/notifications.py")
    email = _source("app/services/email.py")

    assert "invoice_attachment_scope_mismatch" in resolver
    assert "invoice_pdf_generation_failed" in resolver
    assert "resolve_email_attachments" in worker
    assert 'MIMEMultipart("mixed" if attachments else "alternative")' in email
    assert '"Content-Disposition"' in email
    assert '"attachment"' in email
