from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api import me as customer_api
from app.models.csat import CsatRequestStatus, CsatSourceType, SupportCsatRequest
from app.models.service_team import ServiceTeam, ServiceTeamType
from app.models.subscriber import Subscriber
from app.models.support import Ticket, TicketStatus
from app.models.team_inbox import (
    InboxConversation,
    InboxConversationAssignment,
    InboxConversationStatus,
)
from app.schemas.support import TicketSatisfactionRequest, TicketUpdate
from app.services import support, support_csat, team_inbox_commands
from app.services.domain_errors import DomainError
from app.web.admin import reports as report_routes


def _ticket(
    db_session,
    subscriber: Subscriber,
    *,
    status: str = TicketStatus.open.value,
    assigned_to_person_id=None,
    service_team_id=None,
) -> Ticket:
    ticket = Ticket(
        subscriber_id=subscriber.id,
        customer_account_id=subscriber.id,
        title="Slow speeds",
        status=status,
        assigned_to_person_id=assigned_to_person_id,
        service_team_id=service_team_id,
    )
    db_session.add(ticket)
    db_session.commit()
    db_session.refresh(ticket)
    return ticket


def _close_ticket(db_session, ticket: Ticket) -> Ticket:
    return support.Tickets.update(
        db_session,
        str(ticket.id),
        TicketUpdate(status=TicketStatus.closed),
        actor_id=str(uuid4()),
    )


def test_direct_ticket_closure_creates_one_csat_request(db_session, subscriber):
    ticket = _ticket(db_session, subscriber)

    closed = _close_ticket(db_session, ticket)
    repeated = support.Tickets.update(
        db_session,
        str(ticket.id),
        TicketUpdate(title="Still closed"),
        actor_id=str(uuid4()),
    )

    rows = db_session.query(SupportCsatRequest).all()
    assert closed.status == TicketStatus.closed.value
    assert repeated.status == TicketStatus.closed.value
    assert len(rows) == 1
    assert rows[0].source_type == CsatSourceType.support_ticket.value
    assert rows[0].source_id == ticket.id
    assert rows[0].customer_id == subscriber.id
    assert rows[0].status == CsatRequestStatus.pending.value


def test_resolution_confirmation_closure_creates_csat_request(db_session, subscriber):
    ticket = _ticket(db_session, subscriber)
    _pending, token = support.Tickets.request_resolution_confirmation(
        db_session,
        str(ticket.id),
        actor_id=str(uuid4()),
    )

    support.Tickets.confirm_resolution(db_session, token)

    row = db_session.query(SupportCsatRequest).one()
    assert row.source_type == CsatSourceType.support_ticket.value
    assert row.source_id == ticket.id
    assert row.resolution_at is not None


def test_customer_submit_rating_is_single_use_and_preserves_metadata(
    db_session, subscriber
):
    ticket = _close_ticket(db_session, _ticket(db_session, subscriber))

    rated = support.Tickets.set_satisfaction(
        db_session,
        ticket,
        rating=5,
        comment="Great help",
    )

    row = db_session.query(SupportCsatRequest).one()
    assert row.rating == 5
    assert row.comment == "Great help"
    assert row.status == CsatRequestStatus.submitted.value
    assert rated.metadata_["csat"]["request_id"] == str(row.id)
    with pytest.raises(DomainError) as exc:
        support.Tickets.set_satisfaction(db_session, ticket, rating=4)
    assert "already_submitted" in exc.value.code


def test_customer_cannot_rate_another_customers_ticket(db_session, subscriber):
    other = Subscriber(
        first_name="Other",
        last_name="User",
        email=f"other-{uuid4().hex}@example.com",
    )
    db_session.add(other)
    db_session.commit()
    ticket = _close_ticket(db_session, _ticket(db_session, subscriber))

    with pytest.raises(HTTPException) as exc:
        customer_api.my_rate_ticket(
            str(ticket.id),
            TicketSatisfactionRequest(rating=5, comment="nope"),
            db_session,
            {"principal_type": "subscriber", "subscriber_id": str(other.id)},
        )

    assert exc.value.status_code == 404
    assert db_session.query(SupportCsatRequest).filter_by(rating=5).count() == 0


def test_reopened_ticket_reresolution_creates_second_csat_cycle(db_session, subscriber):
    ticket = _close_ticket(db_session, _ticket(db_session, subscriber))
    support.Tickets.set_satisfaction(db_session, ticket, rating=4, comment="first")

    support.Tickets.update(
        db_session,
        str(ticket.id),
        TicketUpdate(status=TicketStatus.open),
        actor_id=str(uuid4()),
    )
    support.Tickets.update(
        db_session,
        str(ticket.id),
        TicketUpdate(status=TicketStatus.closed),
        actor_id=str(uuid4()),
    )

    rows = (
        db_session.query(SupportCsatRequest)
        .order_by(SupportCsatRequest.requested_at.asc())
        .all()
    )
    assert len(rows) == 2
    assert rows[0].status == CsatRequestStatus.submitted.value
    assert rows[0].rating == 4
    assert rows[1].status == CsatRequestStatus.pending.value
    assert rows[0].resolution_cycle_key != rows[1].resolution_cycle_key


