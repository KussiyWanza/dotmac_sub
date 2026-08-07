from __future__ import annotations

import uuid

from starlette.requests import Request

from app.models.service_team import ServiceTeam, ServiceTeamMember, ServiceTeamType
from app.models.team_inbox import (
    InboxAgentPresence,
    InboxAgentPresenceStatus,
    InboxConversation,
    InboxConversationAssignment,
    InboxConversationStatus,
    InboxMessage,
    InboxMessageDirection,
    InboxSavedFilter,
)
from app.services import team_inbox_filters, team_inbox_operations, team_inbox_read
from app.web.admin import inbox as inbox_web
from tests.staff_identity_fixtures import add_bound_staff_user


def _request() -> Request:
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request({"type": "http", "method": "POST", "path": "/"}, receive)


def _team(db_session, name: str = "Support") -> ServiceTeam:
    team = ServiceTeam(name=name, team_type=ServiceTeamType.support.value)
    db_session.add(team)
    db_session.flush()
    return team


def _member(db_session, team: ServiceTeam):
    user, person = add_bound_staff_user(db_session)
    db_session.add(
        ServiceTeamMember(team_id=team.id, person_id=person.id, is_active=True)
    )
    db_session.add(
        InboxAgentPresence(
            person_id=user.id,
            status=InboxAgentPresenceStatus.online.value,
        )
    )
    db_session.flush()
    return user.id


def _conversation(db_session, *, subject: str = "Need help") -> InboxConversation:
    conversation = InboxConversation(
        channel_type="email",
        subject=subject,
        status=InboxConversationStatus.open.value,
        contact_address="ada@example.com",
    )
    db_session.add(conversation)
    db_session.flush()
    return conversation


def test_admin_workflow_action_sets_priority_mute_and_snooze(db_session, monkeypatch):
    actor_id = uuid.uuid4()
    conversation = _conversation(db_session)
    from app.services import web_admin as web_admin_service

    monkeypatch.setattr(
        web_admin_service, "get_actor_id", lambda request: str(actor_id)
    )

    response = inbox_web.team_inbox_workflow_action(
        conversation.id,
        _request(),
        priority=10,
        is_muted=True,
        snooze_minutes=60,
        db=db_session,
    )

    db_session.refresh(conversation)
    assert response.status_code == 303
    assert conversation.priority == 10
    assert conversation.is_muted is True
    assert conversation.status == InboxConversationStatus.snoozed.value
    assert conversation.snoozed_until is not None
    assert conversation.metadata_["workflow_history"][-1]["actor_id"] == str(actor_id)


def test_saved_filters_are_visible_to_owner_and_shared_users(db_session):
    owner_id = uuid.uuid4()
    other_id = uuid.uuid4()

    personal = team_inbox_operations.save_filter(
        db_session,
        name="My urgent queue",
        filter_payload=team_inbox_filters.InboxSavedFilterPayload(priority_at_most=25),
        owner_person_id=owner_id,
        is_shared=False,
    )
    shared = team_inbox_operations.save_filter(
        db_session,
        name="Unmapped social",
        filter_payload=team_inbox_filters.InboxSavedFilterPayload(
            contact_resolution_status="unmatched"
        ),
        owner_person_id=owner_id,
        is_shared=True,
    )

    owner_filters = team_inbox_operations.list_saved_filters(
        db_session, person_id=owner_id
    )
    other_filters = team_inbox_operations.list_saved_filters(
        db_session, person_id=other_id
    )

    assert {item.id for item in owner_filters} == {str(personal.id), str(shared.id)}
    assert [item.id for item in other_filters] == [str(shared.id)]


def test_admin_saved_filter_route_persists_current_view(db_session, monkeypatch):
    actor_id = uuid.uuid4()
    from app.services import web_admin as web_admin_service

    monkeypatch.setattr(
        web_admin_service, "get_actor_id", lambda request: str(actor_id)
    )

    response = inbox_web.team_inbox_saved_filter_create(
        _request(),
        name="Needs response",
        search="router",
        status_value="open",
        channel_type="email",
        service_team_id=None,
        service_team_ids=None,
        assigned_person_id=None,
        needs_response=True,
        needs_attention=False,
        contact_resolution_status=None,
        priority_at_most=50,
        muted=None,
        snoozed=None,
        open_only=False,
        unassigned=False,
        unread=False,
        ai_handling=False,
        has_ticket=False,
        activity_from=None,
        activity_to=None,
        is_shared=True,
        db=db_session,
    )

    saved_filter = db_session.query(InboxSavedFilter).one()
    assert response.status_code == 303
    assert saved_filter.owner_person_id == actor_id
    assert saved_filter.is_shared is True
    assert saved_filter.filter_payload["search"] == "router"
    assert saved_filter.filter_payload["priority_at_most"] == 50


def test_bulk_escalate_assigns_conversations_to_available_agent(
    db_session, monkeypatch
):
    actor_id = uuid.uuid4()
    team = _team(db_session)
    agent_id = _member(db_session, team)
    conversation = _conversation(db_session)
    from app.services import web_admin as web_admin_service

    monkeypatch.setattr(
        web_admin_service, "get_actor_id", lambda request: str(actor_id)
    )

    response = inbox_web.team_inbox_bulk_action(
        _request(),
        conversation_ids=[str(conversation.id)],
        action="escalate",
        status_value=None,
        label_id=None,
        service_team_id=str(team.id),
        assigned_person_id=None,
        auto_assign=True,
        db=db_session,
    )

    assignment = db_session.query(InboxConversationAssignment).one()
    db_session.refresh(conversation)
    assert response.status_code == 303
    assert conversation.primary_service_team_id == team.id
    assert assignment.person_id == agent_id
    assert assignment.assigned_by_person_id == actor_id


def test_failed_outbox_report_lists_retryable_messages(db_session, monkeypatch):
    conversation = _conversation(db_session)
    failed = InboxMessage(
        conversation_id=conversation.id,
        channel_type="email",
        direction=InboxMessageDirection.outbound.value,
        body="Failed reply",
        to_addresses=["ada@example.com"],
        metadata_={"delivery_status": "failed", "send_error": "SMTP rejected"},
    )
    db_session.add(failed)
    db_session.flush()
    captured: dict[str, object] = {}

    def _fake_template_response(template_name, context):
        captured["template_name"] = template_name
        captured["context"] = context
        return context

    monkeypatch.setattr(
        inbox_web.templates, "TemplateResponse", _fake_template_response
    )

    context = inbox_web.team_inbox_outbox_failures(_request(), db_session)

    assert captured["template_name"] == "admin/inbox/outbox_failures.html"
    assert context["messages"][0].id == failed.id


def test_read_model_filters_priority_and_snoozed(db_session):
    high = _conversation(db_session, subject="High")
    high.priority = 10
    low = _conversation(db_session, subject="Low")
    low.priority = 100
    low.snoozed_until = None
    db_session.flush()

    result = team_inbox_read.list_conversations(
        db_session,
        priority_at_most=25,
        snoozed=False,
    )

    assert [item.id for item in result.items] == [str(high.id)]
    assert result.items[0].priority == 10
