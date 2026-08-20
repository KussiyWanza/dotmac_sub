from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from app.models.service_team import ServiceTeam, ServiceTeamMember, ServiceTeamType
from app.models.team_inbox import (
    InboxAgentPresence,
    InboxAgentPresenceEvent,
    InboxAgentPresenceStatus,
    InboxAuditEvidenceGrade,
    InboxConversation,
    InboxConversationAssignment,
    InboxConversationStatus,
    InboxRoutingEvent,
    InboxRoutingEventType,
    InboxStatusTransitionEvent,
)
from app.services import (
    team_inbox_assignment,
    team_inbox_audit,
    team_inbox_audit_reconstruction,
    team_inbox_status,
)
from scripts.one_off import team_inbox_lifecycle_audit as lifecycle_audit_cli
from tests.staff_identity_fixtures import add_bound_staff_user


def _team_and_agent(db_session):
    team = ServiceTeam(name="Audit team", team_type=ServiceTeamType.support.value)
    user, person = add_bound_staff_user(db_session)
    db_session.add(team)
    db_session.flush()
    db_session.add(
        ServiceTeamMember(team_id=team.id, person_id=person.id, is_active=True)
    )
    db_session.add(
        InboxAgentPresence(
            person_id=user.id,
            status=InboxAgentPresenceStatus.online.value,
            last_seen_at=datetime.now(UTC),
        )
    )
    db_session.flush()
    return team, user.id


def _conversation(db_session):
    conversation = InboxConversation(channel_type="email", status="open")
    db_session.add(conversation)
    db_session.flush()
    return conversation


def test_reassignment_closes_interval_through_authoritative_event(db_session):
    team, first_agent = _team_and_agent(db_session)
    _other_user, other_person = add_bound_staff_user(db_session)
    db_session.add(
        ServiceTeamMember(team_id=team.id, person_id=other_person.id, is_active=True)
    )
    db_session.add(
        InboxAgentPresence(
            person_id=_other_user.id,
            status=InboxAgentPresenceStatus.online.value,
            last_seen_at=datetime.now(UTC),
        )
    )
    db_session.flush()
    second_agent = _other_user.id
    conversation = _conversation(db_session)

    team_inbox_assignment.assign_conversation_to_agent(
        db_session,
        conversation=conversation,
        service_team_id=team.id,
        person_id=first_agent,
        source_id="test:first-assignment",
    )
    team_inbox_assignment.assign_conversation_to_agent(
        db_session,
        conversation=conversation,
        service_team_id=team.id,
        person_id=second_agent,
        source_id="test:reassignment",
    )
    db_session.flush()

    events = (
        db_session.query(InboxRoutingEvent)
        .order_by(InboxRoutingEvent.occurred_at)
        .all()
    )
    assignments = (
        db_session.query(InboxConversationAssignment)
        .order_by(InboxConversationAssignment.assigned_at)
        .all()
    )
    assert [event.event_type for event in events] == [
        InboxRoutingEventType.assigned,
        InboxRoutingEventType.reassigned,
    ]
    assert assignments[0].ended_at is not None
    assert assignments[0].ended_by_event_id == events[1].id
    assert assignments[1].is_active is True


def test_queue_appends_event_and_closes_assignment(db_session):
    team, agent = _team_and_agent(db_session)
    conversation = _conversation(db_session)
    team_inbox_assignment.assign_conversation_to_agent(
        db_session,
        conversation=conversation,
        service_team_id=team.id,
        person_id=agent,
        source_id="test:assigned-before-queue",
    )
    outcome = team_inbox_assignment.queue_conversation_for_team(
        db_session,
        conversation=conversation,
        service_team_id=team.id,
        source_id="test:queue",
    )
    db_session.flush()

    assignment = db_session.query(InboxConversationAssignment).one()
    event = db_session.query(InboxRoutingEvent).filter_by(source_id="test:queue").one()
    assert outcome.kind == "queued"
    assert event.event_type is InboxRoutingEventType.unassigned
    assert assignment.is_active is False
    assert assignment.ended_by_event_id == event.id


