from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.models.csat import SupportCsatRequest
from app.models.service_team import ServiceTeam, ServiceTeamMember, ServiceTeamType
from app.models.subscriber import Subscriber
from app.models.team_inbox import (
    InboxAgentPresence,
    InboxConversation,
    InboxConversationAssignment,
    InboxConversationQueueEntry,
    InboxCustomerCompletionPolicyVersion,
    InboxMessage,
    InboxMessageDirection,
    InboxQueueEntryStatus,
    InboxRoutingEvent,
    InboxStatusTransitionEvent,
)
from app.services import (
    team_inbox_assignment,
    team_inbox_channel_receive,
    team_inbox_commands,
    team_inbox_maintenance,
    team_inbox_operations,
    team_inbox_read,
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
    status: str = "open",
    thread_id: str | None = None,
) -> InboxConversation:
    conversation = InboxConversation(
        channel_type="whatsapp",
        status=status,
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
    db_session.refresh(assigned)
    db_session.refresh(queued)
    event = db_session.query(InboxRoutingEvent).one()
    assert result.assignments_released == 1
    assert result.queues_cancelled == 1
    assert assignment.is_active is False
    assert assignment.ended_at == NOW
    assert assignment.ended_by_event_id == event.id
    assert event.reason_code == "whatsapp_window_expired"
    assert entry.status == InboxQueueEntryStatus.cancelled.value
    assert entry.metadata_["settlement_reason"] == "whatsapp_window_expired"
    assert assigned.status == queued.status == "open"
    assert db_session.query(InboxMessage).count() == 2

    again = team_inbox_maintenance.sweep_expired_whatsapp_windows(
        db_session,
        team_inbox_maintenance.WhatsAppWindowExpirySweepCommand(
            context=_context("expiry_sweep_again"), now=NOW
        ),
    )
    assert again.assignments_released == 0
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

    assert snapshot.active_conversation_count == 1
    assert snapshot.available_capacity == 2
    assert result.kind == "reply_window_expired"
    assert team_inbox_read.queue_conversation_count(db_session) == 1
    assert (
        team_inbox_read.assigned_conversation_count(
            db_session,
            assigned_person_id=agent_id,
        )
        == 1
    )
    assert team_inbox_operations.queue_metrics(db_session).total_open == 1


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
    assert event.actor_person_id == agent_id
    assert event.occurred_at == NOW
    assert event.resolution_reason == "whatsapp_window_expired"
    assert event.channel_state_at_resolution == "expired"
    assert db_session.query(InboxMessage).count() == initial_message_count
    assert db_session.query(SupportCsatRequest).count() == 0
    assert (
        team_inbox_reply_window.decide_reply_window(
            db_session, conversation=conversation, now=NOW
        ).status
        is team_inbox_reply_window.ReplyWindowStatus.expired
    )


def test_expired_incomplete_customer_uses_only_the_controlled_bypass(db_session):
    policy = db_session.query(InboxCustomerCompletionPolicyVersion).first()
    if policy is None:
        policy = InboxCustomerCompletionPolicyVersion(
            version=1,
            required_fields=["name", "phone", "address"],
            decision_source="pytest",
        )
        db_session.add(policy)
        db_session.flush()
    customer = Subscriber(
        first_name="",
        last_name="",
        display_name="",
        email=f"{uuid4().hex}@example.test",
        phone="",
        address_line1="",
    )
    db_session.add(customer)
    db_session.flush()
    conversation = _whatsapp(db_session, inbound_at=NOW - timedelta(hours=25))
    conversation.subscriber_id = customer.id
    conversation.customer_completion_policy_version_id = policy.id

    readiness = team_inbox_status.resolution_readiness(
        db_session, conversation, evaluated_at=NOW
    )
    team_inbox_status.apply_status_transition(
        db_session,
        conversation=conversation,
        status=team_inbox_status.InboxConversationStatus.resolved,
        actor_person_id=uuid4(),
        reason=team_inbox_status.InboxStatusReason.operator_change,
        resolution_reason=(
            team_inbox_status.InboxResolutionReason.customer_stopped_responding
        ),
        occurred_at=NOW,
    )

    assert readiness.customer_readiness.can_agent_resolve is False
    assert readiness.can_agent_resolve is True
    assert conversation.status == "resolved"


def test_expired_resolution_requires_reason_but_active_readiness_is_unchanged(
    db_session,
):
    expired = _whatsapp(db_session, inbound_at=NOW - timedelta(hours=25))
    active = _whatsapp(db_session, inbound_at=NOW - timedelta(hours=1))

    with pytest.raises(
        team_inbox_status.InboxResolutionError,
        match="Choose a resolution reason",
    ):
        team_inbox_status.apply_status_transition(
            db_session,
            conversation=expired,
            status=team_inbox_status.InboxConversationStatus.resolved,
            actor_person_id=uuid4(),
            reason=team_inbox_status.InboxStatusReason.operator_change,
            occurred_at=NOW,
        )
    assert (
        team_inbox_status.resolution_readiness(
            db_session, active, evaluated_at=NOW
        ).can_agent_resolve
        is False
    )


@pytest.mark.parametrize("sender_type", ["agent", "ai", "system"])
def test_outbound_activity_never_extends_customer_window(db_session, sender_type):
    conversation = _whatsapp(db_session, inbound_at=NOW - timedelta(hours=25))
    db_session.add(
        InboxMessage(
            conversation_id=conversation.id,
            channel_type="whatsapp",
            direction=InboxMessageDirection.outbound.value,
            body="Outbound activity",
            sent_at=NOW,
            metadata_={
                "sender_type": sender_type,
                "whatsapp_template": (
                    {"name": "approved_template"} if sender_type == "system" else None
                ),
            },
        )
    )
    conversation.last_message_at = NOW
    db_session.flush()

    assert (
        team_inbox_reply_window.decide_reply_window(
            db_session, conversation=conversation, now=NOW
        ).status
        is team_inbox_reply_window.ReplyWindowStatus.expired
    )


def test_scoped_single_and_bulk_resolution_are_idempotent(db_session):
    team, agent_id = _team_and_agent(db_session)
    first = _whatsapp(db_session, inbound_at=NOW - timedelta(hours=25), team_id=team.id)
    second = _whatsapp(
        db_session, inbound_at=NOW - timedelta(hours=26), team_id=team.id
    )
    db_session.commit()

    with pytest.raises(team_inbox_commands.InboxCommandError, match="team scope"):
        team_inbox_commands.resolve_conversation(
            db_session,
            team_inbox_commands.ResolveConversationCommand(
                context=_context("unauthorized_resolve"),
                conversation_id=first.id,
                actor_person_id=agent_id,
                resolution_reason=team_inbox_status.InboxResolutionReason.other,
                permitted_team_ids=frozenset(),
            ),
        )

    bulk = team_inbox_commands.bulk_resolve_conversations(
        db_session,
        team_inbox_commands.BulkResolveConversationsCommand(
            context=_context("bulk_resolve"),
            conversation_ids=(first.id, second.id),
            actor_person_id=agent_id,
            resolution_reason=(
                team_inbox_status.InboxResolutionReason.customer_stopped_responding
            ),
            permitted_team_ids=frozenset({team.id}),
        ),
    )
    replay = team_inbox_commands.resolve_conversation(
        db_session,
        team_inbox_commands.ResolveConversationCommand(
            context=_context("resolve_replay"),
            conversation_id=first.id,
            actor_person_id=agent_id,
            resolution_reason=team_inbox_status.InboxResolutionReason.other,
            permitted_team_ids=frozenset({team.id}),
        ),
    )

    assert set(bulk.resolved) == {first.id, second.id}
    assert db_session.query(SupportCsatRequest).count() == 0
    assert replay.already_resolved is True
    assert (
        db_session.query(InboxStatusTransitionEvent)
        .filter(InboxStatusTransitionEvent.status == "resolved")
        .count()
        == 2
    )


def test_expired_resolved_disappears_from_unresolved_and_remains_in_history(
    db_session,
):
    conversation = _whatsapp(db_session, inbound_at=NOW - timedelta(hours=25))
    team_inbox_status.apply_status_transition(
        db_session,
        conversation=conversation,
        status=team_inbox_status.InboxConversationStatus.resolved,
        actor_person_id=uuid4(),
        reason=team_inbox_status.InboxStatusReason.operator_change,
        resolution_reason=team_inbox_status.InboxResolutionReason.other,
        occurred_at=NOW,
    )
    db_session.flush()

    unresolved = team_inbox_read.list_conversations(db_session, open_only=True)
    history = team_inbox_read.list_conversations(
        db_session,
        status="resolved",
        reply_window_status="expired",
        open_only=False,
    )
    assert str(conversation.id) not in {row.id for row in unresolved.items}
    assert str(conversation.id) in {row.id for row in history.items}


def test_customer_inbound_after_expiry_reopens_window_without_old_assignment(
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
    team_inbox_maintenance.sweep_expired_whatsapp_windows(
        db_session,
        team_inbox_maintenance.WhatsAppWindowExpirySweepCommand(
            context=_context("release_before_inbound"), now=NOW
        ),
    )

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


def test_customer_inbound_releases_stale_assignment_before_expiry_sweep(db_session):
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
            body="I returned before the expiry worker ran",
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
    assert old_assignment.ended_by_event_id is not None
    assert (
        db_session.query(InboxRoutingEvent)
        .filter(InboxRoutingEvent.conversation_id == conversation.id)
        .filter(InboxRoutingEvent.reason_code == "whatsapp_window_expired")
        .count()
        == 1
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
    db_session.flush()

    db_session.refresh(old_assignment)
    assert received.conversation_id != str(prior.id)
    assert old_assignment.is_active is False
    assert (
        db_session.query(InboxConversationAssignment)
        .filter(InboxConversationAssignment.conversation_id == prior.id)
        .filter(InboxConversationAssignment.is_active.is_(True))
        .count()
        == 0
    )


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
