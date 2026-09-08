from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import column, event, select
from sqlalchemy.dialects import postgresql

from app.api import analytics as analytics_api
from app.models.notification import Notification
from app.models.service_team import (
    ServiceTeam,
    ServiceTeamCapability,
    ServiceTeamCapabilityDefinition,
    ServiceTeamCapabilityKey,
    ServiceTeamMember,
    ServiceTeamType,
)
from app.models.team_inbox import (
    InboxAgentPresence,
    InboxAgentPresenceStatus,
    InboxConversation,
    InboxConversationAssignment,
    InboxConversationStatus,
    InboxConversationTeam,
    InboxMessage,
    InboxMessageDirection,
    InboxTeamRole,
    InboxTeamSource,
)
from app.services import (
    crm_reporting,
    service_team_composition,
    team_inbox_metrics,
    team_inbox_status,
)
from app.web.admin import reports as admin_reports
from tests.staff_identity_fixtures import add_bound_staff_user


def _team(db_session, name: str = "Support") -> ServiceTeam:
    capability = ServiceTeamCapabilityKey.customer_support
    contract = service_team_composition.CAPABILITY_CONTRACTS[capability]
    if db_session.get(ServiceTeamCapabilityDefinition, capability.value) is None:
        db_session.add(
            ServiceTeamCapabilityDefinition(
                key=capability.value,
                display_name=contract.display_name,
                contract_owner=contract.contract_owner,
                contract_version=contract.contract_version,
                description=f"Test definition for {capability.value}",
                is_active=True,
            )
        )
    team = ServiceTeam(name=name, team_type=ServiceTeamType.support.value)
    db_session.add(team)
    db_session.flush()
    db_session.add(
        ServiceTeamCapability(
            team_id=team.id,
            capability_key=capability.value,
            is_active=True,
        )
    )
    db_session.flush()
    return team


def _conversation(
    db_session, team: ServiceTeam, *, first_at: datetime
) -> InboxConversation:
    conversation = InboxConversation(
        channel_type="email",
        status=InboxConversationStatus.open.value,
        subject="Need help",
        contact_address="customer@example.com",
        first_message_at=first_at,
        last_message_at=first_at,
    )
    db_session.add(conversation)
    db_session.flush()
    db_session.add(
        InboxConversationTeam(
            conversation_id=conversation.id,
            service_team_id=team.id,
            role=InboxTeamRole.owner.value,
            source=InboxTeamSource.routing_rule.value,
            is_active=True,
        )
    )
    db_session.flush()
    return conversation


def _message(
    db_session,
    conversation: InboxConversation,
    *,
    direction: str,
    at: datetime,
    sent_by_person_id: object | None = None,
) -> InboxMessage:
    message = InboxMessage(
        conversation_id=conversation.id,
        channel_type="email",
        direction=direction,
        subject="Message",
        body="Body",
        received_at=at if direction == InboxMessageDirection.inbound.value else None,
        sent_at=at if direction == InboxMessageDirection.outbound.value else None,
        metadata_=(
            {"sent_by_person_id": str(sent_by_person_id)}
            if sent_by_person_id is not None
            else None
        ),
    )
    db_session.add(message)
    db_session.flush()
    return message


def test_performance_window_defaults_to_30_days_and_rejects_unbounded_history():
    observed_at = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)

    window = team_inbox_metrics.resolve_performance_window(
        team_inbox_metrics.InboxPerformanceQuery(observed_at=observed_at)
    )

    assert window.start_at == observed_at - timedelta(days=30)
    assert window.end_at == observed_at
    with pytest.raises(
        team_inbox_metrics.InboxMetricsError,
        match="cannot exceed 366 days",
    ):
        team_inbox_metrics.resolve_performance_window(
            team_inbox_metrics.InboxPerformanceQuery(
                period_start_at=observed_at - timedelta(days=367),
                period_end_at=observed_at,
            )
        )


def test_team_performance_uses_constant_set_based_queries(db_session):
    observed_at = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
    for team_index in range(3):
        team = _team(db_session, name=f"Support {team_index}")
        for conversation_index in range(2):
            first_at = observed_at - timedelta(
                days=team_index + 1, minutes=conversation_index
            )
            conversation = _conversation(db_session, team, first_at=first_at)
            _message(
                db_session,
                conversation,
                direction=InboxMessageDirection.inbound.value,
                at=first_at,
            )
    db_session.commit()

    statements: list[str] = []

    def record_statement(
        _connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ):
        statements.append(statement)

    bind = db_session.get_bind()
    event.listen(bind, "before_cursor_execute", record_statement)
    try:
        page = team_inbox_metrics.team_performance_page(
            db_session,
            query=team_inbox_metrics.InboxPerformanceQuery(
                observed_at=observed_at,
                limit=None,
            ),
            response_sla_seconds=900,
        )
    finally:
        event.remove(bind, "before_cursor_execute", record_statement)

    assert len(page.rows) == 3
    assert len(statements) <= 5
    assert all(
        "inbox_messages.body" not in statement.lower() for statement in statements
    )


