from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.models.service_team import ServiceTeam
from app.models.team_inbox import (
    InboxConversation,
    InboxConversationQueueEntry,
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


def test_queue_notification_sweep_uses_next_due_and_sends_heartbeat_once(
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

    five_minutes = team_inbox_queue_notifications.sweep_queue_notifications(
        db_session,
        team_inbox_queue_notifications.QueueNotificationSweepCommand(
            context=CommandContext.system(
                actor="test", scope="team-inbox:routing-command", reason="test"
            ),
            now=now + timedelta(minutes=5),
        ),
    )
    assert five_minutes.sent == 0
    assert db_session.query(InboxQueueNotification).count() == 1
    db_session.commit()

    heartbeat = team_inbox_queue_notifications.sweep_queue_notifications(
        db_session,
        team_inbox_queue_notifications.QueueNotificationSweepCommand(
            context=CommandContext.system(
                actor="test", scope="team-inbox:routing-command", reason="test"
            ),
            now=now + timedelta(minutes=16),
        ),
    )
    assert heartbeat.sent + heartbeat.failed == 1
    assert (
        db_session.query(InboxQueueNotification)
        .filter(InboxQueueNotification.notification_kind == "heartbeat")
        .count()
        == 1
    )
    db_session.commit()

    duplicate = team_inbox_queue_notifications.sweep_queue_notifications(
        db_session,
        team_inbox_queue_notifications.QueueNotificationSweepCommand(
            context=CommandContext.system(
                actor="test", scope="team-inbox:routing-command", reason="test"
            ),
            now=now + timedelta(minutes=16),
        ),
    )
    assert duplicate.sent == 0


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
            now=now + timedelta(minutes=6),
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
            now=now + timedelta(minutes=6),
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
            now=now + timedelta(minutes=5),
        ),
    )

    assert result.skipped == 1
    assert db_session.query(InboxQueueNotification).one().status == "cancelled"
