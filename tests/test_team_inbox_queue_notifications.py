from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.models.notification import (
    Notification,
    NotificationChannel,
    NotificationStatus,
)
from app.models.service_team import ServiceTeam
from app.models.team_inbox import (
    InboxConversation,
    InboxConversationAssignment,
    InboxConversationQueueEntry,
    InboxMessage,
    InboxMessageDirection,
    InboxQueueEntryStatus,
    InboxQueueNotification,
)
from app.services import team_inbox_queue_notifications
from app.services.owner_commands import CommandContext
from app.services.team_inbox_assignment import queue_conversation_for_team
from app.services.team_inbox_outbound import InboxReplyResult


def _team(db_session) -> ServiceTeam:
    team = ServiceTeam(name=f"Queue Notice {uuid4()}", team_type="support")
    db_session.add(team)
    db_session.flush()
    return team


def _conversation(db_session) -> InboxConversation:
    conversation = InboxConversation(
        channel_type="whatsapp",
        status="open",
        contact_address="2348012345678",
        external_thread_id=f"queue-{uuid4()}",
    )
    db_session.add(conversation)
    db_session.flush()
    return conversation


def test_initial_queue_notice_is_recorded_once_per_queue_lifecycle(db_session):
    team = _team(db_session)
    conversation = _conversation(db_session)

    first = queue_conversation_for_team(
        db_session, conversation=conversation, service_team_id=team.id
    )
    repeated = queue_conversation_for_team(
        db_session, conversation=conversation, service_team_id=team.id
    )

    assert repeated.queue_entry_id == first.queue_entry_id
    notices = db_session.query(InboxQueueNotification).all()
    assert len(notices) == 1, [(notice.dedupe_key, notice.status) for notice in notices]
    notice = notices[0]
    assert notice.notification_kind == "initial"
    assert notice.queue_position == 1
    assert notice.status in {"sent", "failed"}


def _queue_delivery_succeeds(monkeypatch) -> None:
    monkeypatch.setattr(
        team_inbox_queue_notifications.team_inbox_outbound,
        "send_ai_intake_message",
        lambda _db, *, conversation, **_kwargs: InboxReplyResult(
            kind="queued",
            conversation_id=str(conversation.id),
            message_id=str(uuid4()),
        ),
    )


def test_queue_notification_sweep_uses_next_due_and_disables_heartbeat_by_default(
    db_session, monkeypatch
):
    _queue_delivery_succeeds(monkeypatch)
    team = _team(db_session)
    conversation = _conversation(db_session)
    now = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)
    queue_conversation_for_team(
        db_session,
        conversation=conversation,
        service_team_id=team.id,
        now=now,
    )
    db_session.commit()

    early = team_inbox_queue_notifications.sweep_queue_notifications(
        db_session,
        team_inbox_queue_notifications.QueueNotificationSweepCommand(
            context=CommandContext.system(
                actor="test", scope="team-inbox:routing-command", reason="test"
            ),
            now=now + timedelta(minutes=4),
        ),
    )
    assert early.sent == 0

    ten_minutes = team_inbox_queue_notifications.sweep_queue_notifications(
        db_session,
        team_inbox_queue_notifications.QueueNotificationSweepCommand(
            context=CommandContext.system(
                actor="test", scope="team-inbox:routing-command", reason="test"
            ),
            now=now + timedelta(minutes=10),
        ),
    )
    assert ten_minutes.sent == 0
    assert db_session.query(InboxQueueNotification).count() == 1
    db_session.commit()

    disabled = team_inbox_queue_notifications.sweep_queue_notifications(
        db_session,
        team_inbox_queue_notifications.QueueNotificationSweepCommand(
            context=CommandContext.system(
                actor="test", scope="team-inbox:routing-command", reason="test"
            ),
            now=now + timedelta(minutes=31),
        ),
    )
    assert disabled.sent == 0
    assert disabled.failed == 0
    assert (
        db_session.query(InboxQueueNotification)
        .filter(InboxQueueNotification.notification_kind == "heartbeat")
        .count()
        == 0
    )
    db_session.commit()

    duplicate = team_inbox_queue_notifications.sweep_queue_notifications(
        db_session,
        team_inbox_queue_notifications.QueueNotificationSweepCommand(
            context=CommandContext.system(
                actor="test", scope="team-inbox:routing-command", reason="test"
            ),
            now=now + timedelta(minutes=31),
        ),
    )
    assert duplicate.sent == 0


