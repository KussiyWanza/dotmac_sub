from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4

from app.services import web_support_tickets


def test_support_ticket_attachments_use_subscriber_safe_uploaded_by(
    db_session, monkeypatch
):
    """Support ticket uploads keep file ownership out of subscriber scope."""
    captured: dict[str, object] = {}

    file_id = uuid4()

    def _fake_upload(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            id=file_id,
            original_filename=kwargs["original_filename"],
            content_type=kwargs["content_type"],
            file_size=len(kwargs["data"]),
            storage_key_or_relative_path="attachments/public/support_ticket/ticket-1/file.pdf",
        )

    monkeypatch.setattr(web_support_tickets.file_uploads, "stage_upload", _fake_upload)
    attachment = SimpleNamespace(
        filename="proof.pdf",
        content_type="application/pdf",
        file=BytesIO(b"%PDF-1.4 test"),
    )

    uploaded = web_support_tickets.upload_ticket_attachments.__wrapped__(
        db_session,
        ticket_id=uuid4(),
        attachments=[attachment],
        entity_type=web_support_tickets.TicketAttachmentEntityType.ticket,
        actor_id=None,
    )

    assert uploaded[0].stored_file_id == file_id
    assert captured["uploaded_by"] is None
    assert captured["owner_subscriber_id"] is None


def test_support_ticket_attachments_ignore_system_user_uploaded_by(
    db_session, monkeypatch
):
    captured: dict[str, object] = {}

    file_id = uuid4()

    def _fake_upload(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            id=file_id,
            original_filename=kwargs["original_filename"],
            content_type=kwargs["content_type"],
            file_size=len(kwargs["data"]),
            storage_key_or_relative_path="attachments/public/support_ticket_comment/ticket-2/file.pdf",
        )

    monkeypatch.setattr(web_support_tickets.file_uploads, "stage_upload", _fake_upload)
    attachment = SimpleNamespace(
        filename="evidence.pdf",
        content_type="application/pdf",
        file=BytesIO(b"%PDF-1.4 test"),
    )

    uploaded = web_support_tickets.upload_ticket_attachments.__wrapped__(
        db_session,
        ticket_id=uuid4(),
        attachments=[attachment],
        entity_type=web_support_tickets.TicketAttachmentEntityType.comment,
        actor_id=uuid4(),
    )

    assert uploaded[0].stored_file_id == file_id
    assert captured["uploaded_by"] is None
