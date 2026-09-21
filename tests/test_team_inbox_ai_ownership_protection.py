from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.models.ai_intake import AiIntakeSession
from app.models.notification import (
    Notification,
    NotificationChannel,
    NotificationStatus,
)
from app.models.service_team import ServiceTeam, ServiceTeamMember, ServiceTeamType
from app.models.team_inbox import (
    InboxAgentPresence,
    InboxConversation,
    InboxConversationAssignment,
    InboxConversationQueueEntry,
    InboxConversationTeam,
    InboxMessage,
    InboxMessageDirection,
    InboxQueueEntryStatus,
)
from app.services import (
    ai_conversation_ownership,
    conversation_ticket_handoff,
    team_inbox_assignment,
    team_inbox_commands,
    team_inbox_projection,
    team_inbox_read,
)
from app.services.owner_commands import CommandContext
from app.tasks import notifications as notification_tasks
from tests.staff_identity_fixtures import add_bound_staff_user

TAKEOVER_PERMISSIONS = frozenset({"support:ticket:update", "support:inbox:self_assign"})


def _owned_conversation(db_session, *, state: str = "collecting_intent"):
    team = ServiceTeam(
        name=f"AI ownership {uuid4()}", team_type=ServiceTeamType.support.value
    )
    user, person = add_bound_staff_user(db_session)
    db_session.add(team)
    db_session.flush()
    db_session.add_all(
        (
            ServiceTeamMember(team_id=team.id, person_id=person.id, role="member"),
            InboxAgentPresence(
                person_id=user.id,
                status="online",
                manual_override_status="online",
                last_seen_at=datetime.now(UTC),
                max_concurrent_conversations=10,
            ),
        )
    )
    conversation = InboxConversation(
        channel_type="whatsapp",
        subject="AI-owned conversation",
        contact_address="+2348000000000",
        primary_service_team_id=team.id,
        metadata_={"ai_handling": False},
    )
    db_session.add_all((team, conversation))
    db_session.flush()
    session = AiIntakeSession(
        conversation_id=conversation.id,
        state=state,
        channel_type="whatsapp",
        provider="meta",
        account_scope="test-account",
        metadata_={},
    )
    db_session.add(session)
    db_session.commit()
    return conversation, session, team, user


@pytest.mark.parametrize(
    "ordinary_assignment_block",
    ["not_member", "offline", "capacity", "existing_owner"],
)
def test_takeover_bypasses_ordinary_assignment_eligibility(
    db_session, ordinary_assignment_block: str
) -> None:
    conversation, session, team, user = _owned_conversation(db_session)
    if ordinary_assignment_block == "not_member":
        member = (
            db_session.query(ServiceTeamMember)
            .filter(ServiceTeamMember.team_id == team.id)
            .one()
        )
        db_session.delete(member)
    elif ordinary_assignment_block == "offline":
        presence = (
            db_session.query(InboxAgentPresence)
            .filter(InboxAgentPresence.person_id == user.id)
            .one()
        )
        presence.status = "offline"
        presence.manual_override_status = "offline"
    elif ordinary_assignment_block == "capacity":
        presence = (
            db_session.query(InboxAgentPresence)
            .filter(InboxAgentPresence.person_id == user.id)
            .one()
        )
        presence.max_concurrent_conversations = 1
        other_conversation = InboxConversation(
            channel_type="email",
            subject="Existing human assignment",
            primary_service_team_id=team.id,
        )
        db_session.add(other_conversation)
        db_session.flush()
        db_session.add(
            InboxConversationAssignment(
                conversation_id=other_conversation.id,
                service_team_id=team.id,
                person_id=user.id,
                is_active=True,
            )
        )
    else:
        existing_owner, _person = add_bound_staff_user(db_session)
        db_session.add(
            InboxConversationAssignment(
                conversation_id=conversation.id,
                service_team_id=team.id,
                person_id=existing_owner.id,
                is_active=True,
            )
        )
    db_session.commit()
    command = _takeover_command(conversation, session, team, user.id)
    session_id = session.id
    db_session.commit()

    outcome = team_inbox_commands.take_over_conversation(db_session, command)

    assert outcome.assigned_person_id == user.id
    assert outcome.service_team_id == team.id
    db_session.expire_all()
    assert db_session.get(AiIntakeSession, session_id).state == "stopped_human_takeover"
    assert (
        db_session.query(InboxConversationAssignment)
        .filter(InboxConversationAssignment.conversation_id == command.conversation_id)
        .filter(InboxConversationAssignment.is_active.is_(True))
        .count()
    ) == 1


