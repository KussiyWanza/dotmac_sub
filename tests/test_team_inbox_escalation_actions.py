from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api import support as support_api
from app.models.service_team import ServiceTeam, ServiceTeamMember, ServiceTeamType
from app.models.team_inbox import (
    InboxAgentPresence,
    InboxAgentPresenceStatus,
    InboxConversation,
    InboxConversationAssignment,
    InboxConversationStatus,
    InboxConversationTeam,
    InboxTeamRole,
)
from app.schemas.team_inbox import InboxConversationEscalateRequest
from tests.staff_identity_fixtures import add_bound_staff_user


def _team(db_session, name: str = "Support") -> ServiceTeam:
    team = ServiceTeam(name=name, team_type=ServiceTeamType.support.value)
    db_session.add(team)
    db_session.flush()
    return team


def _member(
    db_session,
    team: ServiceTeam,
    *,
    status: str = InboxAgentPresenceStatus.online.value,
):
    user, person = add_bound_staff_user(db_session)
    db_session.add(
        ServiceTeamMember(team_id=team.id, person_id=person.id, is_active=True)
    )
    db_session.add(
        InboxAgentPresence(
            person_id=user.id,
            status=status,
            last_seen_at=datetime.now(UTC) if status == "online" else None,
        )
    )
    db_session.flush()
    return user.id


def _conversation(db_session, *, status: str = InboxConversationStatus.open.value):
    conversation = InboxConversation(
        channel_type="email",
        subject="Need help",
        status=status,
    )
    db_session.add(conversation)
    db_session.flush()
    return conversation


def test_escalate_inbox_conversation_auto_assigns_available_agent(db_session):
    team = _team(db_session, "Support")
    agent = _member(db_session, team)
    actor = uuid4()
    conversation = _conversation(db_session)
    db_session.commit()

    result = support_api.escalate_inbox_conversation(
        conversation.id,
        InboxConversationEscalateRequest(
            service_team_id=team.id,
            reason="Response SLA breached",
        ),
        auth={"principal_id": str(actor)},
        db=db_session,
    )

    assignment = db_session.query(InboxConversationAssignment).one()
    link = db_session.query(InboxConversationTeam).one()
    db_session.refresh(conversation)
    assert result.kind == "assigned"
    assert result.assigned_person_id == agent
    assert conversation.primary_service_team_id == team.id
    assert conversation.metadata_["last_inbox_escalation"]["kind"] == "assigned"
    assert conversation.metadata_["last_inbox_escalation"][
        "assigned_by_person_id"
    ] == str(actor)
    assert link.role == InboxTeamRole.owner.value
    assert assignment.person_id == agent
    assert assignment.assigned_by_person_id == actor
    assert assignment.metadata_ == {
        "reason": "Response SLA breached",
        "source": "escalation",
    }


def test_escalate_inbox_conversation_can_target_active_team_member(db_session):
    team = _team(db_session, "Field Service")
    agent = _member(db_session, team, status=InboxAgentPresenceStatus.online.value)
    conversation = _conversation(db_session)
    db_session.commit()

    result = support_api.escalate_inbox_conversation(
        conversation.id,
        InboxConversationEscalateRequest(
            service_team_id=team.id,
            assigned_person_id=agent,
            reason="Field follow-up",
        ),
        auth={"principal_id": str(uuid4())},
        db=db_session,
    )

    assert result.kind == "assigned"
    assert result.assigned_person_id == agent
    assert db_session.query(InboxConversationAssignment).one().person_id == agent


def test_escalate_inbox_conversation_rejects_non_team_member(db_session):
    team = _team(db_session)
    conversation = _conversation(db_session)
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        support_api.escalate_inbox_conversation(
            conversation.id,
            InboxConversationEscalateRequest(
                service_team_id=team.id,
                assigned_person_id=uuid4(),
            ),
            auth={"principal_id": str(uuid4())},
            db=db_session,
        )

    assert exc.value.status_code == 400
    assert exc.value.detail == "person_id must be an active member of the target team"


def test_escalate_inbox_conversation_can_move_to_team_queue(db_session):
    old_team = _team(db_session, "Billing")
    new_team = _team(db_session, "Support")
    old_agent = _member(db_session, old_team)
    actor = uuid4()
    conversation = _conversation(db_session)
    db_session.add(
        InboxConversationAssignment(
            conversation_id=conversation.id,
            service_team_id=old_team.id,
            person_id=old_agent,
            is_active=True,
        )
    )
    db_session.commit()

    result = support_api.escalate_inbox_conversation(
        conversation.id,
        InboxConversationEscalateRequest(
            service_team_id=new_team.id,
            auto_assign=False,
            reason="Billing tagged support",
        ),
        auth={"principal_id": str(actor)},
        db=db_session,
    )

    assignment = db_session.query(InboxConversationAssignment).one()
    db_session.refresh(conversation)
    assert result.kind == "queued"
    assert result.service_team_id == new_team.id
    assert result.reason == "manual_queue"
    assert assignment.is_active is False
    assert conversation.primary_service_team_id == new_team.id
    assert conversation.metadata_["last_inbox_escalation"]["kind"] == "queued"
    assert conversation.metadata_["last_inbox_escalation"]["reason"] == (
        "Billing tagged support"
    )


def test_escalate_inbox_conversation_rejects_resolved_conversation(db_session):
    team = _team(db_session)
    conversation = _conversation(
        db_session, status=InboxConversationStatus.resolved.value
    )
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        support_api.escalate_inbox_conversation(
            conversation.id,
            InboxConversationEscalateRequest(service_team_id=team.id),
            auth={"principal_id": str(uuid4())},
            db=db_session,
        )

    assert exc.value.status_code == 409
