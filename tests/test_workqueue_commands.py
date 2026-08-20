from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.models.idempotency import IdempotencyKey
from app.models.service_team import ServiceTeam, ServiceTeamMember
from app.models.support import Ticket, TicketStatus
from app.models.team_inbox import (
    InboxAgentPresence,
    InboxAgentPresenceStatus,
    InboxConversation,
    InboxConversationAssignment,
    InboxConversationStatus,
)
from app.services.owner_commands import CommandContext
from app.services.workqueue import WorkqueuePrincipal
from app.services.workqueue.aggregator import build_workqueue
from app.services.workqueue.commands import (
    SnoozeMode,
    WorkqueueActionCommand,
    WorkqueueActionError,
    action_state_fingerprint,
    execute_action,
)
from app.services.workqueue.types import ActionKind, ItemKind
from tests.staff_identity_fixtures import add_bound_staff_user


def _principal(system_user_id: UUID) -> WorkqueuePrincipal:
    return WorkqueuePrincipal(
        person_id=system_user_id,
        roles=frozenset(),
        scopes=frozenset(),
        can_view=True,
        can_act=True,
    )


def _context(
    system_user_id: UUID,
    *,
    request_id: UUID | None = None,
) -> CommandContext:
    command_id = request_id or uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor=f"user:{system_user_id}",
        scope="support:ticket:update",
        reason="Test native workqueue action",
        idempotency_key=str(command_id),
    )


def _team_member(db, system_user_id: UUID, *, name: str = "Support") -> ServiceTeam:
    team = ServiceTeam(name=name)
    db.add(team)
    db.flush()
    _user, person = add_bound_staff_user(db, system_user_id=system_user_id)
    db.add(ServiceTeamMember(team_id=team.id, person_id=person.id))
    db.add(
        InboxAgentPresence(
            person_id=system_user_id,
            status=InboxAgentPresenceStatus.online.value,
            last_seen_at=datetime.now(UTC),
        )
    )
    db.flush()
    return team


def _fingerprint(
    db,
    principal: WorkqueuePrincipal,
    *,
    item_kind: ItemKind,
    item_id: UUID,
    action: ActionKind,
) -> str:
    view = build_workqueue(db, principal, include_snoozed=True)
    item = next(
        item
        for section in view.sections
        for item in section.items
        if item.item_kind is item_kind and item.item_id == item_id
    )
    return action_state_fingerprint(item, action)


def test_ticket_claim_delegates_to_ticket_owner_and_replays(db_session):
    actor_id = uuid4()
    team = _team_member(db_session, actor_id)
    ticket = Ticket(
        title="Link down",
        status=TicketStatus.open.value,
        priority="normal",
        service_team_id=team.id,
    )
    db_session.add(ticket)
    db_session.commit()
    request_id = uuid4()
    principal = _principal(actor_id)
    command = WorkqueueActionCommand(
        context=_context(actor_id, request_id=request_id),
        principal=principal,
        item_kind=ItemKind.ticket,
        item_id=ticket.id,
        action=ActionKind.claim,
        state_fingerprint=_fingerprint(
            db_session,
            principal,
            item_kind=ItemKind.ticket,
            item_id=ticket.id,
            action=ActionKind.claim,
        ),
    )

    outcome = execute_action(db_session, command)
    replay = execute_action(db_session, command)

    assert outcome.result == "claimed"
    assert outcome.replayed is False
    assert replay.replayed is True
    assert db_session.get(Ticket, ticket.id).assigned_to_person_id == actor_id
    assert (
        db_session.query(IdempotencyKey)
        .filter(IdempotencyKey.key == str(request_id))
        .count()
        == 1
    )


def test_ticket_complete_is_atomic_and_replays_after_item_leaves_queue(db_session):
    actor_id = uuid4()
    team = _team_member(db_session, actor_id)
    ticket = Ticket(
        title="Intermittent service",
        status=TicketStatus.open.value,
        priority="normal",
        service_team_id=team.id,
        assigned_to_person_id=actor_id,
    )
    db_session.add(ticket)
    db_session.commit()
    principal = _principal(actor_id)
    command = WorkqueueActionCommand(
        context=_context(actor_id),
        principal=principal,
        item_kind=ItemKind.ticket,
        item_id=ticket.id,
        action=ActionKind.complete,
        state_fingerprint=_fingerprint(
            db_session,
            principal,
            item_kind=ItemKind.ticket,
            item_id=ticket.id,
            action=ActionKind.complete,
        ),
        confirmed=True,
    )

    first = execute_action(db_session, command)
    second = execute_action(db_session, command)

    assert first.result == "completed"
    assert second.replayed is True
    assert db_session.get(Ticket, ticket.id).status == TicketStatus.closed.value