def test_takeover_uses_the_only_active_conversation_team_when_primary_is_empty(
    db_session,
) -> None:
    conversation, session, team, user = _owned_conversation(db_session)
    conversation.primary_service_team_id = None
    db_session.add(
        InboxConversationTeam(
            conversation_id=conversation.id,
            service_team_id=team.id,
            role="participant",
            source="routing_rule",
            is_active=True,
        )
    )
    db_session.commit()
    command = replace(
        _takeover_command(conversation, session, team, user.id),
        service_team_id=None,
    )

    outcome = team_inbox_commands.take_over_conversation(db_session, command)

    assert outcome.service_team_id == team.id
    assert outcome.assigned_person_id == user.id


def test_takeover_bypasses_fifo_order(db_session) -> None:
    conversation, session, team, user = _owned_conversation(db_session)
    older_conversation = InboxConversation(
        channel_type="email",
        subject="Older queued conversation",
        primary_service_team_id=team.id,
    )
    db_session.add(older_conversation)
    db_session.flush()
    db_session.add_all(
        (
            InboxConversationQueueEntry(
                conversation_id=older_conversation.id,
                service_team_id=team.id,
                queue_position=1,
                status=InboxQueueEntryStatus.queued.value,
            ),
            InboxConversationQueueEntry(
                conversation_id=conversation.id,
                service_team_id=team.id,
                queue_position=2,
                status=InboxQueueEntryStatus.queued.value,
            ),
        )
    )
    db_session.commit()

    outcome = team_inbox_commands.take_over_conversation(
        db_session,
        _takeover_command(conversation, session, team, user.id),
    )

    assert outcome.assigned_person_id == user.id
    target_entry = (
        db_session.query(InboxConversationQueueEntry)
        .filter(InboxConversationQueueEntry.conversation_id == conversation.id)
        .one()
    )
    assert target_entry.status == InboxQueueEntryStatus.promoted.value


def test_takeover_projection_requires_team_selection_when_primary_is_empty(
    db_session,
) -> None:
    conversation, _session, _team, user = _owned_conversation(db_session)
    conversation.primary_service_team_id = None
    db_session.add(
        ServiceTeam(name=f"Second takeover team {uuid4()}", team_type="support")
    )
    db_session.commit()

    projection = team_inbox_projection.get_conversation_projection(
        db_session,
        conversation_id=conversation.id,
        actor_person_id=user.id,
        actor_permission_keys=TAKEOVER_PERMISSIONS,
        include_contact_candidates=False,
        include_catalogue_options=False,
    )

    assert projection is not None
    assert projection.action_eligibility.can_take_over is True
    assert projection.action_eligibility.takeover_team_id is None
    assert projection.action_eligibility.takeover_needs_team_select is True
    assert len(projection.action_eligibility.takeover_team_options) >= 2


def _takeover_command(
    conversation: InboxConversation,
    session: AiIntakeSession,
    team: ServiceTeam,
    actor_id: UUID,
    *,
    key: str = "takeover-test-key",
    expected_state: str | None = None,
) -> team_inbox_commands.TakeOverConversationCommand:
    return team_inbox_commands.TakeOverConversationCommand(
        context=CommandContext.system(
            actor=f"system-user:{actor_id}",
            scope="team-inbox:ai-takeover",
            reason="Focused ownership protection test",
            idempotency_key=key,
        ),
        conversation_id=conversation.id,
        expected_ai_session_id=session.id,
        expected_ai_session_state=expected_state or session.state,
        actor_person_id=actor_id,
        permission_keys=TAKEOVER_PERMISSIONS,
        service_team_id=team.id,
        reason="Focused ownership protection test",
    )


