from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.responses import StreamingResponse

from app.models.stored_file import StoredFile
from app.models.support import Ticket, TicketComment
from app.services import crm_portal, web_support_tickets
from app.services.file_storage import file_uploads
from app.services.object_storage import StreamResult
from app.web.admin import support_tickets


def _stored_ticket_file(
    db_session,
    ticket: Ticket,
    *,
    content_type: str = "image/jpeg",
    entity_type: str = "support_ticket_attachment",
) -> StoredFile:
    record = StoredFile(
        entity_type=entity_type,
        entity_id=str(ticket.id),
        original_filename="evidence.jpg",
        storage_key_or_relative_path=f"attachments/{ticket.id}/evidence.jpg",
        file_size=4,
        content_type=content_type,
        storage_provider="s3",
    )
    db_session.add(record)
    db_session.commit()
    db_session.refresh(record)
    return record


def test_ticket_attachment_is_scoped_and_images_render_inline(
    db_session, monkeypatch
) -> None:
    ticket = Ticket(title="Attachment ticket")
    other_ticket = Ticket(title="Other ticket")
    db_session.add_all([ticket, other_ticket])
    db_session.commit()
    record = _stored_ticket_file(db_session, ticket)
    monkeypatch.setattr(
        file_uploads,
        "stream_file",
        lambda _record: StreamResult(iter([b"data"]), "image/jpeg", 4),
    )

    response = support_tickets.ticket_attachment_download(
        ticket.id, record.id, db_session
    )

    assert isinstance(response, StreamingResponse)
    assert response.headers["content-disposition"].startswith("inline;")
    with pytest.raises(HTTPException) as exc:
        support_tickets.ticket_attachment_download(
            other_ticket.id, record.id, db_session
        )
    assert exc.value.status_code == 404


def test_ticket_attachment_rejects_unrelated_file_types(db_session) -> None:
    ticket = Ticket(title="Attachment ticket")
    db_session.add(ticket)
    db_session.commit()
    record = _stored_ticket_file(db_session, ticket, entity_type="project_attachment")

    assert (
        web_support_tickets.get_ticket_attachment_file(
            db_session, ticket_id=ticket.id, file_id=record.id
        )
        is None
    )


def test_customer_attachment_access_excludes_internal_comments(db_session) -> None:
    ticket = Ticket(title="Attachment ticket")
    public_comment = TicketComment(ticket=ticket, body="Public", is_internal=False)
    internal_comment = TicketComment(ticket=ticket, body="Private", is_internal=True)
    db_session.add_all([ticket, public_comment, internal_comment])
    db_session.commit()
    public_file = _stored_ticket_file(
        db_session, ticket, entity_type="support_ticket_comment_attachment"
    )
    internal_file = _stored_ticket_file(
        db_session, ticket, entity_type="support_ticket_comment_attachment"
    )
    public_comment.attachments = [{"stored_file_id": str(public_file.id)}]
    internal_comment.attachments = [{"stored_file_id": str(internal_file.id)}]
    db_session.commit()

    assert (
        web_support_tickets.get_customer_visible_ticket_attachment_file(
            db_session, ticket_id=ticket.id, file_id=public_file.id
        )
        == public_file
    )
    assert (
        web_support_tickets.get_customer_visible_ticket_attachment_file(
            db_session, ticket_id=ticket.id, file_id=internal_file.id
        )
        is None
    )


def test_portal_projection_keeps_ticket_and_comment_attachments(db_session) -> None:
    ticket_file_id = str(uuid.uuid4())
    comment_file_id = str(uuid.uuid4())
    ticket = Ticket(
        title="Attachment ticket",
        description_is_internal=False,
        attachments=[{"file_name": "ticket.pdf", "stored_file_id": ticket_file_id}],
    )
    comment = TicketComment(
        ticket=ticket,
        body="See the image",
        attachments=[{"file_name": "comment.jpg", "stored_file_id": comment_file_id}],
    )
    db_session.add_all([ticket, comment])
    db_session.commit()

    assert (
        crm_portal._ticket_to_dict(ticket)["attachments"][0]["stored_file_id"]
        == ticket_file_id
    )
    assert (
        crm_portal._comment_to_dict(comment, set())["attachments"][0]["stored_file_id"]
        == comment_file_id
    )


def test_customer_attachment_access_excludes_internal_description(db_session) -> None:
    ticket = Ticket(title="Private attachment", description_is_internal=True)
    db_session.add(ticket)
    db_session.commit()
    record = _stored_ticket_file(
        db_session, ticket, entity_type="support_ticket_attachment"
    )
    ticket.attachments = [{"stored_file_id": str(record.id)}]
    db_session.commit()

    assert (
        web_support_tickets.get_customer_visible_ticket_attachment_file(
            db_session, ticket_id=ticket.id, file_id=record.id
        )
        is None
    )


def test_ticket_templates_link_scoped_attachments() -> None:
    admin_components = Path(
        "templates/admin/support/tickets/_components.html"
    ).read_text()
    customer_detail = Path("templates/customer/support/detail.html").read_text()

    assert "/admin/support/tickets/{{ ticket_id }}/attachments/" in admin_components
    assert "/portal/support/{{ ticket.get('id') }}/attachments/" in customer_detail
