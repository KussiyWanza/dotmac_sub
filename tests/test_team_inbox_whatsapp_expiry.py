from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.models.csat import SupportCsatRequest
from app.models.service_team import ServiceTeam, ServiceTeamMember, ServiceTeamType
from app.models.team_inbox import (
    InboxAgentPresence,
    InboxConversation,
    InboxConversationAssignment,
    InboxConversationQueueEntry,
    InboxMessage,
    InboxMessageDirection,
    InboxQueueEntryStatus,
    InboxRoutingEvent,
    InboxStatusTransitionEvent,
)
from app.services import (
    team_inbox_assignment,
    team_inbox_channel_receive,
    team_inbox_maintenance,
    team_inbox_operations,
    team_inbox_projection,
    team_inbox_reply_window,
    team_inbox_status,
)
from app.services.owner_commands import CommandContext
from tests.staff_identity_fixtures import add_bound_staff_user

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)


def _context(name: str) -> CommandContext:
    return CommandContext.system(
        actor=f"test:{name}",
        scope="team-inbox:test",
        reason=name.replace("_", " "),
    )


def _team_and_agent(db_session, *, capacity: int = 10):
    team = ServiceTeam(
        name=f"Expiry {uuid4().hex[:8]}",
        team_type=ServiceTeamType.support.value,
    )
    db_session.add(team)
    user, person = add_bound_staff_user(db_session)
    db_session.add(
        ServiceTeamMember(team_id=team.id, person_id=person.id, is_active=True)
    )
    db_session.add(
        InboxAgentPresence(
            person_id=user.id,
            status="online",
            manual_override_status="online",
            max_concurrent_conversations=capacity,
            last_seen_at=NOW,
        )
    )
    db_session.flush()
    return team, user.id


def _whatsapp(
    db_session,
    *,
    inbound_at: datetime,
    team_id: UUID | None = None,
    thread_id: str | None = None,
) -> InboxConversation:
    conversation = InboxConversation(
        channel_type="whatsapp",
        status="open",
        is_active=True,
        contact_address=f"+23480{uuid4().int % 10**8:08d}",
        external_thread_id=thread_id,
        primary_service_team_id=team_id,
        first_message_at=inbound_at,
        last_message_at=inbound_at,
    )
    db_session.add(conversation)
    db_session.flush()
    db_session.add(
        InboxMessage(
            conversation_id=conversation.id,
            channel_type="whatsapp",
            direction=InboxMessageDirection.inbound.value,
            body="Hello",
            received_at=inbound_at,
            metadata_={"reply_window_qualifying": True},
        )
    )
    db_session.flush()
    return conversation


def _assign(db_session, conversation, team_id, person_id):
    assignment = InboxConversationAssignment(
        conversation_id=conversation.id,
        service_team_id=team_id,
        person_id=person_id,
        assigned_at=NOW - timedelta(hours=30),
        is_active=True,
    )
    db_session.add(assignment)
    db_session.flush()
    return assignment


def test_expiry_releases_assignment_and_queue_without_resolving(db_session):
    team, agent_id = _team_and_agent(db_session)
    assigned = _whatsapp(
        db_session, inbound_at=NOW - timedelta(hours=25), team_id=team.id
    )
    assignment = _assign(db_session, assigned, team.id, agent_id)
    queued = _whatsapp(
        db_session, inbound_at=NOW - timedelta(hours=26), team_id=team.id
    )
    entry = InboxConversationQueueEntry(
        conversation_id=queued.id,
        service_team_id=team.id,
        queue_position=1,
        status=InboxQueueEntryStatus.queued.value,
        entered_at=NOW - timedelta(hours=26),
    )
    db_session.add(entry)
    db_session.commit()

    result = team_inbox_maintenance.sweep_expired_whatsapp_windows(
        db_session,
        team_inbox_maintenance.WhatsAppWindowExpirySweepCommand(
            context=_context("expiry_sweep"), now=NOW
        ),
    )

    db_session.refresh(assignment)
    db_session.refresh(entry)
    event = db_session.query(InboxRoutingEvent).one()
    assert result.assignments_released == 1
    assert result.queues_cancelled == 1
    assert assignment.is_active is False
    assert assignment.ended_by_event_id == event.id
    assert event.reason_code == "whatsapp_window_expired"
    assert entry.status == InboxQueueEntryStatus.cancelled.value
    assert entry.metadata_["settlement_reason"] == "whatsapp_window_expired"
    assert assigned.status == queued.status == "open"
    db_session.rollback()

    repeated = team_inbox_maintenance.sweep_expired_whatsapp_windows(
        db_session,
        team_inbox_maintenance.WhatsAppWindowExpirySweepCommand(
            context=_context("expiry_sweep_again"), now=NOW
        ),
    )
    assert repeated.assignments_released == 0
    assert db_session.query(InboxRoutingEvent).count() == 1


