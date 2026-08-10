"""Ticket module flow on PostgreSQL: CRM pull → native decisions → re-pull
merge (blocker-4 semantics on a real JSONB column) → local terminal precedence.

The CRM transport is faked (FakeCrmClient); tickets, comments, tokens, the
metadata merge, and the status guards are the real services on a real
database. Sub is authoritative for the native decision steps; CRM provenance
does not create a second write owner.
"""

from __future__ import annotations

from uuid import uuid4

from app.models.support import Ticket, TicketComment
from app.services.crm_ticket_pull import pull_tickets
from app.services.support import (
    Tickets,
    transition_ticket_status,
)
from tests.test_crm_ticket_pull import FakeCrmClient


def _crm_fixture(subscriber_external_id: str):
    crm_subscriber_id = str(uuid4())
    crm_ticket_id = str(uuid4())
    ticket_payload = {
        "id": crm_ticket_id,
        "subscriber_id": crm_subscriber_id,
        "number": "30201",
        "title": "Intermittent drops",
        "description": "Fibre flapping at the pole",
        "status": "open",
        "priority": "normal",
        "channel": "phone",
        "is_active": True,
        "updated_at": "2026-07-16T10:00:00+00:00",
    }
    client = FakeCrmClient(
        tickets=[ticket_payload],
        subscribers={
            crm_subscriber_id: {
                "id": crm_subscriber_id,
                "external_system": "splynx",
                "external_id": subscriber_external_id,
            }
        },
        comments={
            crm_ticket_id: [
                {
                    "id": str(uuid4()),
                    "body": "Dispatched once already.",
                    "is_internal": False,
                    "attachments": [],
                }
            ]
        },
    )
    return client, ticket_payload, crm_ticket_id


def test_ticket_lifecycle_pull_native_decisions_merge(db_session, subscriber):
    subscriber.splynx_customer_id = 201
    db_session.commit()
    client, payload, crm_ticket_id = _crm_fixture("201")

    # 1. First pull: the CRM ticket lands with provenance metadata.
    result = pull_tickets(db_session, client=client)
    db_session.commit()
    assert result.created == 1
    ticket = db_session.query(Ticket).filter(Ticket.number == "30201").one()
    assert ticket.metadata_["crm_ticket_id"] == crm_ticket_id
    assert ticket.subscriber_id == subscriber.id
    ticket_id = str(ticket.id)
    db_session.commit()

    # 2. Native decisions on the pulled ticket (Sub-owned write posture).
    ticket, token = Tickets.request_resolution_confirmation(
        db_session, ticket_id, actor_id=None
    )
    assert ticket.status == "pending_confirmation"
    assert ticket.metadata_["resolution_confirmation"]["requested_at"]
    assert token.purpose == "resolution_confirm"

    transition_ticket_status(ticket, "closed", source="integration_flow")
    db_session.commit()
    ticket = Tickets.set_satisfaction(db_session, ticket, rating=5, comment="great")
    assert ticket.metadata_["csat"]["rating"] == 5

    # 3. CRM updates its side; the re-pull must merge on real JSONB —
    # sub-owned decisions survive, CRM-derived keys refresh.
    payload["priority"] = "high"
    payload["updated_at"] = "2026-07-16T12:00:00+00:00"
    result = pull_tickets(db_session, client=client)
    db_session.commit()
    assert result.updated == 1
    db_session.refresh(ticket)
    assert ticket.metadata_["csat"]["rating"] == 5
    assert ticket.metadata_["resolution_confirmation"]["requested_at"]
    assert ticket.metadata_["crm_updated_at"] == "2026-07-16T12:00:00+00:00"
    assert ticket.priority == "high"
    # Closed is terminal, so a CRM observation cannot reopen the native ticket.
    assert ticket.status == "closed"

    # 4. Local terminal precedence: a locally-closed ticket cannot be
    # reopened by a CRM pull.
    transition_ticket_status(ticket, "closed", source="integration_flow")
    db_session.commit()
    payload["updated_at"] = "2026-07-16T14:00:00+00:00"
    pull_tickets(db_session, client=client)
    db_session.commit()
    db_session.refresh(ticket)
    assert ticket.status == "closed"
    assert ticket.metadata_["crm_updated_at"] == "2026-07-16T14:00:00+00:00"
    assert ticket.metadata_["csat"]["rating"] == 5


def test_ticket_comments_idempotent_across_pulls(db_session, subscriber):
    subscriber.splynx_customer_id = 201
    db_session.commit()
    client, payload, _ = _crm_fixture("201")

    pull_tickets(db_session, client=client)
    payload["updated_at"] = "2026-07-16T11:00:00+00:00"
    pull_tickets(db_session, client=client)
    db_session.commit()

    ticket = db_session.query(Ticket).filter(Ticket.number == "30201").one()
    assert (
        db_session.query(TicketComment)
        .filter(TicketComment.ticket_id == ticket.id)
        .count()
        == 1
    )
