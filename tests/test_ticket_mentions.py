from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.notification import (
    Notification,
    NotificationChannel,
    NotificationStatus,
)
from app.models.service_team import ServiceTeam, ServiceTeamMember, ServiceTeamType
from app.models.support import Ticket, TicketCommentMention
from app.models.system_user import SystemUser
from app.schemas.support import (
    TicketCommentCreate,
    TicketCommentUpdate,
    TicketMentionTarget,
    TicketMentionTargetKind,
)
from app.services import support as support_service
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
        ticket_id=ticket_id,
        ticket_number="TCK-1",
        ticket_title="Router swap",
        comment_preview="Please check this",
        mention_targets=(
            TicketMentionTarget(
                kind=TicketMentionTargetKind.person,
                target_id=recipient.id,
            ),
            TicketMentionTarget(
                kind=TicketMentionTargetKind.person,
                target_id=actor.id,
            ),
        ),
        actor_person_id=str(actor.id),
        source_event_id=uuid4(),
        source_comment_id=uuid4(),
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
    person_id = uuid4()
    group_id = uuid4()
    parsed = _parse_mentions_payload(
        f"["
        f'{{"id":"person:{person_id}","label":"One"}}, '
        f'"group:{group_id}", '
        f'{{"kind":"person","target_id":"{person_id}"}}'
        f"]"
    )

    assert parsed == (
        TicketMentionTarget(
            kind=TicketMentionTargetKind.person,
            target_id=person_id,
        ),
        TicketMentionTarget(
            kind=TicketMentionTargetKind.group,
            target_id=group_id,
        ),
    )


def test_comment_form_preserves_person_and_group_mention_tokens(monkeypatch):
    captured = {}
    ticket_id = uuid4()
    person_id = uuid4()
    group_id = uuid4()

    monkeypatch.setattr(
        web_support_tickets,
        "upload_ticket_attachments",
        lambda *args, **kwargs: (),
    )
    monkeypatch.setattr(
        web_support_tickets.db_session_adapter,
        "release_read_transaction",
        lambda db: None,
    )

    def fake_create_comment(db, ticket_id, payload, *, actor_id, request):
        captured["mention_targets"] = payload.mentions
        return object()

    monkeypatch.setattr(
        web_support_tickets.support_service.tickets,
        "create_comment",
        fake_create_comment,
    )

    web_support_tickets.add_ticket_comment_from_form(
        None,
        request=None,
        ticket_id=str(ticket_id),
        actor_id=None,
        body="Please check this",
        is_internal=False,
        attachments=[],
        mentions=(f'[{{"id":"person:{person_id}","label":"One"}}, "group:{group_id}"]'),
    )

    assert captured["mention_targets"] == (
        TicketMentionTarget(
            kind=TicketMentionTargetKind.person,
            target_id=person_id,
        ),
        TicketMentionTarget(
            kind=TicketMentionTargetKind.group,
            target_id=group_id,
        ),
    )


def test_comment_edit_form_forwards_complete_typed_mention_set(monkeypatch):
    ticket_id = uuid4()
    comment_id = uuid4()
    person_id = uuid4()
    group_id = uuid4()
    captured = {}
    comment = SimpleNamespace(id=comment_id, ticket_id=ticket_id)

    monkeypatch.setattr(
        web_support_tickets.support_service.ticket_comments,
        "get",
        lambda db, value: comment,
    )
    monkeypatch.setattr(
        web_support_tickets.db_session_adapter,
        "release_read_transaction",
        lambda db: None,
    )

    def fake_update(db, *, comment, payload, actor_id, request):
        captured["payload"] = payload
        return comment

    monkeypatch.setattr(
        web_support_tickets.support_service.ticket_comments,
        "update",
        fake_update,
    )

    web_support_tickets.update_ticket_comment_from_form(
        None,
        request=None,
        ticket_id=str(ticket_id),
        comment_id=str(comment_id),
        actor_id=None,
        body="Edited body",
        mentions=f'["person:{person_id}", "group:{group_id}"]',
    )

    assert captured["payload"].mentions == (
        TicketMentionTarget(
            kind=TicketMentionTargetKind.person,
            target_id=person_id,
        ),
        TicketMentionTarget(
            kind=TicketMentionTargetKind.group,
            target_id=group_id,
        ),
    )


