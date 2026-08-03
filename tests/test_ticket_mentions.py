from __future__ import annotations

from uuid import uuid4

from app.models.notification import (
    Notification,
    NotificationChannel,
    NotificationStatus,
)
from app.models.service_team import ServiceTeam, ServiceTeamMember, ServiceTeamType
from app.models.system_user import SystemUser
from app.services import ticket_mentions, web_support_tickets
from app.services.web_support_tickets import _parse_mentions_payload
from tests.staff_identity_fixtures import add_bound_staff_user


def _system_user(db_session, *, email: str, active: bool = True) -> SystemUser:
    user, _person = add_bound_staff_user(
        db_session,
        email=email,
        is_active=active,
    )
    user.first_name = email.split("@", 1)[0]
    user.last_name = "Agent"
    user.display_name = email.split("@", 1)[0].title()
    return user


def test_list_ticket_mention_users_includes_active_users_and_groups(db_session):
    ticket_mentions._TICKET_MENTION_USERS_CACHE = None
    user = _system_user(db_session, email="field@example.com")
    inactive = _system_user(db_session, email="inactive@example.com", active=False)
    team = ServiceTeam(name="Field Ops", team_type=ServiceTeamType.support.value)
    db_session.add(team)
    db_session.flush()
    db_session.add(ServiceTeamMember(team_id=team.id, person_id=user.person_party_id))
    db_session.add(
        ServiceTeamMember(team_id=team.id, person_id=inactive.person_party_id)
    )
    db_session.commit()

    items = ticket_mentions.list_ticket_mention_users(db_session)
    ids = {item["id"] for item in items}

    assert f"person:{user.id}" in ids
    assert f"person:{inactive.id}" not in ids
    assert f"group:{team.id}" in ids


def test_resolve_mentions_expands_groups_and_filters_inactive(db_session):
    active = _system_user(db_session, email="active@example.com")
    inactive = _system_user(db_session, email="inactive-two@example.com", active=False)
    team = ServiceTeam(name="Dispatch", team_type=ServiceTeamType.support.value)
    db_session.add(team)
    db_session.flush()
    db_session.add(ServiceTeamMember(team_id=team.id, person_id=active.person_party_id))
    db_session.add(
        ServiceTeamMember(team_id=team.id, person_id=inactive.person_party_id)
    )
    db_session.commit()

    resolved = ticket_mentions.resolve_mentioned_person_ids(
        db_session,
        [f"group:{team.id}", f"person:{active.id}", f"person:{inactive.id}", "bad"],
    )

    assert resolved == [str(active.id)]


def test_notify_ticket_comment_mentions_queues_absolute_link(db_session, monkeypatch):
    recipient = _system_user(db_session, email="mentioned@example.com")
    actor = _system_user(db_session, email="actor@example.com")
    db_session.commit()
    ticket_id = uuid4()
    monkeypatch.setattr(
        ticket_mentions,
        "get_brand",
        lambda: {"app_url": "https://sub.example.test/"},
    )

    ticket_mentions.notify_ticket_comment_mentions(
        db_session,
        ticket_id=str(ticket_id),
        ticket_number="TCK-1",
        ticket_title="Router swap",
        comment_preview="Please check this",
        mentioned_agent_ids=[f"person:{recipient.id}", f"person:{actor.id}"],
        actor_person_id=str(actor.id),
    )
    db_session.flush()

    rows = db_session.query(Notification).all()
    assert {row.channel for row in rows} == {
        NotificationChannel.push,
        NotificationChannel.email,
    }
    assert {row.recipient for row in rows} == {str(recipient.id), recipient.email}
    assert all(
        row.status in {NotificationStatus.delivered, NotificationStatus.queued}
        for row in rows
    )
    assert all("TCK-1" in (row.subject or "") for row in rows)
    assert all(
        f"Open: https://sub.example.test/admin/support/tickets/{ticket_id}"
        in (row.body or "")
        for row in rows
    )


def test_render_ticket_mention_message_omits_unsafe_public_url():
    ticket_id = uuid4()

    message = ticket_mentions.render_ticket_mention_message(
        ticket_mentions.TicketMentionMessageInput(
            ticket_id=ticket_id,
            ticket_number="TCK-2",
            ticket_title="Unsafe URL check",
            comment_preview="Please review",
            public_base_url="javascript:alert(1)",
        )
    )

    assert message.target_url is None
    assert "Open:" not in message.body


def test_parse_mentions_payload_accepts_strings_and_objects():
    parsed = _parse_mentions_payload(
        '[{"id":"person:one","label":"One"}, "group:two", {"id":"person:one"}]'
    )

    assert parsed == ["person:one", "group:two"]


def test_comment_form_preserves_person_and_group_mention_tokens(monkeypatch):
    captured = {}

    monkeypatch.setattr(
        web_support_tickets,
        "upload_ticket_attachments",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        web_support_tickets.db_session_adapter,
        "release_read_transaction",
        lambda db: None,
    )

    def fake_create_comment(
        db,
        ticket_id,
        payload,
        *,
        actor_id,
        request,
        mentioned_agent_ids,
    ):
        captured["mentioned_agent_ids"] = list(mentioned_agent_ids)
        return object()

    monkeypatch.setattr(
        web_support_tickets.support_service.tickets,
        "create_comment",
        fake_create_comment,
    )

    web_support_tickets.add_ticket_comment_from_form(
        None,
        request=None,
        ticket_id="ticket-1",
        actor_id=None,
        body="Please check this",
        is_internal=False,
        attachments=[],
        mentions='[{"id":"person:one","label":"One"}, "group:two"]',
    )

    assert captured["mentioned_agent_ids"] == ["person:one", "group:two"]