def test_team_performance_searches_and_paginates_teams(db_session):
    observed_at = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
    for name in ("Billing Support", "Network Support", "Sales"):
        _team(db_session, name=name)
    db_session.commit()

    page = team_inbox_metrics.team_performance_page(
        db_session,
        query=team_inbox_metrics.InboxPerformanceQuery(
            observed_at=observed_at,
            search="support",
            limit=1,
            offset=1,
        ),
        response_sla_seconds=900,
    )

    assert page.total_count == 2
    assert [row.service_team_name for row in page.rows] == ["Network Support"]


def test_escalation_page_paginates_candidates_and_preserves_full_totals(db_session):
    team = _team(db_session)
    observed_at = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
    for index in range(3):
        first_at = observed_at - timedelta(hours=index + 2)
        conversation = _conversation(db_session, team, first_at=first_at)
        _message(
            db_session,
            conversation,
            direction=InboxMessageDirection.inbound.value,
            at=first_at,
        )
    db_session.commit()

    page = team_inbox_metrics.escalation_page(
        db_session,
        query=team_inbox_metrics.InboxEscalationQuery(
            response_sla_seconds=60,
            queue_sla_seconds=60,
            limit=1,
            offset=1,
            observed_at=observed_at,
        ),
    )

    assert len(page.rows) == 1
    assert page.total_count == 3
    assert page.response_breach_count == 3
    assert page.queue_breach_count == 3
    assert page.no_agent_count == 3


def test_team_performance_metrics_tracks_response_and_queue_wait(db_session):
    team = _team(db_session)
    base = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    conversation = _conversation(db_session, team, first_at=base)
    _message(
        db_session,
        conversation,
        direction=InboxMessageDirection.inbound.value,
        at=base,
    )
    _message(
        db_session,
        conversation,
        direction=InboxMessageDirection.outbound.value,
        at=base + timedelta(minutes=12),
    )
    user, person = add_bound_staff_user(db_session)
    person_id = user.id
    db_session.add(
        InboxConversationAssignment(
            conversation_id=conversation.id,
            service_team_id=team.id,
            person_id=person.id,
            assigned_at=base + timedelta(minutes=4),
            is_active=True,
        )
    )
    db_session.commit()

    metrics = team_inbox_metrics.team_performance_metrics(
        db_session,
        team.id,
        response_sla_seconds=900,
        now=base + timedelta(hours=1),
    )

    assert metrics.conversation_count == 1
    assert metrics.open_count == 1
    assert metrics.assigned_open_count == 1
    assert metrics.unassigned_open_count == 0
    assert metrics.inbound_message_count == 1
    assert metrics.outbound_message_count == 1
    assert metrics.responded_count == 1
    assert metrics.response_sla_breached_count == 0
    assert metrics.average_first_response_seconds == 720
    assert metrics.average_queue_wait_seconds == 240


def test_team_performance_metrics_counts_pending_response_breach(db_session):
    team = _team(db_session)
    base = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    conversation = _conversation(db_session, team, first_at=base)
    _message(
        db_session,
        conversation,
        direction=InboxMessageDirection.inbound.value,
        at=base,
    )
    db_session.commit()

    metrics = team_inbox_metrics.team_performance_metrics(
        db_session,
        team.id,
        response_sla_seconds=600,
        now=base + timedelta(minutes=20),
    )

    assert metrics.open_count == 1
    assert metrics.unassigned_open_count == 1
    assert metrics.responded_count == 0
    assert metrics.response_sla_breached_count == 1
    assert metrics.average_first_response_seconds is None


def test_resolved_conversation_is_not_open(db_session):
    team = _team(db_session)
    base = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    conversation = _conversation(db_session, team, first_at=base)
    conversation.status = InboxConversationStatus.resolved.value
    _message(
        db_session,
        conversation,
        direction=InboxMessageDirection.inbound.value,
        at=base,
    )
    db_session.commit()

    metrics = team_inbox_metrics.team_performance_metrics(
        db_session, team.id, now=base + timedelta(days=1)
    )

    assert metrics.conversation_count == 1
    assert metrics.open_count == 0
    assert metrics.unassigned_open_count == 0


