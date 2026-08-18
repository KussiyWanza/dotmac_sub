from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

from app.models.service_team import ServiceTeam
from app.models.team_inbox import (
    InboxAuditEvidenceGrade,
    InboxAuditSource,
    InboxConversation,
    InboxConversationStatus,
    InboxMessage,
    InboxMessageDirection,
    InboxRoutingDecisionMode,
    InboxRoutingEvent,
    InboxRoutingEventType,
    InboxStatusTransitionEvent,
)
from app.services import team_inbox_analysis_projection as projection
from app.services.workqueue.permissions import WorkqueuePrincipal
from app.services.workqueue.scope import WorkqueueScope
from app.services.workqueue.types import WorkqueueAudience


def _scope(*, team_ids=frozenset(), org_wide=False):
    principal = WorkqueuePrincipal(
        person_id=uuid4(),
        roles=frozenset(),
        scopes=frozenset(),
        can_view=True,
        can_act=False,
    )
    return WorkqueueScope(
        principal=principal,
        audience=WorkqueueAudience.org if org_wide else WorkqueueAudience.team,
        member_service_team_ids=team_ids,
        accessible_service_team_ids=team_ids,
        accessible_person_ids=frozenset(),
        service_team_filter=None,
        is_org_wide=org_wide,
    )


def _conversation(db_session, *, team_id, at, status="open"):
    conversation = InboxConversation(
        primary_service_team_id=team_id,
        status=status,
        channel_type="email",
        first_message_at=at,
        last_message_at=at,
    )
    db_session.add(conversation)
    db_session.flush()
    return conversation


def _message(db_session, conversation, at, body="help"):
    db_session.add(
        InboxMessage(
            conversation_id=conversation.id,
            direction=InboxMessageDirection.inbound.value,
            channel_type="email",
            body=body,
            received_at=at,
            created_at=at,
        )
    )


def test_period_projection_excludes_out_of_scope_conversations_and_keeps_current_state_separate(
    db_session,
):
    allowed_team = ServiceTeam(name=f"Allowed-{uuid4().hex[:8]}")
    denied_team = ServiceTeam(name=f"Denied-{uuid4().hex[:8]}")
    db_session.add_all([allowed_team, denied_team])
    db_session.flush()
    activity_at = datetime(2026, 8, 5, 12, tzinfo=UTC)
    allowed = _conversation(
        db_session,
        team_id=allowed_team.id,
        at=activity_at,
        status=InboxConversationStatus.resolved.value,
    )
    denied = _conversation(db_session, team_id=denied_team.id, at=activity_at)
    _message(db_session, allowed, activity_at)
    _message(db_session, denied, activity_at)
    db_session.commit()

    result = projection.build_projection(
        db_session,
        projection.ManagerAnalysisRequest(
            scope=_scope(team_ids=frozenset({allowed_team.id})),
            mode=projection.ManagerAnalysisMode.period,
            period=projection.ManagerAnalysisPeriod.custom,
            custom_start=date(2026, 8, 5),
            custom_end=date(2026, 8, 5),
        ),
    )

    assert result.facts is not None
    assert result.facts.total_conversations == 1
    assert result.facts.current_state_status_counts == (("resolved", 1),)
    assert result.facts.resolved_transition_count == 0
    assert {item.id for item in result.evidence_conversations} == {allowed.id}
    assert denied.id not in {item.id for item in result.evidence_conversations}