def test_comment_edit_notifies_only_new_persisted_mentions(db_session, monkeypatch):
    first = _system_user(db_session, email="first-mentioned@example.com")
    second = _system_user(db_session, email="second-mentioned@example.com")
    actor = _system_user(db_session, email="mention-author@example.com")
    ticket = Ticket(title="Mention persistence", number="TCK-MENTION-1")
    db_session.add(ticket)
    db_session.commit()
    monkeypatch.setattr(
        ticket_mentions,
        "get_brand",
        lambda: {"app_url": "https://sub.example.test/"},
    )

    first_target = TicketMentionTarget(
        kind=TicketMentionTargetKind.person,
        target_id=first.id,
    )
    second_target = TicketMentionTarget(
        kind=TicketMentionTargetKind.person,
        target_id=second.id,
    )
    comment = support_service.tickets.create_comment(
        db_session,
        str(ticket.id),
        TicketCommentCreate(
            body=f"Please review @{first.display_name}",
            author_type="staff",
            author_system_user_id=actor.id,
            mentions=(first_target,),
        ),
        actor_id=str(actor.id),
    )
    initial_notification_count = db_session.query(Notification).count()
    assert initial_notification_count == 2
    assert db_session.query(TicketCommentMention).count() == 1

    unchanged = support_service.ticket_comments.update(
        db_session,
        comment=comment,
        payload=TicketCommentUpdate(
            body=comment.body,
            mentions=(first_target,),
        ),
        actor_id=str(actor.id),
    )
    assert db_session.query(Notification).count() == initial_notification_count

    updated = support_service.ticket_comments.update(
        db_session,
        comment=unchanged,
        payload=TicketCommentUpdate(
            body=f"{comment.body} and @{second.display_name}",
            mentions=(first_target, second_target),
        ),
        actor_id=str(actor.id),
    )
    assert db_session.query(Notification).count() == initial_notification_count + 2
    assert db_session.query(TicketCommentMention).count() == 2

    support_service.ticket_comments.update(
        db_session,
        comment=updated,
        payload=TicketCommentUpdate(
            body=f"Please review @{second.display_name}",
            mentions=(second_target,),
        ),
        actor_id=str(actor.id),
    )
    assert db_session.query(Notification).count() == initial_notification_count + 2
    links = db_session.query(TicketCommentMention).all()
    assert [link.system_user_id for link in links] == [second.id]


def test_comment_team_and_direct_mentions_persist_but_notify_member_once(
    db_session, monkeypatch
):
    member = _system_user(db_session, email="team-mentioned@example.com")
    actor = _system_user(db_session, email="team-mention-author@example.com")
    team = ServiceTeam(name="Mention Team", team_type=ServiceTeamType.support.value)
    ticket = Ticket(title="Team mention", number="TCK-MENTION-2")
    db_session.add_all([team, ticket])
    db_session.flush()
    db_session.add(ServiceTeamMember(team_id=team.id, person_id=member.person_party_id))
    db_session.commit()
    monkeypatch.setattr(
        ticket_mentions,
        "get_brand",
        lambda: {"app_url": "https://sub.example.test/"},
    )

    comment = support_service.tickets.create_comment(
        db_session,
        str(ticket.id),
        TicketCommentCreate(
            body=f"Please review @{member.display_name} and @Mention Team (Group)",
            author_type="staff",
            author_system_user_id=actor.id,
            mentions=(
                TicketMentionTarget(
                    kind=TicketMentionTargetKind.person,
                    target_id=member.id,
                ),
                TicketMentionTarget(
                    kind=TicketMentionTargetKind.group,
                    target_id=team.id,
                ),
            ),
        ),
        actor_id=str(actor.id),
    )

    links = db_session.query(TicketCommentMention).all()
    assert {(row.system_user_id, row.service_team_id) for row in links} == {
        (member.id, None),
        (None, team.id),
    }
    assert db_session.query(Notification).count() == 2
    selections = web_support_tickets._comment_mention_selections(
        db_session,
        comments=[comment],
        available_options=[],
    )
    assert {item["token"] for item in selections[str(comment.id)]} == {
        f"person:{member.id}",
        f"group:{team.id}",
    }


def test_comment_rejects_unavailable_new_mention_target(db_session):
    actor = _system_user(db_session, email="invalid-mention-author@example.com")
    ticket = Ticket(title="Unavailable mention", number="TCK-MENTION-3")
    db_session.add(ticket)
    db_session.commit()

    with pytest.raises(support_service.SupportTicketError) as exc:
        support_service.tickets.create_comment(
            db_session,
            str(ticket.id),
            TicketCommentCreate(
                body="Please review",
                author_type="staff",
                author_system_user_id=actor.id,
                mentions=(
                    TicketMentionTarget(
                        kind=TicketMentionTargetKind.person,
                        target_id=uuid4(),
                    ),
                ),
            ),
            actor_id=str(actor.id),
        )

    assert exc.value.code == "ticket_comment_mention_target_unavailable"


def test_comment_edit_rejects_mentions_on_customer_authored_comment(db_session):
    recipient = _system_user(db_session, email="customer-edit-target@example.com")
    ticket = Ticket(title="Customer comment edit", number="TCK-MENTION-4")
    db_session.add(ticket)
    db_session.commit()
    comment = support_service.tickets.create_comment(
        db_session,
        str(ticket.id),
        TicketCommentCreate(body="Customer reply", author_type="customer"),
    )

    with pytest.raises(support_service.SupportTicketError) as exc:
        support_service.ticket_comments.update(
            db_session,
            comment=comment,
            payload=TicketCommentUpdate(
                mentions=(
                    TicketMentionTarget(
                        kind=TicketMentionTargetKind.person,
                        target_id=recipient.id,
                    ),
                )
            ),
            actor_id=str(recipient.id),
        )

    assert exc.value.code == "ticket_comment_mentions_not_allowed"
