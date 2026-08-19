from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

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
from app.services import service_team_composition, team_inbox_metrics
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

    metrics = team_inbox_metrics.team_performance_metrics(db_session, team.id)

    assert metrics.conversation_count == 1
    assert metrics.open_count == 0
    assert metrics.unassigned_open_count == 0


def test_agent_performance_metrics_tracks_active_assignments_and_wait(db_session):
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
    )

    assert metrics.active_assignment_count == 1
    assert metrics.handled_conversation_count == 1
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
    )

    assert len(rows) == 1
    assert rows[0].person_id == str(person_id)
    assert rows[0].service_team_capabilities == ("customer_support",)
    assert rows[0].metrics.active_assignment_count == 1
    assert rows[0].metrics.average_queue_wait_seconds == 180

    filtered_rows = team_inbox_metrics.agent_performance_report(
        db_session,
        service_team_id=team.id,
        search="Test Staff",
    )
    assert len(filtered_rows) == 1
    assert (
        team_inbox_metrics.agent_performance_report(
            db_session,
            service_team_id=team.id,
            search="does-not-exist",
        )
        == []
    )


def test_analytics_api_returns_inbox_team_performance(db_session):
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
    assert candidate.conversation_id == str(conversation.id)
    assert candidate.service_team_id == str(team.id)
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