def test_status_and_presence_transitions_append_native_evidence(db_session):
    conversation = _conversation(db_session)
    actor_id = uuid4()
    team_inbox_status.apply_status_transition(
        db_session,
        conversation=conversation,
        status=InboxConversationStatus.resolved,
        actor_person_id=actor_id,
        reason=team_inbox_status.InboxStatusReason.operator_change,
        source_id="test:status",
    )
    team_inbox_assignment.set_agent_presence(
        db_session,
        person_id=actor_id,
        status=InboxAgentPresenceStatus.online.value,
        actor_person_id=actor_id,
        source_id="test:presence",
    )
    db_session.flush()

    status_event = db_session.query(InboxStatusTransitionEvent).one()
    presence_event = db_session.query(InboxAgentPresenceEvent).one()
    assert status_event.evidence_grade is InboxAuditEvidenceGrade.native
    assert status_event.previous_status == InboxConversationStatus.open.value
    assert presence_event.actor_person_id == actor_id


def test_reconstruction_preview_is_deterministic_and_preserves_unknown_end(db_session):
    team, agent = _team_and_agent(db_session)
    conversation = _conversation(db_session)
    assignment = InboxConversationAssignment(
        conversation_id=conversation.id,
        service_team_id=team.id,
        person_id=agent,
        assigned_at=datetime(2026, 1, 2, tzinfo=UTC),
        is_active=False,
    )
    db_session.add(assignment)
    db_session.flush()

    first = team_inbox_audit_reconstruction.preview_reconstruction(db_session)
    second = team_inbox_audit_reconstruction.preview_reconstruction(db_session)

    assert first.sha256 == second.sha256
    assert first.source_watermark == second.source_watermark
    assert {item.kind for item in first.items} == {
        team_inbox_audit_reconstruction.ReconstructionKind.assignment_started,
        team_inbox_audit_reconstruction.ReconstructionKind.assignment_end_unknown,
    }
    unknown = next(item for item in first.items if item.occurred_at is None)
    assert unknown.evidence_grade is InboxAuditEvidenceGrade.unknown
    report = lifecycle_audit_cli._manifest_report(first)
    assert report["sha256"] == first.sha256
    assert report["counts_by_evidence_grade"] == {
        "authoritative_historical": 1,
        "unknown": 1,
    }


def test_reconstruction_apply_is_hash_bound_and_replays(db_session, monkeypatch):
    team, agent = _team_and_agent(db_session)
    conversation = _conversation(db_session)
    db_session.add(
        InboxConversationAssignment(
            conversation_id=conversation.id,
            service_team_id=team.id,
            person_id=agent,
            assigned_at=datetime(2026, 1, 2, tzinfo=UTC),
            is_active=True,
        )
    )
    db_session.flush()
    preview = team_inbox_audit_reconstruction.preview_reconstruction(db_session)
    command = team_inbox_audit_reconstruction.ApplyReconstructionCommand(
        expected_manifest_sha256=preview.sha256,
        expected_source_watermark=preview.source_watermark,
        actor_person_id=uuid4(),
        approval_reference="CHANGE-42",
        idempotency_key="audit-reconstruction:test",
    )
    monkeypatch.setattr(
        team_inbox_audit_reconstruction,
        "execute_owner_command",
        lambda _db, *, operation, **_kwargs: operation(),
    )

    first = team_inbox_audit_reconstruction.apply_reconstruction(db_session, command)
    second = team_inbox_audit_reconstruction.apply_reconstruction(db_session, command)

    assert first == second
    assert first.applied == 1
    assert db_session.query(InboxRoutingEvent).count() == 1


def test_timeline_combines_events_and_reports_projection_drift(db_session):
    conversation = _conversation(db_session)
    team_inbox_status.apply_status_transition(
        db_session,
        conversation=conversation,
        status=InboxConversationStatus.pending,
        actor_person_id=None,
        reason=team_inbox_status.InboxStatusReason.operator_change,
        source_id="test:timeline-status",
    )
    db_session.flush()

    healthy = team_inbox_audit.conversation_audit_timeline(
        db_session, conversation_id=conversation.id
    )
    assert [entry.kind for entry in healthy.entries] == [
        team_inbox_audit.InboxAuditEntryKind.status
    ]
    assert healthy.findings == ()
    assert healthy.native_coverage_started_at is not None
    report = lifecycle_audit_cli._timeline_report(healthy)
    assert report["conversation_id"] == str(conversation.id)
    assert report["entries"][0]["source_id"] == "test:timeline-status"

    conversation.status = InboxConversationStatus.open.value
    db_session.flush()
    drifted = team_inbox_audit.conversation_audit_timeline(
        db_session, conversation_id=conversation.id
    )
    assert drifted.findings[0].kind is (
        team_inbox_audit.InboxAuditDriftKind.status_projection_mismatch
    )
