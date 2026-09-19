"""Whitespace handling for support ticket/comment input schemas.

Regression for #20: ``min_length=1`` alone let whitespace-only titles/bodies
through (length >= 1), creating blank-titled tickets. The schemas now strip
surrounding whitespace so blank input fails validation and good input is
trimmed.
"""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.models.support import TicketCommentAuthorType
from app.schemas.support import (
    MySupportCommentCreate,
    MySupportTicketCreate,
    TicketCommentCreate,
    TicketCommentRead,
    TicketCreate,
)


@pytest.mark.parametrize("blank", ["", "   ", "\t\n "])
def test_ticket_create_rejects_blank_or_whitespace_title(blank):
    with pytest.raises(ValidationError):
        TicketCreate(subscriber_id=uuid4(), title=blank, description="x")


def test_ticket_create_trims_title():
    ticket = TicketCreate(subscriber_id=uuid4(), title="  Real  ", description="x")
    assert ticket.title == "Real"


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_ticket_comment_rejects_blank_body(blank):
    with pytest.raises(ValidationError):
        TicketCommentCreate(body=blank)


def test_staff_ticket_comment_defaults_internal():
    payload = TicketCommentCreate(body="Staff-only note")

    assert payload.is_internal is True


@pytest.mark.parametrize("author_type", list(TicketCommentAuthorType))
def test_ticket_comment_read_keeps_author_type_typed(author_type):
    comment = TicketCommentRead.model_validate(
        {
            "id": uuid4(),
            "ticket_id": uuid4(),
            "author_person_id": None,
            "author_type": author_type.value,
            "author_system_user_id": None,
            "body": "Visible reply",
            "is_internal": False,
            "created_at": "2026-09-14T12:00:00Z",
        }
    )

    assert comment.author_type is author_type


def test_ticket_comment_read_rejects_unknown_author_type():
    with pytest.raises(ValidationError):
        TicketCommentRead.model_validate(
            {
                "id": uuid4(),
                "ticket_id": uuid4(),
                "author_person_id": None,
                "author_type": "operator-shaped-guess",
                "author_system_user_id": None,
                "body": "Visible reply",
                "is_internal": False,
                "created_at": "2026-09-14T12:00:00Z",
            }
        )


@pytest.mark.parametrize("blank", ["   ", "\n\t"])
def test_my_support_schemas_reject_whitespace(blank):
    with pytest.raises(ValidationError):
        MySupportTicketCreate(title=blank)
    with pytest.raises(ValidationError):
        MySupportCommentCreate(body=blank)
