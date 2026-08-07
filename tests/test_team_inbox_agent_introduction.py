from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.models.party import Party
from app.models.service_team import ServiceTeam, ServiceTeamMember
from app.models.system_user import SystemUser
from app.models.team_inbox import InboxConversation, InboxMessage
from app.services.team_inbox_agent_introduction import (
    rendered_introduction,
    update_preference,
)
from app.services.team_inbox_assignment import assign_conversation_to_agent


def _agent_and_team(db_session):
    party = Party(party_type="person", display_name="Ada Agent")
    team = ServiceTeam(name=f"Introductions {uuid4()}", team_type="support")
    db_session.add_all([party, team])
    db_session.flush()
    user = SystemUser(
        person_party_id=party.id,
        party_bound_at=datetime.now(UTC),
        party_binding_source="test",
        party_binding_reason="introduction test",
        first_name="Ada",
        last_name="Agent",
        display_name="Ada Agent",
        email=f"ada-{uuid4()}@example.test",
    )
    db_session.add(user)
    db_session.flush()
    db_session.add(
        ServiceTeamMember(team_id=team.id, person_id=party.id, role="member")
    )
    db_session.flush()
    return user, team


def test_manual_insertion_uses_rendered_per_agent_template(db_session):
    user, _team = _agent_and_team(db_session)
    update_preference(
        db_session,
        person_id=user.id,
        template="Hello, I am {agent_name}. How can I help?",
        auto_send_chat_widget=False,
    )

    assert (
        rendered_introduction(db_session, user.id)
        == "Hello, I am Ada Agent. How can I help?"
    )
    template = Path("templates/admin/inbox/_conversation.html").read_text()
    javascript = Path("static/js/admin-inbox.js").read_text()
    assert "agent_introduction_text" in template
    assert "this.insertQuickResponse(this.introductionText)" in javascript


def test_first_chat_widget_pickup_auto_sends_once(db_session):
    user, team = _agent_and_team(db_session)
    conversation = InboxConversation(
        channel_type="chat_widget",
        status="open",
        contact_address="widget-session",
    )
    db_session.add(conversation)
    db_session.flush()
    update_preference(
        db_session,
        person_id=user.id,
        template="Welcome — {agent_name} here.",
        auto_send_chat_widget=True,
    )

    first = assign_conversation_to_agent(
        db_session,
        conversation=conversation,
        service_team_id=team.id,
        person_id=user.id,
    )
    assert first.kind == "assigned"
    messages = db_session.query(InboxMessage).all()
    assert [message.body for message in messages] == ["Welcome — Ada Agent here."]
    assert messages[0].metadata_["message_kind"] == "agent_introduction"

    second = assign_conversation_to_agent(
        db_session,
        conversation=conversation,
        service_team_id=team.id,
        person_id=user.id,
    )
    assert second.kind == "assigned"
    assert db_session.query(InboxMessage).count() == 1


def test_non_widget_pickup_never_auto_sends(db_session):
    user, team = _agent_and_team(db_session)
    conversation = InboxConversation(channel_type="email", status="open")
    db_session.add(conversation)
    db_session.flush()
    result = assign_conversation_to_agent(
        db_session,
        conversation=conversation,
        service_team_id=team.id,
        person_id=user.id,
    )
    assert result.kind == "assigned"
    assert db_session.query(InboxMessage).count() == 0