def test_active_session_is_authoritative_even_when_metadata_disagrees(db_session):
    conversation, session, _team, _user = _owned_conversation(
        db_session, state="awaiting_customer"
    )

    ownership = ai_conversation_ownership.resolve_ai_conversation_ownership(
        db_session, conversation_id=conversation.id
    )

    assert ownership.ai_owned is True
    assert ownership.ai_session_id == session.id
    assert ownership.ai_session_state == "awaiting_customer"
    assert ownership.waiting_for_customer is True
    assert ownership.provenance is (
        ai_conversation_ownership.AiOwnershipProvenance.active_ai_intake_session
    )


@pytest.mark.parametrize("state", ["collecting_intent", "awaiting_customer"])
def test_ai_owned_conversation_blocks_normal_human_mutations(db_session, state):
    conversation, _session, team, user = _owned_conversation(db_session, state=state)
    conversation_id = conversation.id
    team_id = team.id
    user_id = user.id

    mutations = (
        (
            "reply",
            lambda: team_inbox_commands.reply(
                db_session,
                command=team_inbox_commands.ReplyCommand(
                    conversation_id=conversation_id,
                    body_text="Human reply",
                    actor_person_id=user_id,
                ),
            ),
        ),
        (
            "self assignment",
            lambda: team_inbox_commands.assign_conversation_to_me(
                db_session,
                conversation_id=conversation_id,
                service_team_id=team_id,
                actor_person_id=user_id,
            ),
        ),
        (
            "named assignment",
            lambda: team_inbox_commands.assign_conversation(
                db_session,
                conversation_id=conversation_id,
                service_team_id=team_id,
                person_id=user_id,
                actor_person_id=user_id,
            ),
        ),
        (
            "status",
            lambda: team_inbox_commands.update_status(
                db_session,
                conversation_id=conversation_id,
                status_value="resolved",
                actor_person_id=user_id,
            ),
        ),
        (
            "private note",
            lambda: team_inbox_commands.create_internal_note(
                db_session,
                team_inbox_commands.CreateInternalNoteCommand(
                    context=CommandContext.system(
                        actor=f"system-user:{user_id}",
                        scope="team-inbox:private-note",
                        reason="test",
                    ),
                    conversation_id=conversation_id,
                    body="Internal note",
                    actor_person_id=user_id,
                    actor_system_user_id=user_id,
                ),
            ),
        ),
        (
            "macro",
            lambda: team_inbox_commands.run_macro(
                db_session,
                conversation_id=conversation_id,
                macro_id=uuid4(),
                actor_person_id=user_id,
            ),
        ),
        (
            "bulk",
            lambda: team_inbox_commands.bulk_action(
                db_session,
                conversation_ids=(str(conversation_id),),
                action="status",
                status_value="resolved",
                actor_person_id=user_id,
            ),
        ),
        (
            "ticket",
            lambda: conversation_ticket_handoff.issue_ticket(
                db_session,
                conversation_ticket_handoff.ConversationTicketIssueCommand(
                    conversation_id=conversation_id,
                    actor_id=user_id,
                    actor_type=conversation_ticket_handoff.HandoffActorType.SYSTEM_USER,
                    permission_keys=frozenset({"support:ticket:update"}),
                    title="Blocked ticket",
                ),
            ),
        ),
    )
    for name, mutation in mutations:
        try:
            mutation()
        except Exception as exc:
            if not isinstance(exc, ai_conversation_ownership.AiConversationOwnedError):
                raise
            captured = exc
        else:
            pytest.fail(f"{name} mutation was not blocked")
        finally:
            db_session.rollback()
        assert captured.code == "communications.team_inbox_commands.ai_owned", name