def test_agent_performance_metrics_tracks_active_assignments_and_wait(db_session):
    team = _team(db_session)
    base = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    person_id = uuid4()
    conversation = _conversation(db_session, team, first_at=base)
    conversation.status = InboxConversationStatus.resolved.value
    _message(
        db_session,
        conversation,
        direction=InboxMessageDirection.inbound.value,
        at=base,
    )
    _message(
        db_session,
        conversation,
        direction=InboxMessageDirection.outbound.value,
        at=base + timedelta(minutes=9),
        sent_by_person_id=person_id,
    )
    db_session.add(
        InboxConversationAssignment(
            conversation_id=conversation.id,
            service_team_id=team.id,
            person_id=person_id,
            assigned_at=base + timedelta(minutes=5),
            is_active=True,
        )
    )
    db_session.commit()

    metrics = team_inbox_metrics.agent_performance_metrics(
        db_session,
        service_team_id=team.id,
        person_id=person_id,
        now=base + timedelta(days=1),
    )

    assert metrics.active_assignment_count == 1
    assert metrics.handled_conversation_count == 1
    assert metrics.resolved_conversation_count == 1
    assert metrics.average_first_response_seconds == 540
    assert metrics.average_queue_wait_seconds == 300


def test_agent_first_response_ignores_automated_outbound_messages(db_session):
    team = _team(db_session)
    base = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    person_id = uuid4()
    conversation = _conversation(db_session, team, first_at=base)
    _message(
        db_session,
        conversation,
        direction=InboxMessageDirection.inbound.value,
        at=base,
    )
    _message(
        db_session,
        conversation,
        direction=InboxMessageDirection.outbound.value,
        at=base + timedelta(minutes=1),
    )
    _message(
        db_session,
        conversation,
        direction=InboxMessageDirection.outbound.value,
        at=base + timedelta(minutes=6),
        sent_by_person_id=person_id,
    )
    db_session.commit()

    metrics = team_inbox_metrics.agent_performance_metrics(
        db_session,
        service_team_id=team.id,
        person_id=person_id,
        now=base + timedelta(days=1),
    )

    assert metrics.average_first_response_seconds == 360


def test_team_performance_report_uses_team_sla_metadata(db_session):
    team = _team(db_session)
    team.metadata_ = {"inbox_sla": {"first_response_seconds": "900"}}
    base = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    conversation = _conversation(db_session, team, first_at=base)
    _message(
        db_session,
        conversation,
        direction=InboxMessageDirection.inbound.value,
        at=base,
    )
    db_session.commit()

    rows = team_inbox_metrics.team_performance_report(
        db_session,
        response_sla_seconds=3600,
        now=base + timedelta(minutes=20),
    )

    assert len(rows) == 1
    assert rows[0].response_sla_seconds == 900
    assert rows[0].service_team_capabilities == ("customer_support",)
    assert rows[0].metrics.response_sla_breached_count == 1


def test_agent_performance_report_lists_active_team_members(db_session):
    team = _team(db_session)
    user, person = add_bound_staff_user(db_session)
    person_id = user.id
    db_session.add(
        ServiceTeamMember(
            team_id=team.id,
            person_id=person.id,
            is_active=True,
        )
    )
    base = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    conversation = _conversation(db_session, team, first_at=base)
    conversation.status = InboxConversationStatus.resolved.value
    db_session.add(
        InboxConversationAssignment(
            conversation_id=conversation.id,
            service_team_id=team.id,
            person_id=person_id,
            assigned_at=base + timedelta(minutes=3),
            is_active=True,
        )
    )
    db_session.commit()

    rows = team_inbox_metrics.agent_performance_report(
        db_session,
        service_team_id=team.id,
        now=base + timedelta(days=1),
    )

    assert len(rows) == 1
    assert rows[0].person_id == person_id
    assert rows[0].agent_name == "Test Staff"
    assert rows[0].service_team_capabilities == ("customer_support",)
    assert rows[0].metrics.active_assignment_count == 1
    assert rows[0].metrics.handled_conversation_count == 1
    assert rows[0].metrics.resolved_conversation_count == 1
    assert rows[0].metrics.average_queue_wait_seconds == 180

    projected_rows = admin_reports._inbox_agent_rows(rows)
    assert projected_rows[0]["agent_name"] == "Test Staff"
    assert "person_id" not in projected_rows[0]

    filtered_rows = team_inbox_metrics.agent_performance_report(
        db_session,
        service_team_id=team.id,
        search="Test Staff",
        now=base + timedelta(days=1),
    )
    assert len(filtered_rows) == 1
    assert (
        team_inbox_metrics.agent_performance_report(
            db_session,
            service_team_id=team.id,
            search="does-not-exist",
            now=base + timedelta(days=1),
        )
        == []
    )


