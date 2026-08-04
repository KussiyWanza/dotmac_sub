"""Architecture guards for Quote document, delivery, and activity ownership."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_quote_document_and_delivery_are_registered_owner_commands():
    documents = _source("app/services/sales/quote_documents.py")
    delivery = _source("app/services/sales/quote_delivery.py")
    registry = _source("app/services/sot_relationships.py")

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
    assert 'name="sales.quote_documents"' in registry
    assert 'name="sales.quote_delivery"' in registry
    assert 'name="ui.quote_detail_projection"' in registry


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