def test_opt_in_heartbeat_uses_separate_non_position_template(db_session, monkeypatch):
    bodies: list[str] = []

    def _record_send(_db, *, conversation, body_text, **_kwargs):
        bodies.append(str(body_text))
        return InboxReplyResult(
            kind="queued",
            conversation_id=str(conversation.id),
            message_id=str(uuid4()),
        )

    original_policy = team_inbox_queue_notifications._queue_policy

    def _enabled_policy(db, conversation):
        policy = original_policy(db, conversation)
        policy.update(
            heartbeat_enabled=True,
            heartbeat_minutes=30,
            heartbeat="We are still working to connect you with the team.",
        )
        return policy

    monkeypatch.setattr(
        team_inbox_queue_notifications.team_inbox_outbound,
        "send_ai_intake_message",
        _record_send,
    )
    monkeypatch.setattr(
        team_inbox_queue_notifications, "_queue_policy", _enabled_policy
    )
    team = _team(db_session)
    conversation = _conversation(db_session)
    now = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)
    queue_conversation_for_team(
        db_session,
        conversation=conversation,
        service_team_id=team.id,
        now=now,
    )
    db_session.commit()

    recent = team_inbox_queue_notifications.sweep_queue_notifications(
        db_session,
        team_inbox_queue_notifications.QueueNotificationSweepCommand(
            context=CommandContext.system(
                actor="test", scope="team-inbox:routing-command", reason="test"
            ),
            now=now + timedelta(minutes=10),
        ),
    )
    assert recent.sent == 0
    db_session.commit()

    result = team_inbox_queue_notifications.sweep_queue_notifications(
        db_session,
        team_inbox_queue_notifications.QueueNotificationSweepCommand(
            context=CommandContext.system(
                actor="test", scope="team-inbox:routing-command", reason="test"
            ),
            now=now + timedelta(minutes=31),
        ),
    )

    assert result.sent == 1
    assert len(bodies) == 2
    assert bodies[-1] == "We are still working to connect you with the team."
    assert "number" not in bodies[-1].lower()


def test_queue_notification_sweep_cancels_due_notice_after_human_reply(
    db_session, monkeypatch
):
    sends: list[str] = []

    def _record_send(_db, *, conversation, body_text, **_kwargs):
        sends.append(str(body_text))
        return InboxReplyResult(
            kind="queued",
            conversation_id=str(conversation.id),
            message_id=str(uuid4()),
        )

    monkeypatch.setattr(
        team_inbox_queue_notifications.team_inbox_outbound,
        "send_ai_intake_message",
        _record_send,
    )
    team = _team(db_session)
    conversation = _conversation(db_session)
    now = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)
    queue_conversation_for_team(
        db_session,
        conversation=conversation,
        service_team_id=team.id,
        now=now,
    )
    notice = db_session.query(InboxQueueNotification).one()
    assert notice.status == "sent"
    assert len(sends) == 1

    db_session.add(
        InboxMessage(
            conversation_id=conversation.id,
            channel_type=conversation.channel_type,
            direction=InboxMessageDirection.outbound.value,
            body="An agent is checking this now.",
            sent_at=now + timedelta(minutes=1),
            metadata_={"sent_by_person_id": str(uuid4())},
        )
    )
    db_session.commit()

    result = team_inbox_queue_notifications.sweep_queue_notifications(
        db_session,
        team_inbox_queue_notifications.QueueNotificationSweepCommand(
            context=CommandContext.system(
                actor="test", scope="team-inbox:routing-command", reason="test"
            ),
            now=now + timedelta(minutes=31),
        ),
    )

    assert result.skipped == 1
    assert len(sends) == 1
    assert db_session.get(InboxQueueNotification, notice.id).status == "cancelled"
    assert (
        db_session.query(InboxQueueNotification)
        .filter(InboxQueueNotification.notification_kind == "heartbeat")
        .count()
        == 0
    )