def test_analytics_api_returns_inbox_agent_performance_resolved_count(db_session):
    team = _team(db_session)
    user, person = add_bound_staff_user(db_session)
    db_session.add(
        ServiceTeamMember(
            team_id=team.id,
            person_id=person.id,
            is_active=True,
        )
    )
    base = datetime.now(UTC) - timedelta(days=1)
    conversation = _conversation(db_session, team, first_at=base)
    conversation.status = InboxConversationStatus.resolved.value
    _message(
        db_session,
        conversation,
        direction=InboxMessageDirection.inbound.value,
        at=base,
    )
    _message(
        db_session,
        conversation,
        direction=InboxMessageDirection.outbound.value,
        at=base + timedelta(minutes=4),
        sent_by_person_id=user.id,
    )
    db_session.add(
        InboxConversationAssignment(
            conversation_id=conversation.id,
            service_team_id=team.id,
            person_id=user.id,
            assigned_at=base + timedelta(minutes=1),
            is_active=False,
        )
    )
    db_session.commit()

    response = analytics_api.list_inbox_agent_performance(
        service_team_id=str(team.id),
        include_inactive_members=False,
        limit=50,
        offset=0,
        db=db_session,
    )

    assert response["count"] == 1
    item = response["items"][0]
    assert item.person_id == user.id
    assert item.handled_conversation_count == 1
    assert item.resolved_conversation_count == 1
    assert item.average_first_response_seconds == 240


def test_crm_agent_performance_report_includes_resolved_conversations(db_session):
    team = _team(db_session)
    user, person = add_bound_staff_user(db_session)
    db_session.add(
        ServiceTeamMember(
            team_id=team.id,
            person_id=person.id,
            is_active=True,
        )
    )
    base = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    conversation = _conversation(db_session, team, first_at=base)
    _message(
        db_session,
        conversation,
        direction=InboxMessageDirection.inbound.value,
        at=base,
    )
    _message(
        db_session,
        conversation,
        direction=InboxMessageDirection.outbound.value,
        at=base + timedelta(minutes=7),
        sent_by_person_id=user.id,
    )
    db_session.add(
        InboxConversationAssignment(
            conversation_id=conversation.id,
            service_team_id=team.id,
            person_id=user.id,
            assigned_at=base + timedelta(minutes=2),
            is_active=False,
        )
    )
    team_inbox_status.apply_status_transition(
        db_session,
        conversation=conversation,
        status=InboxConversationStatus.resolved,
        actor_person_id=user.id,
        reason=team_inbox_status.InboxStatusReason.operator_change,
        source_id="test:crm-agent-resolution",
        occurred_at=base + timedelta(minutes=30),
    )
    db_session.commit()

    report = crm_reporting.get_report(
        db_session,
        slug=crm_reporting.CrmReportSlug.AGENT_PERFORMANCE,
        query=crm_reporting.CrmReportQuery(
            date_from=base.date(),
            date_to=base.date(),
        ),
    )

    assert "Resolved" in report.columns
    metric_values = {metric.label: metric.value for metric in report.metrics}
    assert metric_values["Assigned chats"] == "1"
    assert metric_values["Resolved chats"] == "1"
    assert report.rows == (
        (
            "Test Staff",
            "Support",
            "1",
            "1",
            "0",
            "7.0m",
            "Low evidence / Not scored",
        ),
    )