def test_conversation_claim_participates_in_workqueue_root_transaction(db_session):
    actor_id = uuid4()
    team = _team_member(db_session, actor_id)
    conversation = InboxConversation(
        subject="Where is my invoice?",
        status=InboxConversationStatus.open.value,
        primary_service_team_id=team.id,
    )
    db_session.add(conversation)
    db_session.commit()
    principal = _principal(actor_id)

    outcome = execute_action(
        db_session,
        WorkqueueActionCommand(
            context=_context(actor_id),
            principal=principal,
            item_kind=ItemKind.conversation,
            item_id=conversation.id,
            action=ActionKind.claim,
            state_fingerprint=_fingerprint(
                db_session,
                principal,
                item_kind=ItemKind.conversation,
                item_id=conversation.id,
                action=ActionKind.claim,
            ),
        ),
    )

    assignment = (
        db_session.query(InboxConversationAssignment)
        .filter(
            InboxConversationAssignment.conversation_id == conversation.id,
            InboxConversationAssignment.is_active.is_(True),
        )
        .one()
    )
    assert outcome.result == "claimed"
    assert assignment.person_id == actor_id
    assert assignment.service_team_id == team.id


def test_lifecycle_action_rechecks_review_and_confirmation_under_lock(db_session):
    actor_id = uuid4()
    team = _team_member(db_session, actor_id)
    ticket = Ticket(
        title="Review before resolve",
        status=TicketStatus.open.value,
        priority="normal",
        service_team_id=team.id,
        assigned_to_person_id=actor_id,
    )
    db_session.add(ticket)
    db_session.commit()
    principal = _principal(actor_id)
    fingerprint = _fingerprint(
        db_session,
        principal,
        item_kind=ItemKind.ticket,
        item_id=ticket.id,
        action=ActionKind.complete,
    )

    with pytest.raises(WorkqueueActionError) as missing_confirmation:
        execute_action(
            db_session,
            WorkqueueActionCommand(
                context=_context(actor_id),
                principal=principal,
                item_kind=ItemKind.ticket,
                item_id=ticket.id,
                action=ActionKind.complete,
                state_fingerprint=fingerprint,
            ),
        )

    assert (
        missing_confirmation.value.code
        == "operations.agent_workqueue.confirmation_required"
    )
    ticket.status = TicketStatus.pending.value
    db_session.commit()
    with pytest.raises(WorkqueueActionError) as stale_review:
        execute_action(
            db_session,
            WorkqueueActionCommand(
                context=_context(actor_id),
                principal=principal,
                item_kind=ItemKind.ticket,
                item_id=ticket.id,
                action=ActionKind.complete,
                state_fingerprint=fingerprint,
                confirmed=True,
            ),
        )

    assert stale_review.value.code == "operations.agent_workqueue.stale_action_review"


def test_action_fails_closed_outside_native_service_team_scope(db_session):
    actor_id = uuid4()
    _team_member(db_session, actor_id, name="My team")
    other_id = uuid4()
    other_team = _team_member(db_session, other_id, name="Other team")
    ticket = Ticket(
        title="Hidden work",
        status=TicketStatus.open.value,
        priority="normal",
        service_team_id=other_team.id,
    )
    db_session.add(ticket)
    db_session.commit()
    command = WorkqueueActionCommand(
        context=_context(actor_id),
        principal=_principal(actor_id),
        item_kind=ItemKind.ticket,
        item_id=ticket.id,
        action=ActionKind.claim,
    )

    with pytest.raises(WorkqueueActionError) as error:
        execute_action(db_session, command)

    assert error.value.code == "operations.agent_workqueue.item_out_of_scope"
    assert (
        db_session.query(IdempotencyKey)
        .filter(IdempotencyKey.key == command.context.idempotency_key)
        .count()
        == 0
    )


def test_personal_snooze_state_is_written_by_workqueue_owner(db_session):
    actor_id = uuid4()
    team = _team_member(db_session, actor_id)
    ticket = Ticket(
        title="Planned callback",
        status=TicketStatus.open.value,
        priority="normal",
        service_team_id=team.id,
    )
    db_session.add(ticket)
    db_session.commit()

    outcome = execute_action(
        db_session,
        WorkqueueActionCommand(
            context=_context(actor_id),
            principal=_principal(actor_id),
            item_kind=ItemKind.ticket,
            item_id=ticket.id,
            action=ActionKind.snooze,
            snooze_mode=SnoozeMode.minutes_30,
        ),
    )

    assert outcome.snooze is not None
    assert outcome.snooze.system_user_id == actor_id
    assert outcome.snooze.snooze_until is not None
