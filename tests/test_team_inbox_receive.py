from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.models.service_team import ServiceTeam, ServiceTeamMember, ServiceTeamType
from app.models.team_inbox import (
    InboxAgentPresence,
    InboxAgentPresenceStatus,
    InboxConversation,
    InboxConversationAssignment,
    InboxConversationTeam,
    InboxMessage,
    InboxTeamRole,
    TeamInboxEmailRoute,
)
from app.services import team_inbox_receive
from tests.staff_identity_fixtures import add_bound_staff_user


def _team(db_session, name: str, team_type: str) -> ServiceTeam:
    team = ServiceTeam(name=name, team_type=team_type)
    db_session.add(team)
    db_session.flush()
    return team


def _route(
    db_session,
    team: ServiceTeam,
    email: str,
    *,
    priority: int = 100,
    metadata: dict | None = None,
) -> None:
    db_session.add(
        TeamInboxEmailRoute(
            service_team_id=team.id,
            email_address=email.lower(),
            priority=priority,
            is_active=True,
            metadata_=metadata,
        )
    )
    db_session.flush()


def test_receive_inbound_email_creates_one_thread_for_multiple_teams(db_session):
    support = _team(db_session, "Support", ServiceTeamType.support.value)
    billing = _team(db_session, "Finance", ServiceTeamType.billing.value)
    field = _team(db_session, "Field", ServiceTeamType.field_service.value)
    _route(db_session, support, "support@dotmac.io", priority=10)
    _route(db_session, billing, "billing@dotmac.io", priority=20)
    _route(db_session, field, "field@dotmac.io", priority=30)
    db_session.commit()

    result = team_inbox_receive.receive_inbound_email(
        db_session,
        team_inbox_receive.InboundEmailPayload(
            from_address="Customer <customer@example.com>",
            to_addresses=["support@dotmac.io", "billing@dotmac.io"],
            cc_addresses=["field@dotmac.io"],
            subject="Install and invoice question",
            body="Please help.",
            message_id="<msg-1@example.com>",
            received_at=datetime(2026, 7, 10, tzinfo=UTC),
        ),
    )
    db_session.commit()

    conversation = db_session.get(InboxConversation, result.conversation_id)
    message = db_session.get(InboxMessage, result.message_id)
    links = (
        db_session.query(InboxConversationTeam)
        .filter(InboxConversationTeam.conversation_id == conversation.id)
        .all()
    )

    assert result.kind == "received"
    assert result.duplicate is False
    assert conversation.primary_service_team_id == support.id
    assert conversation.external_thread_id == "<msg-1@example.com>"
    assert message.from_address == "customer@example.com"
    assert message.to_addresses == ["support@dotmac.io", "billing@dotmac.io"]
    assert message.cc_addresses == ["field@dotmac.io"]
    assert {str(link.service_team_id) for link in links} == {
        str(support.id),
        str(billing.id),
        str(field.id),
    }
    assert [link for link in links if link.service_team_id == support.id][
        0
    ].role == InboxTeamRole.owner.value


def test_receive_inbound_email_auto_assigns_route_to_online_agent(db_session):
    support = _team(db_session, "Support", ServiceTeamType.support.value)
    user, person = add_bound_staff_user(db_session)
    db_session.add(
        ServiceTeamMember(team_id=support.id, person_id=person.id, is_active=True)
    )
    db_session.add(
        InboxAgentPresence(
            person_id=user.id,
            status=InboxAgentPresenceStatus.online.value,
        )
    )
    _route(
        db_session,
        support,
        "support@dotmac.io",
        metadata={"inbox_auto_assign_to_agent": True},
    )
    db_session.commit()

    result = team_inbox_receive.receive_inbound_email(
        db_session,
        team_inbox_receive.InboundEmailPayload(
            from_address="customer@example.com",
            to_addresses=["support@dotmac.io"],
            subject="Help",
            body="Please assign this.",
            message_id="<assign-me@example.com>",
            received_at=datetime(2026, 7, 10, tzinfo=UTC),
        ),
    )
    db_session.commit()

    conversation = db_session.get(InboxConversation, result.conversation_id)
    assignment = db_session.query(InboxConversationAssignment).one()
    assert conversation.primary_service_team_id == support.id
    assert assignment.person_id == user.id
    assert assignment.service_team_id == support.id
    assert assignment.is_active is True
    assert assignment.metadata_["source"] == "routing_rule"
    assert assignment.metadata_["reason"] == "inbound_email_route_auto_assign"


