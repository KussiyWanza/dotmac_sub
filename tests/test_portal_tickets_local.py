"""Customer portal tickets are served by the internal (local) ticket module.

The portal previously depended on an external CRM (unconfigured here), so no
customer could open or view tickets. These flows now use the local
support.Tickets / TicketComments service so the portal works standalone.
"""

import uuid
from unittest.mock import patch

from app.models.audit import AuditEvent
from app.models.event_store import EventStatus, EventStore
from app.models.sequence import DocumentSequence
from app.models.service_team import ServiceTeam, ServiceTeamType
from app.models.support import Ticket, TicketChannel, TicketCommentAuthorType
from app.schemas.support import TicketCommentCreate
from app.services import crm_portal
from app.services import support as support_service
from app.services import support_ticket_settings as support_ticket_settings_service


def _portal_create(
    db_session,
    subscriber_id: str,
    title: str,
    description: str,
    priority: str,
    *,
    region: str = "north",
):
    return crm_portal.handle_ticket_create(
        db_session,
        {},
        subscriber_id,
        title,
        description,
        priority,
        region,
        support_ticket_settings_service.resolve_portal_ticket_team_routing(db_session),
    )


def test_create_uses_local_ticket_module(db_session, subscriber):
    customer_experience = ServiceTeam(
        name="Customer Experience",
        team_type=ServiceTeamType.support.value,
        is_active=True,
    )
    system_admin = ServiceTeam(
        name="System Admin",
        team_type=ServiceTeamType.support.value,
        is_active=True,
    )
    db_session.add_all([customer_experience, system_admin])
    db_session.commit()

    result = _portal_create(
        db_session,
        str(subscriber.id),
        "Internet down",
        "No light on the ONT",
        "high",
    )
    assert result["success"] is True, result
    ticket = result["ticket"]
    assert ticket["title"] == "Internet down"
    assert ticket["subscriber_id"] == str(subscriber.id)
    # persisted in the local support_tickets table
    stored = db_session.get(Ticket, uuid.UUID(ticket["id"]))
    assert stored is not None
    assert stored.region == "north"
    assert stored.service_team_id == customer_experience.id
    event = (
        db_session.query(EventStore)
        .filter(
            EventStore.event_type == "custom",
            EventStore.payload["ticket_id"].as_string() == ticket["id"],
        )
        .one()
    )
    assert event.payload["region"] == "north"
    assert event.payload["service_team_id"] == str(customer_experience.id)
    assert event.payload["creation_routing_mode"] == "preserve_requested_team"
    audit = (
        db_session.query(AuditEvent)
        .filter(
            AuditEvent.action == "create",
            AuditEvent.entity_type == "support_ticket",
            AuditEvent.entity_id == ticket["id"],
        )
        .one()
    )
    assert audit.metadata_["region"] == "north"
    assert audit.metadata_["service_team_id"] == str(customer_experience.id)
    assert audit.metadata_["creation_routing_mode"] == "preserve_requested_team"


def test_create_remains_unassigned_when_portal_fallbacks_are_unavailable(
    db_session, subscriber
):
    inactive = ServiceTeam(
        name="Customer Experience",
        team_type=ServiceTeamType.support.value,
        is_active=False,
    )
    partial = ServiceTeam(
        name="Admin",
        team_type=ServiceTeamType.support.value,
        is_active=True,
    )
    db_session.add_all([inactive, partial])
    db_session.commit()

    result = _portal_create(
        db_session,
        str(subscriber.id),
        "No matching team",
        "Creation must remain available",
        "normal",
    )

    assert result["success"] is True, result
    stored = db_session.get(Ticket, uuid.UUID(result["ticket"]["id"]))
    assert stored is not None
    assert stored.service_team_id is None


def test_create_uses_system_admin_when_customer_experience_is_inactive(
    db_session, subscriber
):
    inactive = ServiceTeam(
        name="Customer Experience",
        team_type=ServiceTeamType.support.value,
        is_active=False,
    )
    system_admin = ServiceTeam(
        name="System Admin",
        team_type=ServiceTeamType.support.value,
        is_active=True,
    )
    db_session.add_all([inactive, system_admin])
    db_session.commit()

    result = _portal_create(
        db_session,
        str(subscriber.id),
        "Fallback team",
        "Customer Experience is unavailable",
        "normal",
    )

    assert result["success"] is True, result
    stored = db_session.get(Ticket, uuid.UUID(result["ticket"]["id"]))
    assert stored is not None
    assert stored.service_team_id == system_admin.id


