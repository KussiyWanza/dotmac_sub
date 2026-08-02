"""Incident-ticket link composition (OUTAGE_SLA_SPINE ticket slice).

One canonical infrastructure ticket, deduplicated complaint links,
reconciliation state transitions, scope-revision context, and the invariant
that network recovery never transitions a ticket.
"""

from __future__ import annotations

import pytest

from app.models.catalog import NasDevice
from app.models.network_monitoring import NetworkDevice
from app.models.support import Ticket
from app.services.topology.outage import declare_outage, resolve_outage
from app.services.topology.outage_tickets import (
    infrastructure_link_for,
    link_complaint_ticket,
    link_infrastructure_ticket,
    links_for_incident,
    mark_reconciliation,
)


def _node(db):
    nas = NasDevice(name="NAS-TL", management_ip="10.6.0.1")
    db.add(nas)
    db.flush()
    node = NetworkDevice(
        name="tl-node",
        matched_device_type="nas",
        matched_device_id=nas.id,
        is_active=True,
    )
    db.add(node)
    db.flush()
    return node


def _ticket(db, title):
    ticket = Ticket(title=title, status="open")
    db.add(ticket)
    db.flush()
    return ticket


def test_one_canonical_infrastructure_ticket(db_session, catalog_offer):
    node = _node(db_session)
    incident = declare_outage(db_session, node=node)
    ticket_a = _ticket(db_session, "Fiber cut — Gudu feeder")
    ticket_b = _ticket(db_session, "Duplicate infra ticket")

    link = link_infrastructure_ticket(
        db_session, incident, ticket_a.id, linked_by="noc@x"
    )
    assert link.role == "infrastructure"
    assert link.reconciliation_state == "native"
    # The link binds to the scope revision it was made under.
    assert link.scope_revision_sequence == 1

    # Idempotent for the same ticket.
    again = link_infrastructure_ticket(
        db_session, incident, ticket_a.id, linked_by="noc@x"
    )
    assert again.id == link.id

    # A different canonical ticket is an explicit reviewed operation.
    with pytest.raises(ValueError, match="already has canonical"):
        link_infrastructure_ticket(db_session, incident, ticket_b.id, linked_by="noc@x")

    found = infrastructure_link_for(db_session, incident.id)
    assert found is not None and found.ticket_id == ticket_a.id


def test_complaint_links_deduplicate_per_pair(db_session, catalog_offer):
    node = _node(db_session)
    incident = declare_outage(db_session, node=node)
    complaint = _ticket(db_session, "No internet in Garki")

    first = link_complaint_ticket(
        db_session, incident, complaint.id, linked_by="agent@x"
    )
    second = link_complaint_ticket(
        db_session, incident, complaint.id, linked_by="agent@x"
    )

    assert first.id == second.id
    assert [link.role for link in links_for_incident(db_session, incident.id)] == [
        "complaint"
    ]


def test_reconciliation_states_are_typed(db_session, catalog_offer):
    node = _node(db_session)
    incident = declare_outage(db_session, node=node)
    ticket = _ticket(db_session, "Infra")
    link = link_infrastructure_ticket(
        db_session, incident, ticket.id, linked_by="noc@x"
    )

    mark_reconciliation(db_session, link, state="pending")
    mark_reconciliation(db_session, link, state="synced", external_ref="CRM-123")
    assert link.reconciliation_state == "synced"
    assert link.external_ref == "CRM-123"

    with pytest.raises(ValueError, match="unknown reconciliation state"):
        mark_reconciliation(db_session, link, state="closed")


def test_network_recovery_never_transitions_the_ticket(db_session, catalog_offer):
    node = _node(db_session)
    incident = declare_outage(db_session, node=node)
    ticket = _ticket(db_session, "Infra work")
    link_infrastructure_ticket(db_session, incident, ticket.id, linked_by="noc@x")

    resolve_outage(db_session, incident.id)

    db_session.refresh(ticket)
    assert ticket.status == "open"
    # The link survives resolution as history.
    assert infrastructure_link_for(db_session, incident.id) is not None