def test_agent_and_team_snapshot_survives_reassignment(db_session, subscriber):
    agent_id = uuid4()
    team = ServiceTeam(
        name="Support CSAT Team", team_type=ServiceTeamType.support.value
    )
    db_session.add(team)
    db_session.commit()
    ticket = _ticket(
        db_session,
        subscriber,
        assigned_to_person_id=agent_id,
        service_team_id=team.id,
    )

    _close_ticket(db_session, ticket)
    ticket.assigned_to_person_id = uuid4()
    ticket.service_team_id = None
    db_session.commit()

    row = db_session.query(SupportCsatRequest).one()
    assert row.agent_person_id == agent_id
    assert row.service_team_id == team.id
    assert row.service_team_name == "Support CSAT Team"


def test_csat_creation_failure_does_not_prevent_ticket_closure(
    monkeypatch, db_session, subscriber
):
    ticket = _ticket(db_session, subscriber)
    monkeypatch.setattr(
        support_csat,
        "ensure_ticket_request",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    closed = _close_ticket(db_session, ticket)

    assert closed.status == TicketStatus.closed.value


def test_csat_notification_failure_keeps_request(monkeypatch, db_session, subscriber):
    ticket = _ticket(db_session, subscriber)
    monkeypatch.setattr(
        support_csat,
        "queue_request_notification",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("notify")),
    )

    closed = _close_ticket(db_session, ticket)

    assert closed.status == TicketStatus.closed.value
    assert db_session.query(SupportCsatRequest).count() == 1


def test_inbox_resolution_creates_one_request_and_reresolution_creates_second(
    db_session, subscriber
):
    team = ServiceTeam(name="Inbox CSAT Team", team_type=ServiceTeamType.support.value)
    conversation = InboxConversation(
        channel_type="chat_widget",
        status=InboxConversationStatus.open.value,
        subscriber_id=subscriber.id,
        primary_service_team_id=team.id,
    )
    db_session.add_all([team, conversation])
    db_session.flush()
    agent_id = uuid4()
    db_session.add(
        InboxConversationAssignment(
            conversation_id=conversation.id,
            service_team_id=team.id,
            person_id=agent_id,
            assigned_at=datetime.now(UTC),
            is_active=True,
        )
    )
    db_session.commit()

    changed = team_inbox_commands.update_status(
        db_session,
        conversation_id=conversation.id,
        status_value=InboxConversationStatus.resolved.value,
        actor_person_id=agent_id,
    )
    repeated = team_inbox_commands.update_status(
        db_session,
        conversation_id=conversation.id,
        status_value=InboxConversationStatus.resolved.value,
        actor_person_id=agent_id,
    )
    team_inbox_commands.update_status(
        db_session,
        conversation_id=conversation.id,
        status_value=InboxConversationStatus.open.value,
        actor_person_id=agent_id,
    )
    team_inbox_commands.update_status(
        db_session,
        conversation_id=conversation.id,
        status_value=InboxConversationStatus.resolved.value,
        actor_person_id=agent_id,
    )

    rows = db_session.query(SupportCsatRequest).all()
    assert changed.already_set is False
    assert repeated.already_set is True
    assert len(rows) == 2
    assert {row.source_type for row in rows} == {
        CsatSourceType.inbox_conversation.value
    }
    assert all(row.agent_person_id == agent_id for row in rows)
    assert rows[0].resolution_cycle_key != rows[1].resolution_cycle_key


def test_inbox_satisfaction_requires_pending_cycle(db_session, subscriber):
    conversation = InboxConversation(
        channel_type="chat_widget",
        status=InboxConversationStatus.resolved.value,
        subscriber_id=subscriber.id,
    )
    db_session.add(conversation)
    db_session.commit()

    support_csat.submit_inbox_rating(
        db_session,
        conversation,
        rating=5,
        comment="good",
        submitted_by="visitor",
    )
    with pytest.raises(DomainError) as exc:
        support_csat.submit_inbox_rating(
            db_session,
            conversation,
            rating=4,
            submitted_by="visitor",
        )
    assert "already_submitted" in exc.value.code


def test_csat_report_filters_and_summary(db_session, subscriber):
    ticket = _close_ticket(db_session, _ticket(db_session, subscriber))
    support.Tickets.set_satisfaction(db_session, ticket, rating=5, comment="great")
    query = support_csat.CsatReportQuery(rating=5)

    rows = support_csat.report_rows(db_session, query)
    summary = support_csat.report_summary(db_session, query)

    assert len(rows) == 1
    assert rows[0].comment == "great"
    assert summary.submitted == 1
    assert summary.rating_counts[5] == 1


def test_csat_report_hub_and_row_links(db_session, subscriber):
    ticket = _close_ticket(db_session, _ticket(db_session, subscriber))
    support.Tickets.set_satisfaction(db_session, ticket, rating=5, comment="great")
    rows = report_routes._csat_report_rows(  # noqa: SLF001 - route projection contract
        support_csat.report_rows(db_session, support_csat.CsatReportQuery())
    )
    links = {
        link["name"]: link
        for section in report_routes.REPORT_HUB_SECTIONS
        for link in section["links"]
    }

    assert links["Support CSAT"]["url"] == "/admin/reports/support-csat"
    assert links["Support CSAT"]["permission"] == "reports:support:read"
    assert rows[0]["source_url"] == f"/admin/support/tickets/{ticket.id}"
    assert rows[0]["rating"] == 5