def test_create_advances_past_an_existing_imported_ticket_number(
    db_session, subscriber
):
    db_session.add(
        Ticket(
            subscriber_id=subscriber.id,
            number="1",
            title="Imported ticket",
            description="Existing external identity",
            channel=TicketChannel.api,
        )
    )
    db_session.add(DocumentSequence(key="support_ticket", next_value=1))
    db_session.commit()

    result = _portal_create(
        db_session,
        str(subscriber.id),
        "New request",
        "Please investigate",
        "normal",
    )

    assert result["success"] is True, result
    assert result["ticket"]["ticket_number"] == "2"


def test_create_does_not_dispatch_integrations_in_customer_request(
    db_session, subscriber
):
    """A slow broker must not turn a successful ticket write into a 504."""
    with patch(
        "app.services.events.dispatcher.EventDispatcher.dispatch_pending_event"
    ) as dispatch:
        result = _portal_create(
            db_session,
            str(subscriber.id),
            "Portal should return promptly",
            "",
            "normal",
        )

    assert result["success"] is True, result
    dispatch.assert_not_called()
    event = (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "custom")
        .order_by(EventStore.created_at.desc())
        .first()
    )
    assert event is not None
    assert event.status == EventStatus.pending


def test_list_and_detail_round_trip(db_session, subscriber):
    _portal_create(db_session, str(subscriber.id), "Slow speeds", "details", "normal")
    ctx = crm_portal.tickets_list_context(None, db_session, {}, [str(subscriber.id)])
    assert len(ctx["tickets"]) == 1
    tid = ctx["tickets"][0]["id"]

    detail = crm_portal.ticket_detail_context(
        None, db_session, {}, [str(subscriber.id)], tid
    )
    assert detail["ticket"] is not None
    assert detail["ticket"]["id"] == tid


def test_detail_enforces_ownership(db_session, subscriber):
    res = _portal_create(db_session, str(subscriber.id), "Mine", "d", "normal")
    tid = res["ticket"]["id"]
    # A different subscriber must not be able to view it.
    other = str(uuid.uuid4())
    detail = crm_portal.ticket_detail_context(None, db_session, {}, [other], tid)
    assert detail["ticket"] is None
    assert detail.get("crm_error") is True


def test_comment_round_trip(db_session, subscriber):
    res = _portal_create(db_session, str(subscriber.id), "Need help", "d", "normal")
    tid = res["ticket"]["id"]
    cres = crm_portal.handle_ticket_comment(
        db_session, {}, [str(subscriber.id)], tid, "Any update on this?"
    )
    assert cres["success"] is True, cres

    detail = crm_portal.ticket_detail_context(
        None, db_session, {}, [str(subscriber.id)], tid
    )
    assert any(c["body"] == "Any update on this?" for c in detail["comments"])
    # the customer's own comment shows as "You"
    assert detail["comments"][0]["author_name"] == "You"

    stored = support_service.TicketComments.list(db_session, tid)[0]
    assert stored.author_type == TicketCommentAuthorType.customer.value
    assert stored.author_person_id == subscriber.id


def test_customer_ticket_detail_labels_staff_comments_as_support_team(
    db_session, subscriber
):
    res = _portal_create(db_session, str(subscriber.id), "Need help", "d", "normal")
    tid = res["ticket"]["id"]
    ticket = support_service.Tickets.get(db_session, tid)
    support_service.TicketComments.create(
        db_session,
        ticket=ticket,
        payload=TicketCommentCreate(
            body="Staff reply",
            is_internal=False,
            author_type=TicketCommentAuthorType.staff,
        ),
        actor_id=None,
    )
    db_session.commit()

    detail = crm_portal.ticket_detail_context(
        None, db_session, {}, [str(subscriber.id)], tid
    )

    assert detail["comments"][0]["body"] == "Staff reply"
    assert detail["comments"][0]["author_name"] == "Support Team"


def test_comment_rejected_for_non_owner(db_session, subscriber):
    res = _portal_create(db_session, str(subscriber.id), "Owned", "d", "normal")
    tid = res["ticket"]["id"]
    cres = crm_portal.handle_ticket_comment(
        db_session, {}, [str(uuid.uuid4())], tid, "sneaky"
    )
    assert cres["success"] is False