def test_period_projection_bounds_evidence_and_records_period_events(db_session):
    team = ServiceTeam(name=f"Inbox-{uuid4().hex[:8]}")
    db_session.add(team)
    db_session.flush()
    activity_at = datetime(2026, 8, 5, 12, tzinfo=UTC)
    conversations = []
    for number in range(26):
        conversation = _conversation(db_session, team_id=team.id, at=activity_at)
        conversations.append(conversation)
        for message_number in range(15):
            _message(
                db_session,
                conversation,
                activity_at + timedelta(minutes=message_number),
                body=f"{number}-{message_number}",
            )
    reopened = conversations[0]
    db_session.add(
        InboxStatusTransitionEvent(
            conversation_id=reopened.id,
            previous_status=InboxConversationStatus.resolved.value,
            status=InboxConversationStatus.open.value,
            actor_person_id=None,
            reason_code="customer_replied",
            source=InboxAuditSource.status_command,
            source_id=f"test-reopen-{uuid4()}",
            evidence_grade=InboxAuditEvidenceGrade.native,
            occurred_at=activity_at,
        )
    )
    db_session.add(
        InboxRoutingEvent(
            conversation_id=reopened.id,
            event_type=InboxRoutingEventType.escalated,
            previous_service_team_id=team.id,
            service_team_id=team.id,
            previous_person_id=None,
            person_id=None,
            actor_person_id=None,
            decision_mode=InboxRoutingDecisionMode.manual,
            reason_code="manager_attention",
            source=InboxAuditSource.routing_command,
            source_id=f"test-escalation-{uuid4()}",
            evidence_grade=InboxAuditEvidenceGrade.native,
            occurred_at=activity_at,
        )
    )
    db_session.commit()

    result = projection.build_projection(
        db_session,
        projection.ManagerAnalysisRequest(
            scope=_scope(team_ids=frozenset({team.id})),
            mode=projection.ManagerAnalysisMode.period,
            period=projection.ManagerAnalysisPeriod.custom,
            custom_start=date(2026, 8, 5),
            custom_end=date(2026, 8, 5),
        ),
    )

    assert result.facts is not None
    assert result.facts.total_conversations == 26
    assert result.facts.evidence_count == 25
    assert reopened.id in result.facts.reopened_conversation_ids
    assert reopened.id in result.facts.escalated_conversation_ids
    assert len(result.evidence_conversations) == 25
    assert all(len(item.messages) <= 12 for item in result.evidence_conversations)


def test_period_cohort_uses_each_canonical_activity_source_once_and_is_half_open(
    db_session,
):
    team = ServiceTeam(name=f"Boundary-{uuid4().hex[:8]}")
    db_session.add(team)
    db_session.flush()
    lower = datetime(2026, 8, 5, tzinfo=UTC)
    upper = lower + timedelta(days=1)
    message_only = _conversation(db_session, team_id=team.id, at=lower)
    duplicate = _conversation(db_session, team_id=team.id, at=lower)
    status_only = _conversation(db_session, team_id=team.id, at=lower)
    routing_only = _conversation(db_session, team_id=team.id, at=lower)
    upper_boundary = _conversation(db_session, team_id=team.id, at=upper)
    _message(db_session, message_only, lower)
    _message(db_session, duplicate, lower)
    _message(db_session, duplicate, lower + timedelta(hours=1))
    _message(db_session, upper_boundary, upper)
    db_session.add(
        InboxStatusTransitionEvent(
            conversation_id=status_only.id,
            previous_status=InboxConversationStatus.open.value,
            status=InboxConversationStatus.resolved.value,
            actor_person_id=None,
            reason_code="resolved",
            source=InboxAuditSource.status_command,
            source_id=f"test-status-{uuid4()}",
            evidence_grade=InboxAuditEvidenceGrade.native,
            occurred_at=lower,
        )
    )
    db_session.add(
        InboxRoutingEvent(
            conversation_id=routing_only.id,
            event_type=InboxRoutingEventType.escalated,
            previous_service_team_id=team.id,
            service_team_id=team.id,
            previous_person_id=None,
            person_id=None,
            actor_person_id=None,
            decision_mode=InboxRoutingDecisionMode.manual,
            reason_code="manager_attention",
            source=InboxAuditSource.routing_command,
            source_id=f"test-routing-{uuid4()}",
            evidence_grade=InboxAuditEvidenceGrade.native,
            occurred_at=lower,
        )
    )
    db_session.commit()

    result = projection.build_projection(
        db_session,
        projection.ManagerAnalysisRequest(
            scope=_scope(team_ids=frozenset({team.id})),
            mode=projection.ManagerAnalysisMode.period,
            period=projection.ManagerAnalysisPeriod.custom,
            custom_start=lower.date(),
            custom_end=lower.date(),
        ),
    )

    assert result.facts is not None
    assert result.facts.total_conversations == 4
    assert result.facts.resolved_transition_count == 1
    assert result.facts.escalated_conversation_ids == (routing_only.id,)
    assert upper_boundary.id not in {item.id for item in result.evidence_conversations}


def test_period_projection_returns_an_empty_safe_cohort(db_session):
    team = ServiceTeam(name=f"Empty-{uuid4().hex[:8]}")
    db_session.add(team)
    db_session.commit()

    result = projection.build_projection(
        db_session,
        projection.ManagerAnalysisRequest(
            scope=_scope(team_ids=frozenset({team.id})),
            mode=projection.ManagerAnalysisMode.period,
            period=projection.ManagerAnalysisPeriod.custom,
            custom_start=date(2026, 8, 5),
            custom_end=date(2026, 8, 5),
        ),
    )

    assert result.facts is not None
    assert result.facts.total_conversations == 0
    assert result.evidence_conversations == ()