def test_only_typed_ai_handoff_provenance_can_route_ai_owned_conversation(db_session):
    conversation, _session, team, _user = _owned_conversation(db_session)

    with pytest.raises(ai_conversation_ownership.AiConversationOwnedError):
        team_inbox_assignment.queue_conversation_for_team(
            db_session,
            conversation=conversation,
            service_team_id=team.id,
        )
    db_session.rollback()

    result = team_inbox_assignment.queue_conversation_for_team(
        db_session,
        conversation=conversation,
        service_team_id=team.id,
        provenance=team_inbox_assignment.InboxAssignmentProvenance.ai_intake_handoff,
    )

    assert result.kind == "queued"
    assert (
        db_session.query(InboxConversationQueueEntry)
        .filter_by(
            conversation_id=conversation.id,
            status=InboxQueueEntryStatus.queued.value,
        )
        .one()
    )


def test_explicit_takeover_is_atomic_idempotent_and_cancels_ai_outbound(db_session):
    conversation, session, team, user = _owned_conversation(
        db_session, state="awaiting_customer"
    )
    notification = Notification(
        channel=NotificationChannel.whatsapp,
        recipient=conversation.contact_address,
        body="Queued AI answer",
        status=NotificationStatus.queued,
        metadata_={
            "conversation_id": str(conversation.id),
            "ai_intake_session_id": str(session.id),
            "sender_type": "ai",
        },
    )
    db_session.add(notification)
    db_session.flush()
    message = InboxMessage(
        conversation_id=conversation.id,
        notification_id=notification.id,
        channel_type="whatsapp",
        direction=InboxMessageDirection.outbound.value,
        body="Queued AI answer",
        metadata_={
            "ai_intake_session_id": str(session.id),
            "sender_type": "ai",
            "delivery_status": "queued",
        },
    )
    db_session.add(message)
    db_session.commit()
    command = _takeover_command(conversation, session, team, user.id)

    outcome = team_inbox_commands.take_over_conversation(db_session, command)

    assert outcome.replayed is False
    assert outcome.canceled_ai_outbound_count == 1
    db_session.expire_all()
    assert db_session.get(AiIntakeSession, session.id).state == "stopped_human_takeover"
    assert db_session.get(AiIntakeSession, session.id).completed_at is not None
    assert db_session.get(AiIntakeSession, session.id).customer_wait_started_at is None
    assert (
        db_session.get(Notification, notification.id).status
        == NotificationStatus.canceled
    )
    assert (
        db_session.query(InboxConversationAssignment)
        .filter_by(conversation_id=conversation.id, is_active=True)
        .one()
        .person_id
        == user.id
    )

    replay = team_inbox_commands.take_over_conversation(db_session, command)
    assert replay.replayed is True
    assert replay.ai_session_id == session.id


def test_takeover_assignment_failure_rolls_back_ai_stop(db_session, monkeypatch):
    conversation, session, team, user = _owned_conversation(db_session)

    monkeypatch.setattr(
        team_inbox_assignment,
        "assign_conversation_to_agent",
        lambda *args, **kwargs: team_inbox_assignment.InboxAssignmentResult(
            kind="invalid_agent", service_team_id=str(team.id), reason="test failure"
        ),
    )
    with pytest.raises(ai_conversation_ownership.AiTakeoverConflictError):
        team_inbox_commands.take_over_conversation(
            db_session, _takeover_command(conversation, session, team, user.id)
        )

    db_session.expire_all()
    persisted = db_session.get(AiIntakeSession, session.id)
    assert persisted.completed_at is None
    assert persisted.state == "collecting_intent"


def test_stale_takeover_state_returns_conflict(db_session):
    conversation, session, team, user = _owned_conversation(db_session)

    with pytest.raises(ai_conversation_ownership.AiTakeoverConflictError):
        team_inbox_commands.take_over_conversation(
            db_session,
            _takeover_command(
                conversation,
                session,
                team,
                user.id,
                expected_state="awaiting_customer",
            ),
        )


