"""PostgreSQL contracts for the Manager AI Team Inbox projection."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from app.models.service_team import ServiceTeam
from app.models.team_inbox import InboxConversation, InboxConversationAssignment
from app.services import team_inbox_analysis_projection as projection
from app.services.workqueue.permissions import WorkqueuePrincipal
from app.services.workqueue.scope import WorkqueueScope
from app.services.workqueue.types import WorkqueueAudience


def test_recent_queue_visibility_supports_conversation_json_metadata(db_session):
    person_id = uuid4()
    team = ServiceTeam(name=f"Manager AI PostgreSQL {uuid4().hex[:8]}")
    db_session.add(team)
    db_session.flush()
    conversation = InboxConversation(
        primary_service_team_id=team.id,
        channel_type="email",
        status="open",
        subject="PostgreSQL JSON visibility",
        last_message_at=datetime.now(UTC),
        metadata_={"source": "integration-test"},
        is_active=True,
    )
    db_session.add(conversation)
    db_session.flush()
    db_session.add(
        InboxConversationAssignment(
            conversation_id=conversation.id,
            service_team_id=team.id,
            person_id=person_id,
            is_active=True,
        )
    )
    db_session.commit()
    scope = WorkqueueScope(
        principal=WorkqueuePrincipal(
            person_id=person_id,
            roles=frozenset(),
            scopes=frozenset(),
            can_view=True,
            can_act=False,
        ),
        audience=WorkqueueAudience.team,
        member_service_team_ids=frozenset(),
        accessible_service_team_ids=frozenset(),
        accessible_person_ids=frozenset(),
        service_team_filter=None,
        is_org_wide=False,
    )

    result = projection.build_projection(
        db_session,
        projection.ManagerAnalysisRequest(
            scope=scope,
            mode=projection.ManagerAnalysisMode.recent_queue,
        ),
    )

    assert tuple(item.id for item in result.recent_conversations) == (conversation.id,)