def test_queue_notification_sends_changed_position_update_once(db_session, monkeypatch):
    _queue_delivery_succeeds(monkeypatch)
    team = _team(db_session)
    first_conversation = _conversation(db_session)
    second_conversation = _conversation(db_session)
    now = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)
    queue_conversation_for_team(
        db_session,
        conversation=first_conversation,
        service_team_id=team.id,
        now=now,
    )
    queue_conversation_for_team(
        db_session,
        conversation=second_conversation,
        service_team_id=team.id,
        now=now + timedelta(seconds=1),
    )
    second_entry = (
        db_session.query(InboxConversationQueueEntry)
        .filter(InboxConversationQueueEntry.conversation_id == second_conversation.id)
        .one()
    )
    first_entry = (
        db_session.query(InboxConversationQueueEntry)
        .filter(InboxConversationQueueEntry.conversation_id == first_conversation.id)
        .one()
    )
    first_entry.status = InboxQueueEntryStatus.promoted.value
    first_entry.settled_at = now + timedelta(minutes=1)
    db_session.commit()

    changed = team_inbox_queue_notifications.sweep_queue_notifications(
        db_session,
        team_inbox_queue_notifications.QueueNotificationSweepCommand(
            context=CommandContext.system(
                actor="test", scope="team-inbox:routing-command", reason="test"
            ),
            now=now + timedelta(minutes=11),
        ),
    )
    assert changed.sent + changed.failed == 1
    update = (
        db_session.query(InboxQueueNotification)
        .filter(InboxQueueNotification.queue_entry_id == second_entry.id)
        .filter(InboxQueueNotification.notification_kind == "position_update")
        .one()
    )
    assert update.queue_position == 1
    db_session.commit()

    duplicate = team_inbox_queue_notifications.sweep_queue_notifications(
        db_session,
        team_inbox_queue_notifications.QueueNotificationSweepCommand(
            context=CommandContext.system(
                actor="test", scope="team-inbox:routing-command", reason="test"
            ),
            now=now + timedelta(minutes=11),
        ),
    )
    assert duplicate.sent == 0


def test_failed_queue_notice_retries_same_logical_notification(
    db_session,
    monkeypatch,
):
    team = _team(db_session)
    conversation = _conversation(db_session)
    now = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)
    calls = 0

    def _fail_once_then_queue(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return team_inbox_queue_notifications.team_inbox_outbound.InboxReplyResult(
                kind="failed",
                conversation_id=str(kwargs["conversation"].id),
                message_id=None,
                reason="provider_unavailable",
            )
        return team_inbox_queue_notifications.team_inbox_outbound.InboxReplyResult(
            kind="queued",
            conversation_id=str(kwargs["conversation"].id),
            message_id=str(uuid4()),
            reason="queued",
        )

    monkeypatch.setattr(
        team_inbox_queue_notifications.team_inbox_outbound,
        "send_ai_intake_message",
        _fail_once_then_queue,
    )
    queue_conversation_for_team(
        db_session,
        conversation=conversation,
        service_team_id=team.id,
        now=now,
    )
    notice = db_session.query(InboxQueueNotification).one()
    assert notice.status == "failed"
    dedupe_key = notice.dedupe_key
    next_due_at = notice.next_due_at
    db_session.commit()

    retried = team_inbox_queue_notifications.sweep_queue_notifications(
        db_session,
        team_inbox_queue_notifications.QueueNotificationSweepCommand(
            context=CommandContext.system(
                actor="test", scope="team-inbox:routing-command", reason="test"
            ),
            now=next_due_at,
        ),
    )
    assert retried.sent == 1
    assert db_session.query(InboxQueueNotification).count() == 1
    assert db_session.query(InboxQueueNotification).one().dedupe_key == dedupe_key


def test_handoff_notice_is_sent_once_per_queue_lifecycle(db_session):
    team = _team(db_session)
    conversation = _conversation(db_session)
    now = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)
    queue_conversation_for_team(
        db_session,
        conversation=conversation,
        service_team_id=team.id,
        now=now,
    )
    entry = db_session.query(InboxConversationQueueEntry).one()

    team_inbox_queue_notifications.send_handoff_notice(
        db_session,
        conversation=conversation,
        entry=entry,
        now=now + timedelta(minutes=1),
    )
    team_inbox_queue_notifications.send_handoff_notice(
        db_session,
        conversation=conversation,
        entry=entry,
        now=now + timedelta(minutes=2),
    )

    assert (
        db_session.query(InboxQueueNotification)
        .filter(InboxQueueNotification.notification_kind == "handoff")
        .count()
        == 1
    )


def test_terminal_queue_state_cancels_due_notification(db_session):
    team = _team(db_session)
    conversation = _conversation(db_session)
    now = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)
    queue_conversation_for_team(
        db_session,
        conversation=conversation,
        service_team_id=team.id,
        now=now,
    )
    entry = db_session.query(InboxConversationQueueEntry).one()
    entry.status = InboxQueueEntryStatus.cancelled.value
    db_session.commit()

    result = team_inbox_queue_notifications.sweep_queue_notifications(
        db_session,
        team_inbox_queue_notifications.QueueNotificationSweepCommand(
            context=CommandContext.system(
                actor="test", scope="team-inbox:routing-command", reason="test"
            ),
            now=now + timedelta(minutes=10),
        ),
    )

    assert result.skipped == 1
    assert db_session.query(InboxQueueNotification).one().status == "cancelled"


