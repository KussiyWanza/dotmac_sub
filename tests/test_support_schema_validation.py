"""Whitespace handling for support ticket/comment input schemas.

Regression for #20: ``min_length=1`` alone let whitespace-only titles/bodies
through (length >= 1), creating blank-titled tickets. The schemas now strip
surrounding whitespace so blank input fails validation and good input is
trimmed.
"""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.support import (
    MySupportCommentCreate,
    MySupportTicketCreate,
    TicketCommentCreate,
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


@pytest.mark.parametrize("blank", ["   ", "\n\t"])
def test_my_support_schemas_reject_whitespace(blank):
    with pytest.raises(ValidationError):
        MySupportTicketCreate(title=blank)
    with pytest.raises(ValidationError):
        MySupportCommentCreate(body=blank)