def test_agent_performance_analytics_matches_raw_inbox_events(db_session):
    team = _team(db_session)
    user, person = add_bound_staff_user(db_session)
    db_session.add(
        ServiceTeamMember(team_id=team.id, person_id=person.id, is_active=True)
    )
    base = datetime(2026, 8, 10, 8, 0, tzinfo=UTC)
    conversation = _conversation(db_session, team, first_at=base)
    _message(
        db_session,
        conversation,
        direction=InboxMessageDirection.inbound.value,
        at=base,
    )
    _message(
        db_session,
        conversation,
        direction=InboxMessageDirection.outbound.value,
        at=base + timedelta(minutes=5),
        sent_by_person_id=user.id,
    )
    db_session.add(
        InboxConversationAssignment(
            conversation_id=conversation.id,
            service_team_id=team.id,
            person_id=user.id,
            assigned_at=base + timedelta(minutes=1),
            is_active=True,
        )
    )
    team_inbox_status.apply_status_transition(
        db_session,
        conversation=conversation,
        status=InboxConversationStatus.resolved,
        actor_person_id=user.id,
        reason=team_inbox_status.InboxStatusReason.operator_change,
        source_id="test:agent-analytics-resolution",
        occurred_at=base + timedelta(minutes=30),
    )
    db_session.commit()

    page = team_inbox_metrics.agent_performance_analytics(
        db_session,
        query=team_inbox_metrics.InboxAgentPerformanceQuery(
            start_at=base - timedelta(hours=1),
            end_at=base + timedelta(days=1),
            page=1,
            per_page=10,
        ),
    )

    assert page.total == 1
    assert page.summary.agent_count == 1
    assert page.summary.assigned_conversation_count == 1
    assert page.summary.resolved_conversation_count == 1
    assert page.summary.active_assignment_count == 1
    assert page.summary.average_resolution_seconds == 1800
    assert page.summary.average_first_response_seconds == 300
    row = page.rows[0]
    assert row.person_id == user.id
    assert row.agent_name == "Test Staff"
    assert row.assigned_conversation_count == 1
    assert row.resolved_conversation_count == 1
    assert row.average_resolution_seconds == 1800
    assert row.average_first_response_seconds == 300


def test_agent_performance_analytics_filters_event_time_and_search(db_session):
    team = _team(db_session)
    user, person = add_bound_staff_user(db_session, email="ada@example.test")
    user.display_name = "Ada Agent"
    db_session.add(
        ServiceTeamMember(team_id=team.id, person_id=person.id, is_active=True)
    )
    base = datetime(2026, 8, 10, 8, 0, tzinfo=UTC)
    old_conversation = _conversation(
        db_session, team, first_at=base - timedelta(days=40)
    )
    db_session.add(
        InboxConversationAssignment(
            conversation_id=old_conversation.id,
            service_team_id=team.id,
            person_id=user.id,
            assigned_at=base - timedelta(days=40),
            is_active=False,
        )
    )
    db_session.commit()

    page = team_inbox_metrics.agent_performance_analytics(
        db_session,
        query=team_inbox_metrics.InboxAgentPerformanceQuery(
            start_at=base,
            end_at=base + timedelta(days=1),
            page=1,
            per_page=10,
            search="Ada",
        ),
    )

    assert page.total == 1
    assert page.rows[0].agent_name == "Ada Agent"
    assert page.rows[0].assigned_conversation_count == 0
    assert page.rows[0].resolved_conversation_count == 0
    assert page.rows[0].active_assignment_count == 0
    assert (
        team_inbox_metrics.agent_performance_analytics(
            db_session,
            query=team_inbox_metrics.InboxAgentPerformanceQuery(
                start_at=base,
                end_at=base + timedelta(days=1),
                page=1,
                per_page=10,
                search="missing",
            ),
        ).total
        == 0
    )


def test_agent_performance_analytics_paginates_agent_team_rows_in_sql(db_session):
    team = _team(db_session)
    for index in range(11):
        user, person = add_bound_staff_user(
            db_session, email=f"agent-{index:02d}@example.test"
        )
        user.display_name = f"Agent {index:02d}"
        db_session.add(
            ServiceTeamMember(team_id=team.id, person_id=person.id, is_active=True)
        )
    db_session.commit()
    base = datetime(2026, 8, 1, tzinfo=UTC)

    first = team_inbox_metrics.agent_performance_analytics(
        db_session,
        query=team_inbox_metrics.InboxAgentPerformanceQuery(
            start_at=base,
            end_at=base + timedelta(days=31),
            page=1,
            per_page=10,
        ),
    )
    second = team_inbox_metrics.agent_performance_analytics(
        db_session,
        query=team_inbox_metrics.InboxAgentPerformanceQuery(
            start_at=base,
            end_at=base + timedelta(days=31),
            page=2,
            per_page=10,
        ),
    )

    assert first.total == 11
    assert len(first.rows) == 10
    assert first.has_next is True
    assert len(second.rows) == 1
    assert second.has_previous is True