def test_worsening_position_is_logged_but_not_sent(db_session, monkeypatch):
    _queue_delivery_succeeds(monkeypatch)
    team = _team(db_session)
    waiting = _conversation(db_session)
    now = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)
    queue_conversation_for_team(
        db_session, conversation=waiting, service_team_id=team.id, now=now
    )
    inserted_ahead = _conversation(db_session)
    queue_conversation_for_team(
        db_session,
        conversation=inserted_ahead,
        service_team_id=team.id,
        now=now - timedelta(seconds=1),
    )
    db_session.commit()

    result = team_inbox_queue_notifications.sweep_queue_notifications(
        db_session,
        team_inbox_queue_notifications.QueueNotificationSweepCommand(
            context=CommandContext.system(
                actor="test", scope="team-inbox:routing-command", reason="test"
            ),
            now=now + timedelta(minutes=11),
        ),
    )

    assert result.sent == 0
    assert (
        db_session.query(InboxQueueNotification)
        .filter(InboxQueueNotification.conversation_id == waiting.id)
        .count()
        == 1
    )


def test_dispatch_preflight_suppresses_notice_after_assignment(db_session, monkeypatch):
    _queue_delivery_succeeds(monkeypatch)
    team = _team(db_session)
    conversation = _conversation(db_session)
    queue_conversation_for_team(
        db_session, conversation=conversation, service_team_id=team.id
    )
    entry = db_session.query(InboxConversationQueueEntry).one()
    ledger = db_session.query(InboxQueueNotification).one()
    db_session.add(
        InboxConversationAssignment(
            conversation_id=conversation.id,
            service_team_id=team.id,
            person_id=uuid4(),
            is_active=True,
        )
    )
    delivery = Notification(
        channel=NotificationChannel.whatsapp,
        recipient=conversation.contact_address,
        status=NotificationStatus.sending,
        metadata_={
            "automation_kind": "queue_notification",
            "conversation_id": str(conversation.id),
            "queue_entry_id": str(entry.id),
            "admission_generation": entry.admission_generation,
            "queue_notification_kind": "initial",
            "queue_notification_dedupe_key": ledger.dedupe_key,
            "current_visible_position": 1,
        },
    )
    db_session.add(delivery)
    db_session.flush()

    outcome = team_inbox_queue_notifications.preflight_queue_notification_delivery(
        db_session, notification=delivery
    )

    assert outcome.applies is True
    assert outcome.allowed is False
    assert outcome.reason == "human_assignment_active"


def test_assignment_cancellation_stops_pending_outbound_intent(db_session, monkeypatch):
    _queue_delivery_succeeds(monkeypatch)
    team = _team(db_session)
    conversation = _conversation(db_session)
    queue_conversation_for_team(
        db_session, conversation=conversation, service_team_id=team.id
    )
    entry = db_session.query(InboxConversationQueueEntry).one()
    ledger = db_session.query(InboxQueueNotification).one()
    delivery = Notification(
        channel=NotificationChannel.whatsapp,
        recipient=conversation.contact_address,
        status=NotificationStatus.queued,
        metadata_={"automation_kind": "queue_notification"},
    )
    db_session.add(delivery)
    db_session.flush()
    message = InboxMessage(
        id=ledger.outbound_message_id,
        conversation_id=conversation.id,
        channel_type=conversation.channel_type,
        direction=InboxMessageDirection.outbound.value,
        body="Queued notice",
        notification_id=delivery.id,
        metadata_={"delivery_status": "queued"},
    )
    db_session.add(message)
    db_session.flush()

    cancelled = team_inbox_queue_notifications.cancel_queue_lifecycle_notifications(
        db_session,
        entry=entry,
        reason="human_assignment_created",
    )

    assert cancelled == 1
    assert delivery.status is NotificationStatus.canceled
    assert ledger.status == "cancelled"
    assert ledger.suppression_reason == "human_assignment_created"
    assert message.metadata_["delivery_status"] == "cancelled"