def test_expired_whatsapp_does_not_consume_capacity_or_accept_assignment(db_session):
    team, agent_id = _team_and_agent(db_session, capacity=3)
    active = _whatsapp(db_session, inbound_at=NOW - timedelta(hours=1), team_id=team.id)
    expired = _whatsapp(
        db_session, inbound_at=NOW - timedelta(hours=25), team_id=team.id
    )
    _assign(db_session, active, team.id, agent_id)
    _assign(db_session, expired, team.id, agent_id)
    db_session.commit()

    snapshot = team_inbox_assignment.agent_availability_snapshots(
        db_session, (agent_id,), now=NOW
    )[agent_id]
    result = team_inbox_assignment.assign_conversation_to_agent(
        db_session,
        conversation=expired,
        service_team_id=team.id,
        person_id=agent_id,
        now=NOW,
    )
    projection = team_inbox_projection.get_conversation_projection(
        db_session,
        conversation_id=expired.id,
        actor_person_id=agent_id,
        include_contact_candidates=False,
        include_catalogue_options=False,
    )

    assert snapshot.active_conversation_count == 1
    assert snapshot.available_capacity == 2
    assert result.kind == "reply_window_expired"
    assert projection is not None
    assert projection.action_eligibility.can_assign is False


def test_expired_unidentified_conversation_resolves_internally_with_audit(db_session):
    team, agent_id = _team_and_agent(db_session)
    conversation = _whatsapp(
        db_session, inbound_at=NOW - timedelta(hours=25), team_id=team.id
    )
    assignment = _assign(db_session, conversation, team.id, agent_id)
    initial_message_count = db_session.query(InboxMessage).count()

    readiness = team_inbox_status.resolution_readiness(
        db_session, conversation, evaluated_at=NOW
    )
    outcome = team_inbox_status.apply_status_transition(
        db_session,
        conversation=conversation,
        status=team_inbox_status.InboxConversationStatus.resolved,
        actor_person_id=agent_id,
        reason=team_inbox_status.InboxStatusReason.operator_change,
        resolution_reason=team_inbox_status.InboxResolutionReason.whatsapp_window_expired,
        occurred_at=NOW,
    )
    db_session.flush()

    event = db_session.get(InboxStatusTransitionEvent, outcome.event_id)
    db_session.refresh(assignment)
    assert readiness.can_agent_resolve is True
    assert readiness.requires_resolution_reason is True
    assert readiness.classification.value == "unresolved"
    assert conversation.status == "resolved"
    assert assignment.is_active is False
    assert event.resolution_reason == "whatsapp_window_expired"
    assert event.channel_state_at_resolution == "expired"
    assert db_session.query(InboxMessage).count() == initial_message_count
    assert db_session.query(SupportCsatRequest).count() == 0


def test_expired_resolution_requires_reason_and_outbound_never_extends_window(
    db_session,
):
    conversation = _whatsapp(db_session, inbound_at=NOW - timedelta(hours=25))
    db_session.add(
        InboxMessage(
            conversation_id=conversation.id,
            channel_type="whatsapp",
            direction=InboxMessageDirection.outbound.value,
            body="Approved template",
            sent_at=NOW,
            metadata_={"whatsapp_template": {"name": "approved"}},
        )
    )
    with pytest.raises(
        team_inbox_status.InboxResolutionError,
        match="Choose a resolution reason",
    ):
        team_inbox_status.apply_status_transition(
            db_session,
            conversation=conversation,
            status=team_inbox_status.InboxConversationStatus.resolved,
            actor_person_id=uuid4(),
            reason=team_inbox_status.InboxStatusReason.operator_change,
            occurred_at=NOW,
        )
    assert (
        team_inbox_reply_window.decide_reply_window(
            db_session, conversation=conversation, now=NOW
        ).status
        is team_inbox_reply_window.ReplyWindowStatus.expired
    )


def test_customer_inbound_after_expiry_releases_old_assignment_and_opens_window(
    db_session,
):
    team, agent_id = _team_and_agent(db_session)
    thread_id = f"whatsapp:{uuid4()}"
    conversation = _whatsapp(
        db_session,
        inbound_at=NOW - timedelta(hours=25),
        team_id=team.id,
        thread_id=thread_id,
    )
    old_assignment = _assign(db_session, conversation, team.id, agent_id)
    db_session.commit()

    received = team_inbox_channel_receive.receive_inbound_channel(
        db_session,
        team_inbox_channel_receive.InboundChannelPayload(
            channel_type="whatsapp",
            contact_address=conversation.contact_address or "",
            body="I am back",
            external_message_id=f"wamid.{uuid4()}",
            external_thread_id=thread_id,
            fallback_service_team_id=team.id,
            received_at=NOW + timedelta(minutes=1),
            metadata={"reply_window_qualifying": True},
        ),
    )
    db_session.flush()

    db_session.refresh(old_assignment)
    assert received.conversation_id == str(conversation.id)
    assert old_assignment.is_active is False
    assert (
        team_inbox_reply_window.decide_reply_window(
            db_session, conversation=conversation, now=NOW + timedelta(minutes=1)
        ).status
        is team_inbox_reply_window.ReplyWindowStatus.open
    )