def test_actionable_views_counts_and_controls_exclude_ai_owned(db_session):
    ai_conversation, session, team, user = _owned_conversation(db_session)
    human_conversation = InboxConversation(channel_type="email", subject="Human work")
    db_session.add(human_conversation)
    db_session.flush()
    db_session.add(
        InboxConversationQueueEntry(
            conversation_id=human_conversation.id,
            service_team_id=team.id,
            queue_position=1,
            status=InboxQueueEntryStatus.queued.value,
        )
    )
    db_session.commit()

    actionable = team_inbox_read.list_conversations(
        db_session,
        ownership_cohort=(
            ai_conversation_ownership.ConversationOwnershipCohort.actionable
        ),
    )
    ai_intake = team_inbox_read.list_conversations(
        db_session,
        ownership_cohort=ai_conversation_ownership.ConversationOwnershipCohort.ai_intake,
    )
    queued = team_inbox_read.list_conversations(
        db_session,
        ownership_cohort=ai_conversation_ownership.ConversationOwnershipCohort.queue,
    )
    projection = team_inbox_projection.get_conversation_projection(
        db_session,
        conversation_id=ai_conversation.id,
        actor_person_id=user.id,
        actor_permission_keys=TAKEOVER_PERMISSIONS,
        include_contact_candidates=False,
        include_catalogue_options=False,
    )

    assert {row.id for row in actionable.items} == {str(human_conversation.id)}
    assert {row.id for row in ai_intake.items} == {str(ai_conversation.id)}
    assert {row.id for row in queued.items} == {str(human_conversation.id)}
    assert team_inbox_read.queue_conversation_count(db_session) == 1
    assert team_inbox_read.queued_conversation_count(db_session) == 1
    assert team_inbox_read.ai_handling_conversation_count(db_session) == 1
    assert projection is not None
    eligibility = projection.action_eligibility
    assert eligibility.control_owner == "ai"
    assert eligibility.ai_session_id == session.id
    assert eligibility.can_take_over is True
    assert eligibility.takeover_team_id == team.id
    assert eligibility.takeover_team_options[0].id == team.id
    assert eligibility.takeover_needs_team_select is False
    assert eligibility.can_reply is False
    assert eligibility.can_private_note is False
    assert eligibility.can_assign is False
    assert eligibility.can_change_status is False
    assert eligibility.can_create_ticket is False
    assert eligibility.can_run_macro is False


def test_delivery_worker_suppresses_ai_message_after_takeover(db_session, monkeypatch):
    conversation, session, _team, _user = _owned_conversation(db_session)
    session.state = "stopped_human_takeover"
    session.completed_at = datetime.now(UTC)
    session.takeover_at = session.completed_at
    notification = Notification(
        channel=NotificationChannel.whatsapp,
        recipient=conversation.contact_address,
        body="Stale queued AI answer",
        status=NotificationStatus.queued,
        metadata_={
            "conversation_id": str(conversation.id),
            "ai_intake_session_id": str(session.id),
            "sender_type": "ai",
        },
    )
    db_session.add(notification)
    db_session.flush()
    message = InboxMessage(
        conversation_id=conversation.id,
        notification_id=notification.id,
        channel_type="whatsapp",
        direction=InboxMessageDirection.outbound.value,
        body="Stale queued AI answer",
        metadata_={
            "ai_intake_session_id": str(session.id),
            "sender_type": "ai",
            "delivery_status": "queued",
        },
    )
    db_session.add(message)
    db_session.commit()
    provider_called = False

    def fail_if_provider_called(*args, **kwargs):
        nonlocal provider_called
        provider_called = True
        raise AssertionError("provider must not be contacted")

    monkeypatch.setattr(
        notification_tasks.whatsapp_service,
        "send_whatsapp_message",
        fail_if_provider_called,
        raising=False,
    )

    stats = notification_tasks._deliver_notification_queue_stats(
        db_session, notification_id=notification.id
    )

    assert stats["suppressed"] == 1
    assert provider_called is False
    db_session.expire_all()
    assert (
        db_session.get(Notification, notification.id).status
        == NotificationStatus.canceled
    )
    assert (
        db_session.get(InboxMessage, message.id).metadata_["delivery_status"]
        == "canceled"
    )