def test_agent_performance_analytics_without_sla_keeps_current_metrics(db_session):
    team = _team(db_session)
    user, person = add_bound_staff_user(db_session)
    db_session.add(
        ServiceTeamMember(team_id=team.id, person_id=person.id, is_active=True)
    )
    base = datetime(2026, 8, 10, 8, 0, tzinfo=UTC)
    conversation = _conversation(db_session, team, first_at=base)
    _message(
        db_session,
        conversation,
        direction=InboxMessageDirection.inbound.value,
        at=base,
    )
    _message(
        db_session,
        conversation,
        direction=InboxMessageDirection.outbound.value,
        at=base + timedelta(minutes=5),
        sent_by_person_id=user.id,
    )
    db_session.add(
        InboxConversationAssignment(
            conversation_id=conversation.id,
            service_team_id=team.id,
            person_id=user.id,
            assigned_at=base,
            is_active=True,
        )
    )
    db_session.commit()

    page = team_inbox_metrics.agent_performance_analytics(
        db_session,
        query=team_inbox_metrics.InboxAgentPerformanceQuery(
            start_at=base - timedelta(minutes=1),
            end_at=base + timedelta(days=1),
            per_page=10,
        ),
    )

    assert page.summary.assigned_conversation_count == 1
    assert page.summary.active_assignment_count == 1
    assert page.summary.average_first_response_seconds == 300
    assert page.summary.sla_configured is False
    assert page.rows[0].first_response_sla_seconds is None
    assert page.rows[0].resolution_sla_seconds is None


def test_team_sla_expression_casts_empty_threshold_for_postgres():
    expr = team_inbox_metrics._team_sla_expression(column("service_team_id"), {})
    statement = select(
        expr.is_not(None).label("has_sla"),
        (column("duration_seconds") > expr).label("breached"),
    )

    compiled = statement.compile(dialect=postgresql.dialect(paramstyle="numeric"))

    assert "CAST(:1 AS INTEGER) IS NOT NULL" in str(compiled)
    assert "duration_seconds > CAST(:1 AS INTEGER)" in str(compiled)


def test_team_sla_expression_casts_default_for_postgres():
    expr = team_inbox_metrics._team_sla_expression(
        column("service_team_id"), {uuid4(): 600}
    )
    statement = select(expr.is_not(None).label("has_sla"))

    compiled = statement.compile(dialect=postgresql.dialect(paramstyle="numeric"))

    assert "ELSE CAST(:3 AS INTEGER)" in str(compiled)


def test_agent_performance_analytics_returns_sla_met_and_breached_counts(db_session):
    team = _team(db_session)
    team.metadata_ = {
        "inbox_sla": {
            "first_response_seconds": 600,
            "resolution_sla_seconds": 1200,
        }
    }
    user, person = add_bound_staff_user(db_session)
    db_session.add(
        ServiceTeamMember(team_id=team.id, person_id=person.id, is_active=True)
    )
    base = datetime(2026, 8, 10, 8, 0, tzinfo=UTC)
    for index, (response_minutes, resolution_minutes) in enumerate(((5, 15), (15, 30))):
        first_at = base + timedelta(hours=index)
        conversation = _conversation(db_session, team, first_at=first_at)
        _message(
            db_session,
            conversation,
            direction=InboxMessageDirection.inbound.value,
            at=first_at,
        )
        _message(
            db_session,
            conversation,
            direction=InboxMessageDirection.outbound.value,
            at=first_at + timedelta(minutes=response_minutes),
            sent_by_person_id=user.id,
        )
        db_session.add(
            InboxConversationAssignment(
                conversation_id=conversation.id,
                service_team_id=team.id,
                person_id=user.id,
                assigned_at=first_at,
                is_active=False,
            )
        )
        team_inbox_status.apply_status_transition(
            db_session,
            conversation=conversation,
            status=InboxConversationStatus.resolved,
            actor_person_id=user.id,
            reason=team_inbox_status.InboxStatusReason.operator_change,
            source_id=f"test:agent-sla:{index}",
            occurred_at=first_at + timedelta(minutes=resolution_minutes),
        )
    db_session.commit()

    page = team_inbox_metrics.agent_performance_analytics(
        db_session,
        query=team_inbox_metrics.InboxAgentPerformanceQuery(
            start_at=base - timedelta(minutes=1),
            end_at=base + timedelta(days=1),
            per_page=10,
        ),
    )
    row = page.rows[0]

    assert row.first_response_sla_met_count == 1
    assert row.first_response_sla_breached_count == 1
    assert row.first_response_sla_rate == 0.5
    assert row.resolution_sla_met_count == 1
    assert row.resolution_sla_breached_count == 1
    assert row.resolution_sla_rate == 0.5
    assert page.summary.sla_configured is True