def test_inbound_after_expired_resolved_thread_creates_new_conversation(db_session):
    team, agent_id = _team_and_agent(db_session)
    thread_id = f"whatsapp:{uuid4()}"
    prior = _whatsapp(
        db_session,
        inbound_at=NOW - timedelta(hours=25),
        team_id=team.id,
        thread_id=thread_id,
    )
    old_assignment = _assign(db_session, prior, team.id, agent_id)
    team_inbox_status.apply_status_transition(
        db_session,
        conversation=prior,
        status=team_inbox_status.InboxConversationStatus.resolved,
        actor_person_id=agent_id,
        reason=team_inbox_status.InboxStatusReason.operator_change,
        resolution_reason=team_inbox_status.InboxResolutionReason.whatsapp_window_expired,
        occurred_at=NOW,
    )
    db_session.commit()

    received = team_inbox_channel_receive.receive_inbound_channel(
        db_session,
        team_inbox_channel_receive.InboundChannelPayload(
            channel_type="whatsapp",
            contact_address=prior.contact_address or "",
            body="New issue",
            external_message_id=f"wamid.{uuid4()}",
            external_thread_id=thread_id,
            fallback_service_team_id=team.id,
            received_at=NOW + timedelta(minutes=1),
            metadata={"reply_window_qualifying": True},
        ),
    )

    db_session.refresh(old_assignment)
    assert received.conversation_id != str(prior.id)
    assert old_assignment.is_active is False


def test_historical_assignment_repair_is_dry_run_and_idempotent(db_session):
    team, agent_id = _team_and_agent(db_session)
    conversation = _whatsapp(
        db_session, inbound_at=NOW - timedelta(hours=25), team_id=team.id
    )
    assignment = _assign(db_session, conversation, team.id, agent_id)
    db_session.commit()

    dry_run = team_inbox_maintenance.repair_expired_whatsapp_assignments(
        db_session,
        team_inbox_maintenance.RepairExpiredWhatsAppAssignmentsCommand(
            context=_context("repair_preview"), dry_run=True, now=NOW
        ),
    )
    db_session.refresh(assignment)
    assert dry_run.stale_assignments_found == 1
    assert dry_run.assignments_released == 0
    assert assignment.is_active is True
    db_session.rollback()

    applied = team_inbox_maintenance.repair_expired_whatsapp_assignments(
        db_session,
        team_inbox_maintenance.RepairExpiredWhatsAppAssignmentsCommand(
            context=_context("repair_apply"), dry_run=False, now=NOW
        ),
    )
    repeated = team_inbox_maintenance.repair_expired_whatsapp_assignments(
        db_session,
        team_inbox_maintenance.RepairExpiredWhatsAppAssignmentsCommand(
            context=_context("repair_repeat"), dry_run=False, now=NOW
        ),
    )
    assert applied.assignments_released == 1
    assert repeated.assignments_released == 0
    assert repeated.already_correct == 1


def test_bulk_expired_resolution_is_internal_and_idempotent(db_session):
    conversations = tuple(
        _whatsapp(db_session, inbound_at=NOW - timedelta(hours=25 + index))
        for index in range(2)
    )
    initial_messages = db_session.query(InboxMessage).count()

    first = team_inbox_operations.bulk_update_status(
        db_session,
        conversation_ids=[row.id for row in conversations],
        status_value="resolved",
        actor_person_id=uuid4(),
        resolution_reason=team_inbox_status.InboxResolutionReason.whatsapp_window_expired,
    )
    repeated = team_inbox_operations.bulk_update_status(
        db_session,
        conversation_ids=[row.id for row in conversations],
        status_value="resolved",
        actor_person_id=uuid4(),
        resolution_reason=team_inbox_status.InboxResolutionReason.whatsapp_window_expired,
    )

    assert len(first["updated"]) == 2
    assert len(repeated["skipped"]) == 2
    assert db_session.query(InboxMessage).count() == initial_messages
    assert db_session.query(InboxConversationAssignment).count() == 0
    events = db_session.query(InboxStatusTransitionEvent).all()
    assert {event.channel_state_at_resolution for event in events} == {"expired"}
    assert {event.resolution_reason for event in events} == {"whatsapp_window_expired"}


def test_expired_whatsapp_is_not_stale_auto_resolved(db_session):
    conversation = _whatsapp(db_session, inbound_at=NOW - timedelta(hours=100))
    conversation.last_message_at = NOW - timedelta(hours=80)
    db_session.add(
        InboxMessage(
            conversation_id=conversation.id,
            channel_type="whatsapp",
            direction=InboxMessageDirection.outbound.value,
            body="A human follow-up before expiry",
            sent_at=NOW - timedelta(hours=80),
            metadata_={"sent_by_person_id": str(uuid4()), "sender_type": "agent"},
        )
    )
    db_session.flush()

    resolved = team_inbox_operations.auto_resolve_stale_conversations(
        db_session, stale_hours=72, now=NOW
    )

    assert resolved == 0
    assert conversation.status == "open"
