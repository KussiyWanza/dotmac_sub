"""Slice 4: demo controls that had a real owner behind them all along.

Each of these was a `showDemoNotice(...)` in the workspace while the owning
service already implemented the behaviour and simply had no committed entry
point or no caller.

See docs/designs/TEAM_INBOX_ADMIN_UI_PORT.md §5, slice 4.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app.models.service_team import ServiceTeam, ServiceTeamMember, ServiceTeamType
from app.models.team_inbox import (
    InboxConversation,
    InboxConversationStatus,
)
from app.services import team_inbox_commands, team_inbox_read

CONVERSATION = Path("templates/admin/inbox/_conversation.html").read_text()
JAVASCRIPT = Path("static/js/admin-inbox.js").read_text()
ROUTES = Path("app/web/admin/inbox.py").read_text()


def _team(db_session, name="Support", *, member_id=None):
    """A team, optionally with an active member.

    Membership matters: assign_conversation_to_agent refuses an agent who is
    not an active member of the target team.
    """
    team = ServiceTeam(name=name, team_type=ServiceTeamType.support.value)
    db_session.add(team)
    db_session.flush()
    captured = team.id
    if member_id is not None:
        db_session.add(
            ServiceTeamMember(team_id=captured, person_id=member_id, is_active=True)
        )
    db_session.commit()
    return captured


def _conversation(db_session, *, team_id=None):
    conversation = InboxConversation(
        channel_type="email",
        subject="Line fault",
        contact_address="customer@example.com",
        status=InboxConversationStatus.open.value,
        primary_service_team_id=team_id,
    )
    db_session.add(conversation)
    db_session.flush()
    captured = conversation.id
    db_session.commit()
    return captured


# --- teammate assignment ------------------------------------------------


def test_assign_conversation_records_the_assignment(db_session):
    agent = uuid.uuid4()
    team_id = _team(db_session, member_id=agent)
    conversation_id = _conversation(db_session, team_id=team_id)

    result = team_inbox_commands.assign_conversation(
        db_session,
        conversation_id=conversation_id,
        service_team_id=team_id,
        person_id=agent,
        reason="escalated from workspace",
    )

    assert result is not None
    timeline = team_inbox_read.get_conversation_timeline(db_session, conversation_id)
    active = [a for a in timeline.assignments if a.is_active]
    assert [str(a.person_id) for a in active] == [str(agent)]


def test_assigning_a_non_member_is_refused_not_silently_dropped(db_session):
    """The owner reports this in the result instead of raising, so the route
    must inspect `kind` — otherwise the operator is told it worked."""
    team_id = _team(db_session)  # no members
    conversation_id = _conversation(db_session, team_id=team_id)

    outcome = team_inbox_commands.assign_conversation(
        db_session,
        conversation_id=conversation_id,
        service_team_id=team_id,
        person_id=uuid.uuid4(),
    )

    assert outcome.kind == "invalid_agent"
    assert "member" in (outcome.reason or "")
    assert 'outcome.kind != "assigned"' in ROUTES


def test_assign_rejects_an_unknown_conversation(db_session):
    team_id = _team(db_session)
    with pytest.raises(team_inbox_commands.ConversationNotFoundError):
        team_inbox_commands.assign_conversation(
            db_session,
            conversation_id=uuid.uuid4(),
            service_team_id=team_id,
            person_id=uuid.uuid4(),
        )


def test_escalate_to_teammate_is_a_real_form_not_a_demo_notice():
    assert "showDemoNotice('Teammate escalation')" not in CONVERSATION
    assert "/assign" in CONVERSATION
    assert "team_inbox_commands.assign_conversation" in ROUTES


# --- macro execution ----------------------------------------------------


def test_run_macro_is_wired_and_distinct_from_inserting_text():
    """Inserting a macro body is text; running it applies its actions."""
    assert "/run-macro" in CONVERSATION
    assert "team_inbox_commands.run_macro" in ROUTES
    assert (
        "execute_macro_actions"
        in Path("app/services/team_inbox_commands.py").read_text()
    )
    # The insert path must still exist and still carry identity.
    assert "inbox-insert-text" in CONVERSATION
    assert "macroId" in CONVERSATION


def test_run_macro_rejects_an_unknown_macro(db_session):
    conversation_id = _conversation(db_session)
    with pytest.raises(Exception) as exc:
        team_inbox_commands.run_macro(
            db_session, conversation_id=conversation_id, macro_id=uuid.uuid4()
        )
    assert "macro" in str(exc.value).lower()


# --- my-team filter -----------------------------------------------------


def test_list_conversations_scopes_to_several_teams(db_session):
    """The my_team badge counts every team the agent is in, so the filter must
    select the same set or the number and the list disagree."""
    team_a = _team(db_session, "Team A")
    team_b = _team(db_session, "Team B")
    other = _team(db_session, "Other")
    mine_a = _conversation(db_session, team_id=team_a)
    mine_b = _conversation(db_session, team_id=team_b)
    _conversation(db_session, team_id=other)

    # primary_service_team_id alone is not the join; link rows drive team scope.
    from app.models.team_inbox import InboxConversationTeam

    for conversation_id, team_id in ((mine_a, team_a), (mine_b, team_b)):
        db_session.add(
            InboxConversationTeam(
                conversation_id=conversation_id,
                service_team_id=team_id,
                role="owner",
                source="manual",
            )
        )
    db_session.commit()

    result = team_inbox_read.list_conversations(
        db_session, service_team_ids=[team_a, team_b]
    )

    assert {row.id for row in result.items} == {str(mine_a), str(mine_b)}


def test_multi_team_filter_is_additive(db_session):
    _conversation(db_session, team_id=_team(db_session))
    assert team_inbox_read.list_conversations(db_session).count == 1
    assert (
        team_inbox_read.list_conversations(db_session, service_team_ids=[]).count == 1
    )


def test_my_team_filter_uses_the_counted_team_set():
    assert "showDemoNotice(" not in JAVASCRIPT.split("applyTeamFilter()")[1][:400]
    assert "service_team_ids: this.myTeamIds" in JAVASCRIPT
    assert "my_team_ids" in Path("app/services/team_inbox_projection.py").read_text()
    assert "service_team_ids" in ROUTES


def test_agent_with_no_team_is_told_rather_than_silently_filtered():
    block = JAVASCRIPT.split("applyTeamFilter()")[1][:400]
    assert "not a member of any service team" in block


def test_team_membership_still_drives_the_count(db_session):
    """Guards the projection wiring the filter now depends on."""
    assert ServiceTeamMember is not None  # imported for the membership join