def test_dispatch_preflight_replaces_stale_forward_position(db_session, monkeypatch):
    _queue_delivery_succeeds(monkeypatch)
    team = _team(db_session)
    first = _conversation(db_session)
    second = _conversation(db_session)
    now = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)
    queue_conversation_for_team(
        db_session, conversation=first, service_team_id=team.id, now=now
    )
    queue_conversation_for_team(
        db_session,
        conversation=second,
        service_team_id=team.id,
        now=now + timedelta(seconds=1),
    )
    second_entry = (
        db_session.query(InboxConversationQueueEntry)
        .filter(InboxConversationQueueEntry.conversation_id == second.id)
        .one()
    )
    second_initial = (
        db_session.query(InboxQueueNotification)
        .filter(InboxQueueNotification.queue_entry_id == second_entry.id)
        .one()
    )
    first_entry = (
        db_session.query(InboxConversationQueueEntry)
        .filter(InboxConversationQueueEntry.conversation_id == first.id)
        .one()
    )
    first_entry.status = InboxQueueEntryStatus.promoted.value
    first_entry.settled_at = now + timedelta(minutes=1)
    delivery = Notification(
        channel=NotificationChannel.whatsapp,
        recipient=second.contact_address,
        status=NotificationStatus.sending,
        metadata_={
            "automation_kind": "queue_notification",
            "conversation_id": str(second.id),
            "queue_entry_id": str(second_entry.id),
            "admission_generation": second_entry.admission_generation,
            "queue_notification_kind": "initial",
            "queue_notification_dedupe_key": second_initial.dedupe_key,
            "current_visible_position": 2,
        },
    )
    db_session.add(delivery)
    db_session.flush()

    outcome = team_inbox_queue_notifications.preflight_queue_notification_delivery(
        db_session, notification=delivery
    )

    assert outcome.allowed is False
    assert outcome.reason == "visible_position_stale"
    replacement = (
        db_session.query(InboxQueueNotification)
        .filter(InboxQueueNotification.queue_entry_id == second_entry.id)
        .filter(InboxQueueNotification.notification_kind == "position_update")
        .one()
    )
    assert replacement.queue_position == 1
    assert ":generation:1:1" in replacement.dedupe_key


def test_requeue_notification_keys_include_new_generation(db_session, monkeypatch):
    _queue_delivery_succeeds(monkeypatch)
    team = _team(db_session)
    conversation = _conversation(db_session)
    now = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)
    queue_conversation_for_team(
        db_session, conversation=conversation, service_team_id=team.id, now=now
    )
    entry = db_session.query(InboxConversationQueueEntry).one()
    first_key = db_session.query(InboxQueueNotification).one().dedupe_key
    entry.status = InboxQueueEntryStatus.cancelled.value
    entry.settled_at = now + timedelta(minutes=1)
    db_session.flush()

    queue_conversation_for_team(
        db_session,
        conversation=conversation,
        service_team_id=team.id,
        now=now + timedelta(minutes=2),
    )

    keys = [row.dedupe_key for row in db_session.query(InboxQueueNotification).all()]
    assert entry.admission_generation == 2
    assert len(keys) == 2
    assert first_key != keys[-1]
    assert "generation:2" in keys[-1]


def test_rejected_queue_delivery_owner_cancels_and_replay_is_noop(db_session) -> None:
    delivery = Notification(
        channel=NotificationChannel.whatsapp,
        recipient="unit-test-recipient",
        status=NotificationStatus.sending,
        metadata_={"automation_kind": "queue_notification"},
    )
    db_session.add(delivery)
    db_session.commit()
    command = team_inbox_queue_notifications.SettleRejectedQueueDeliveryCommand(
        context=CommandContext.system(
            actor="notification-queue-worker",
            scope="team-inbox:queue-delivery",
            reason="Unit suppression revalidation",
        ),
        notification_id=delivery.id,
    )
    db_session.rollback()
    result = team_inbox_queue_notifications.settle_rejected_queue_delivery(
        db_session, command=command
    )
    assert result.suppressed and not result.deferred
    assert not db_session.in_transaction()
    replay = team_inbox_queue_notifications.settle_rejected_queue_delivery(
        db_session, command=command
    )
    assert not replay.suppressed and not replay.deferred
    db_session.refresh(delivery)
    assert delivery.status is NotificationStatus.canceled
    assert delivery.metadata_["queue_suppression_reason"] == "invalid_queue_metadata"


def test_changed_queue_delivery_decision_defers_for_a_fresh_claim(db_session) -> None:
    delivery = Notification(
        channel=NotificationChannel.whatsapp,
        recipient="unit-test-recipient",
        status=NotificationStatus.sending,
        metadata_={},
    )
    db_session.add(delivery)
    db_session.commit()
    command = team_inbox_queue_notifications.SettleRejectedQueueDeliveryCommand(
        context=CommandContext.system(
            actor="notification-queue-worker",
            scope="team-inbox:queue-delivery",
            reason="Revalidate changed delivery metadata",
        ),
        notification_id=delivery.id,
    )
    db_session.rollback()
    result = team_inbox_queue_notifications.settle_rejected_queue_delivery(
        db_session, command=command
    )
    assert result.deferred and not result.suppressed
    assert not db_session.in_transaction()
    db_session.refresh(delivery)
    assert delivery.status is NotificationStatus.queued