def test_agent_team_filter_limits_totals_and_combines_with_search(db_session):
    support = _team(db_session, "Support")
    billing = _team(db_session, "Billing")
    base = datetime(2026, 8, 10, 8, 0, tzinfo=UTC)
    for index, (team, display_name) in enumerate(
        ((support, "Ada Support"), (billing, "Bola Billing"))
    ):
        user, person = add_bound_staff_user(
            db_session, email=f"agent-filter-{index}@example.test"
        )
        user.display_name = display_name
        db_session.add(
            ServiceTeamMember(team_id=team.id, person_id=person.id, is_active=True)
        )
        conversation = _conversation(db_session, team, first_at=base)
        db_session.add(
            InboxConversationAssignment(
                conversation_id=conversation.id,
                service_team_id=team.id,
                person_id=user.id,
                assigned_at=base,
                is_active=True,
            )
        )
    db_session.commit()

    page = team_inbox_metrics.agent_performance_analytics(
        db_session,
        query=team_inbox_metrics.InboxAgentPerformanceQuery(
            start_at=base - timedelta(minutes=1),
            end_at=base + timedelta(days=1),
            per_page=10,
            service_team_id=support.id,
            search="Ada",
        ),
    )

    assert page.total == 1
    assert page.summary.agent_count == 1
    assert page.summary.activity_agent_count == 1
    assert page.summary.assigned_conversation_count == 1
    assert page.summary.active_assignment_count == 1
    assert page.rows[0].service_team_id == support.id
    assert page.rows[0].agent_name == "Ada Support"


def test_analytics_api_returns_inbox_team_performance(db_session):
    team = _team(db_session)
    base = datetime.now(UTC) - timedelta(days=1)
    conversation = _conversation(db_session, team, first_at=base)
    _message(
        db_session,
        conversation,
        direction=InboxMessageDirection.inbound.value,
        at=base,
    )
    _message(
        db_session,
        conversation,
        direction=InboxMessageDirection.outbound.value,
        at=base + timedelta(minutes=5),
    )
    db_session.commit()

    response = analytics_api.list_inbox_team_performance(
        response_sla_seconds=600,
        limit=50,
        offset=0,
        db=db_session,
    )

    assert response["count"] == 1
    item = response["items"][0]
    assert item.service_team_id == team.id
    assert item.service_team_name == "Support"
    assert item.service_team_capabilities == ("customer_support",)
    assert item.responded_count == 1
    assert item.response_rate == 1.0
    assert item.response_sla_breach_rate == 0.0


def test_escalation_candidates_flag_breached_unassigned_conversation(db_session):
    team = _team(db_session)
    team.metadata_ = {
        "inbox_sla": {
            "first_response_seconds": 600,
            "assignment_seconds": 300,
        }
    }
    base = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    conversation = _conversation(db_session, team, first_at=base)
    conversation.contact_address = "customer@example.com"
    _message(
        db_session,
        conversation,
        direction=InboxMessageDirection.inbound.value,
        at=base,
    )
    db_session.commit()

    candidates = team_inbox_metrics.escalation_candidates(
        db_session,
        now=base + timedelta(minutes=20),
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.conversation_id == conversation.id
    assert candidate.service_team_id == team.id
    assert candidate.service_team_capabilities == ("customer_support",)
    assert candidate.contact_address == "customer@example.com"
    assert candidate.response_sla_seconds == 600
    assert candidate.queue_sla_seconds == 300
    assert candidate.pending_response_seconds == 1200
    assert candidate.queue_wait_seconds == 1200
    assert candidate.available_agent_count == 0
    assert candidate.reasons == (
        "response_sla_breached",
        "unassigned_queue_breached",
        "no_available_agent",
    )


def test_escalation_candidates_ignore_responded_assigned_conversation(db_session):
    team = _team(db_session)
    user, person = add_bound_staff_user(db_session)
    person_id = user.id
    db_session.add(
        ServiceTeamMember(team_id=team.id, person_id=person.id, is_active=True)
    )
    db_session.add(
        InboxAgentPresence(
            person_id=person_id,
            status=InboxAgentPresenceStatus.online.value,
            max_concurrent_conversations=3,
            last_seen_at=datetime.now(UTC),
        )
    )
    base = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    conversation = _conversation(db_session, team, first_at=base)
    _message(
        db_session,
        conversation,
        direction=InboxMessageDirection.inbound.value,
        at=base,
    )
    _message(
        db_session,
        conversation,
        direction=InboxMessageDirection.outbound.value,
        at=base + timedelta(minutes=2),
    )
    db_session.add(
        InboxConversationAssignment(
            conversation_id=conversation.id,
            service_team_id=team.id,
            person_id=person_id,
            assigned_at=base + timedelta(minutes=1),
            is_active=True,
        )
    )
    db_session.commit()

    candidates = team_inbox_metrics.escalation_candidates(
        db_session,
        response_sla_seconds=60,
        queue_sla_seconds=60,
        now=base + timedelta(minutes=20),
    )

    assert candidates == []


def test_analytics_api_returns_escalation_candidates(db_session):
    team = _team(db_session)
    base = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)
    conversation = _conversation(db_session, team, first_at=base)
    _message(
        db_session,
        conversation,
        direction=InboxMessageDirection.inbound.value,
        at=base,
    )
    db_session.commit()

    response = analytics_api.list_inbox_escalation_candidates(
        response_sla_seconds=300,
        queue_sla_seconds=300,
        limit=50,
        offset=0,
        db=db_session,
    )

    assert response["count"] == 1
    item = response["items"][0]
    assert item.conversation_id == conversation.id
    assert item.service_team_id == team.id
    assert item.service_team_capabilities == ("customer_support",)
    assert "response_sla_breached" in item.reasons
    assert "unassigned_queue_breached" in item.reasons