def test_receive_inbound_email_deduplicates_by_message_id(db_session):
    support = _team(db_session, "Support", ServiceTeamType.support.value)
    _route(db_session, support, "support@dotmac.io")
    db_session.commit()
    payload = team_inbox_receive.InboundEmailPayload(
        from_address="customer@example.com",
        to_addresses=["support@dotmac.io"],
        subject="Help",
        body="First copy",
        message_id="<duplicate@example.com>",
    )

    first = team_inbox_receive.receive_inbound_email(db_session, payload)
    second = team_inbox_receive.receive_inbound_email(db_session, payload)
    db_session.commit()

    assert first.kind == "received"
    assert second.kind == "duplicate"
    assert second.duplicate is True
    assert second.conversation_id == first.conversation_id
    assert db_session.query(InboxConversation).count() == 1
    assert db_session.query(InboxMessage).count() == 1


def test_receive_inbound_email_reply_reuses_referenced_thread(db_session):
    support = _team(db_session, "Support", ServiceTeamType.support.value)
    billing = _team(db_session, "Finance", ServiceTeamType.billing.value)
    _route(db_session, support, "support@dotmac.io")
    _route(db_session, billing, "billing@dotmac.io")
    db_session.commit()
    first = team_inbox_receive.receive_inbound_email(
        db_session,
        team_inbox_receive.InboundEmailPayload(
            from_address="customer@example.com",
            to_addresses=["support@dotmac.io"],
            subject="Original",
            body="Original",
            message_id="<original@example.com>",
            received_at=datetime(2026, 7, 10, tzinfo=UTC),
        ),
    )
    reply = team_inbox_receive.receive_inbound_email(
        db_session,
        team_inbox_receive.InboundEmailPayload(
            from_address="customer@example.com",
            to_addresses=["billing@dotmac.io"],
            subject="Re: Original",
            body="Adding billing.",
            message_id="<reply@example.com>",
            in_reply_to="<original@example.com>",
            received_at=datetime(2026, 7, 10, tzinfo=UTC) + timedelta(minutes=5),
        ),
    )
    db_session.commit()

    conversation = db_session.get(InboxConversation, first.conversation_id)
    links = (
        db_session.query(InboxConversationTeam)
        .filter(InboxConversationTeam.conversation_id == conversation.id)
        .all()
    )

    assert reply.conversation_id == first.conversation_id
    assert db_session.query(InboxConversation).count() == 1
    assert db_session.query(InboxMessage).count() == 2
    assert conversation.primary_service_team_id == support.id
    assert {str(link.service_team_id) for link in links} == {
        str(support.id),
        str(billing.id),
    }
    assert {(str(link.service_team_id), link.role) for link in links} == {
        (str(support.id), "owner"),
        (str(billing.id), "participant"),
    }


def test_receive_inbound_email_uses_fallback_team_for_unmatched_recipient(db_session):
    support = _team(db_session, "Support", ServiceTeamType.support.value)
    db_session.commit()

    result = team_inbox_receive.receive_inbound_email(
        db_session,
        team_inbox_receive.InboundEmailPayload(
            from_address="customer@example.com",
            to_addresses=["unknown@dotmac.io"],
            subject="Unknown mailbox",
            message_id="<unknown@example.com>",
            fallback_service_team_id=support.id,
        ),
    )
    db_session.commit()

    conversation = db_session.get(InboxConversation, result.conversation_id)

    assert conversation.primary_service_team_id == support.id
    assert conversation.team_links[0].service_team_id == support.id
    assert conversation.messages[0].metadata_["routing"]["unmatched_recipients"] == [
        "unknown@dotmac.io"
    ]