def test_inbox_escalation_report_export_returns_candidates(db_session):
    team = _team(db_session)
    base = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)
    conversation = _conversation(db_session, team, first_at=base)
    conversation.subject = "Router offline"
    conversation.contact_address = "customer@example.com"
    _message(
        db_session,
        conversation,
        direction=InboxMessageDirection.inbound.value,
        at=base,
    )
    db_session.commit()

    response = admin_reports.reports_inbox_escalations_export(
        response_sla_seconds=300,
        queue_sla_seconds=300,
        db=db_session,
    )
    content = response.body.decode()

    assert (
        "attachment; filename=inbox-escalations.csv"
        in response.headers["Content-Disposition"]
    )
    assert "Router offline" in content
    assert "service_team_capabilities" in content
    assert "customer_support" in content
    assert "Response SLA breached" in content
    assert "Unassigned queue breached" in content


def test_inbox_escalation_report_is_visible_from_reports_hub():
    links = [
        link
        for section in admin_reports.REPORT_HUB_SECTIONS
        for link in section["links"]
    ]

    assert {
        "name": "Inbox Escalations",
        "url": "/admin/reports/inbox-escalations",
        "description": "Conversations that need supervisor attention",
        "permission": "reports:support:read",
    } in links


def test_inbox_escalation_report_action_auto_assigns_candidate(db_session):
    team = _team(db_session)
    user, person = add_bound_staff_user(db_session)
    person_id = user.id
    db_session.add(
        ServiceTeamMember(team_id=team.id, person_id=person.id, is_active=True)
    )
    db_session.add(
        InboxAgentPresence(
            person_id=person_id,
            status=InboxAgentPresenceStatus.online.value,
            max_concurrent_conversations=3,
            last_seen_at=datetime.now(UTC),
        )
    )
    base = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)
    conversation = _conversation(db_session, team, first_at=base)
    _message(
        db_session,
        conversation,
        direction=InboxMessageDirection.inbound.value,
        at=base,
    )
    db_session.commit()

    response = admin_reports.reports_inbox_escalation_action(
        str(conversation.id),
        request=SimpleNamespace(state=SimpleNamespace()),
        service_team_id=str(team.id),
        action="auto_assign",
        reason="Admin escalation report",
        next="/admin/reports/inbox-escalations?response_sla_seconds=300",
        db=db_session,
    )

    assignment = db_session.query(InboxConversationAssignment).one()
    db_session.refresh(conversation)
    assert response.status_code == 303
    assert "status=success" in response.headers["location"]
    assert assignment.person_id == person_id
    assert assignment.is_active is True
    assert conversation.metadata_["last_inbox_escalation"]["reason"] == (
        "Admin escalation report"
    )


def test_inbox_escalation_report_reply_queues_team_message(db_session):
    team = _team(db_session)
    conversation = _conversation(
        db_session,
        team,
        first_at=datetime(2026, 1, 1, 8, 0, tzinfo=UTC),
    )
    db_session.commit()

    response = admin_reports.reports_inbox_escalation_reply(
        str(conversation.id),
        request=SimpleNamespace(state=SimpleNamespace()),
        body_text="We are checking this now.",
        next="/admin/reports/inbox-escalations",
        db=db_session,
    )

    message = db_session.query(InboxMessage).one()
    notification = db_session.query(Notification).one()
    assert response.status_code == 303
    assert "status=success" in response.headers["location"]
    assert notification.recipient == "customer@example.com"
    assert notification.metadata_["activity"] == "support_ticket"
    assert message.direction == InboxMessageDirection.outbound.value
    assert message.notification_id == notification.id
    assert message.metadata_["source_route"] == "admin_inbox_escalation_reply"
