"""Customer-visible FIFO queue notices for Team Inbox."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.ai_intake import AiIntakePolicyVersion, AiIntakeSession
from app.models.team_inbox import (
    InboxChannelType,
    InboxConversation,
    InboxConversationQueueEntry,
    InboxQueueEntryStatus,
    InboxQueueNotification,
)
from app.services import ai_conversation_intake, team_inbox_outbound
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

OWNER = "communications.team_inbox_routing"
_QUEUE_NOTICE_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="customer-visible FIFO queue notification evidence",
    name="execute_team_inbox_queue_notification_command",
)

SUPPORTED_NOTICE_CHANNELS = frozenset(
    {
        InboxChannelType.whatsapp.value,
        InboxChannelType.facebook_messenger.value,
        InboxChannelType.instagram_dm.value,
    }
)
NOTICE_INITIAL = "initial"
NOTICE_POSITION_UPDATE = "position_update"
NOTICE_HEARTBEAT = "heartbeat"
NOTICE_HANDOFF = "handoff"
NOTICE_CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class QueueNotificationSweepCommand:
    context: CommandContext
    limit: int = 200
    now: datetime | None = None


@dataclass(frozen=True, slots=True)
class QueueNotificationSweepResult:
    sent: int
    skipped: int
    failed: int


def current_queue_position(db: Session, entry: InboxConversationQueueEntry) -> int:
    ahead = (
        db.query(func.count(InboxConversationQueueEntry.id))
        .filter(InboxConversationQueueEntry.service_team_id == entry.service_team_id)
        .filter(
            InboxConversationQueueEntry.status == InboxQueueEntryStatus.queued.value
        )
        .filter(
            (InboxConversationQueueEntry.entered_at < entry.entered_at)
            | (
                (InboxConversationQueueEntry.entered_at == entry.entered_at)
                & (InboxConversationQueueEntry.queue_position <= entry.queue_position)
            )
        )
        .scalar()
        or 1
    )
    return int(ahead)


def _last_sent_notice(
    db: Session, entry_id: UUID, kinds: tuple[str, ...]
) -> InboxQueueNotification | None:
    return (
        db.query(InboxQueueNotification)
        .filter(InboxQueueNotification.queue_entry_id == entry_id)
        .filter(InboxQueueNotification.notification_kind.in_(kinds))
        .filter(InboxQueueNotification.status == "sent")
        .order_by(InboxQueueNotification.sent_at.desc())
        .first()
    )


def _queue_policy(db: Session, conversation: InboxConversation) -> dict[str, object]:
    session = None
    session_id = dict(conversation.metadata_ or {}).get("ai_intake_session_id")
    if session_id:
        try:
            session = db.get(AiIntakeSession, UUID(str(session_id)))
        except (TypeError, ValueError):
            session = None
    version = (
        db.get(AiIntakePolicyVersion, session.policy_version_id)
        if session is not None and session.policy_version_id is not None
        else None
    )
    raw = dict(version.queue_templates or {}) if version is not None else {}
    return {
        **ai_conversation_intake.DEFAULT_QUEUE_TEMPLATES,
        **{
            key: str(raw[key])
            for key in ai_conversation_intake.DEFAULT_QUEUE_TEMPLATES
            if str(raw.get(key) or "").strip()
        },
        "position_update_minutes": int(
            raw.get("position_update_minutes")
            or ai_conversation_intake.DEFAULT_QUEUE_POSITION_UPDATE_MINUTES
        ),
        "heartbeat_minutes": int(
            raw.get("heartbeat_minutes")
            or ai_conversation_intake.DEFAULT_QUEUE_HEARTBEAT_MINUTES
        ),
        "display_name": (
            session.display_name
            if session is not None
            else ai_conversation_intake.DEFAULT_DISPLAY_NAME
        ),
    }


def _queue_policy_minutes(policy: dict[str, object], key: str, default: int) -> int:
    value = policy.get(key)
    try:
        return int(value) if isinstance(value, (str, int, float)) else default
    except (TypeError, ValueError):
        return default


def _queue_lifecycle(entry: InboxConversationQueueEntry) -> str:
    entered_at = entry.entered_at
    if entered_at.tzinfo is None or entered_at.utcoffset() is None:
        entered_at = entered_at.replace(tzinfo=UTC)
    return entered_at.astimezone(UTC).isoformat()


def _logical_key(
    *,
    entry: InboxConversationQueueEntry,
    kind: str,
    position: int,
    now: datetime,
    policy_minutes: int,
) -> str:
    lifecycle = _queue_lifecycle(entry)
    if kind in {NOTICE_INITIAL, NOTICE_HANDOFF}:
        return f"queue-notice:{entry.id}:{lifecycle}:{kind}"
    if kind == NOTICE_POSITION_UPDATE:
        minute = max(policy_minutes, 1)
        window = int(now.timestamp()) // (minute * 60)
        return f"queue-notice:{entry.id}:{lifecycle}:{kind}:{position}:{window}"
    if kind == NOTICE_HEARTBEAT:
        minute = max(policy_minutes, 1)
        window = int(now.timestamp()) // (minute * 60)
        return f"queue-notice:{entry.id}:{lifecycle}:{kind}:{window}"
    return f"queue-notice:{entry.id}:{lifecycle}:{kind}:{position}:{now.isoformat()}"


def _schedule_next_due(
    notice: InboxQueueNotification,
    *,
    now: datetime,
    minutes: int,
) -> None:
    notice.next_due_at = now + timedelta(minutes=max(int(minutes), 1))


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _send_notice(
    db: Session,
    *,
    entry: InboxConversationQueueEntry,
    conversation: InboxConversation,
    kind: str,
    position: int,
    body: str,
    now: datetime,
    existing_notice: InboxQueueNotification | None = None,
) -> InboxQueueNotification:
    policy = _queue_policy(db, conversation)
    update_minutes = _queue_policy_minutes(
        policy,
        "position_update_minutes",
        ai_conversation_intake.DEFAULT_QUEUE_POSITION_UPDATE_MINUTES,
    )
    heartbeat_minutes = _queue_policy_minutes(
        policy,
        "heartbeat_minutes",
        ai_conversation_intake.DEFAULT_QUEUE_HEARTBEAT_MINUTES,
    )
    dedupe_key = (
        existing_notice.dedupe_key
        if existing_notice is not None
        else _logical_key(
            entry=entry,
            kind=kind,
            position=position,
            now=now,
            policy_minutes=(
                heartbeat_minutes if kind == NOTICE_HEARTBEAT else update_minutes
            ),
        )
    )
    existing = existing_notice or (
        db.query(InboxQueueNotification)
        .filter(InboxQueueNotification.dedupe_key == dedupe_key)
        .with_for_update()
        .one_or_none()
    )
    if existing is not None:
        if existing.status == "sent" and kind in {NOTICE_INITIAL, NOTICE_HANDOFF}:
            return existing
        if existing.status == "sent" and kind in {
            NOTICE_POSITION_UPDATE,
            NOTICE_HEARTBEAT,
        }:
            return existing
        notice = existing
        notice.queue_position = position
        notice.status = "pending"
        notice.next_due_at = None
    else:
        notice = InboxQueueNotification(
            queue_entry_id=entry.id,
            conversation_id=conversation.id,
            notification_kind=kind,
            queue_position=position,
            status="pending",
            dedupe_key=dedupe_key,
            next_due_at=None,
            metadata_={
                "source": "team_inbox_queue_notifications",
                "queue_lifecycle": _queue_lifecycle(entry),
            },
        )
        db.add(notice)
    db.flush()
    if conversation.channel_type not in SUPPORTED_NOTICE_CHANNELS:
        notice.status = "cancelled"
        notice.next_due_at = None
        db.flush()
        return notice
    display_name = str(policy["display_name"])
    result = team_inbox_outbound.send_ai_intake_message(
        db,
        conversation=conversation,
        body_text=body,
        metadata={
            "sender_type": "ai",
            "author_type": "ai",
            "automation_kind": "queue_notification",
            "ai_display_name": display_name,
            "author_name": display_name,
            "ai_message_purpose": f"queue_{kind}",
            "queue_entry_id": str(entry.id),
            "queue_position": position,
        },
        dedupe_key=dedupe_key,
        now=now,
    )
    notice.status = "sent" if result.kind == "queued" else "failed"
    notice.outbound_message_id = UUID(result.message_id) if result.message_id else None
    notice.sent_at = now if result.kind == "queued" else None
    if kind == NOTICE_HANDOFF:
        notice.next_due_at = None
    elif notice.status == "sent":
        _schedule_next_due(notice, now=now, minutes=update_minutes)
    else:
        _schedule_next_due(notice, now=now, minutes=update_minutes)
    notice.metadata_ = {
        **dict(notice.metadata_ or {}),
        "delivery_kind": result.kind,
        "delivery_reason": result.reason,
        "queue_lifecycle": _queue_lifecycle(entry),
    }
    db.flush()
    return notice


def send_initial_queue_notice(
    db: Session,
    *,
    entry: InboxConversationQueueEntry,
    conversation: InboxConversation,
    now: datetime | None = None,
) -> InboxQueueNotification | None:
    observed_at = now or datetime.now(UTC)
    if (
        entry.status != InboxQueueEntryStatus.queued.value
        or conversation.channel_type not in SUPPORTED_NOTICE_CHANNELS
    ):
        return None
    position = current_queue_position(db, entry)
    policy = _queue_policy(db, conversation)
    return _send_notice(
        db,
        entry=entry,
        conversation=conversation,
        kind=NOTICE_INITIAL,
        position=position,
        body=str(policy["initial"]).format(position=position),
        now=observed_at,
    )


def send_handoff_notice(
    db: Session,
    *,
    conversation: InboxConversation,
    entry: InboxConversationQueueEntry | None,
    now: datetime | None = None,
) -> InboxQueueNotification | None:
    if entry is None:
        return None
    if conversation.channel_type not in SUPPORTED_NOTICE_CHANNELS:
        return None
    observed_at = now or datetime.now(UTC)
    policy = _queue_policy(db, conversation)
    return _send_notice(
        db,
        entry=entry,
        conversation=conversation,
        kind=NOTICE_HANDOFF,
        position=0,
        body=str(policy["handoff"]),
        now=observed_at,
    )


def _cancel_notice(notice: InboxQueueNotification) -> None:
    notice.status = NOTICE_CANCELLED
    notice.next_due_at = None


def _replace_due_notice(
    due_notice: InboxQueueNotification,
    replacement: InboxQueueNotification | None,
) -> InboxQueueNotification | None:
    if replacement is not None and replacement.id != due_notice.id:
        due_notice.next_due_at = None
    return replacement


def _process_due_notice(
    db: Session,
    *,
    notice: InboxQueueNotification,
    observed_at: datetime,
) -> InboxQueueNotification | None:
    entry = db.get(InboxConversationQueueEntry, notice.queue_entry_id)
    conversation = db.get(InboxConversation, notice.conversation_id)
    if entry is None or conversation is None or not conversation.is_active:
        _cancel_notice(notice)
        return None
    if (
        entry.status != InboxQueueEntryStatus.queued.value
        or conversation.channel_type not in SUPPORTED_NOTICE_CHANNELS
    ):
        _cancel_notice(notice)
        return None
    position = current_queue_position(db, entry)
    policy = _queue_policy(db, conversation)
    update_minutes = _queue_policy_minutes(
        policy,
        "position_update_minutes",
        ai_conversation_intake.DEFAULT_QUEUE_POSITION_UPDATE_MINUTES,
    )
    heartbeat_minutes = _queue_policy_minutes(
        policy,
        "heartbeat_minutes",
        ai_conversation_intake.DEFAULT_QUEUE_HEARTBEAT_MINUTES,
    )

    if notice.status == "failed":
        return _send_notice(
            db,
            entry=entry,
            conversation=conversation,
            kind=notice.notification_kind,
            position=position if notice.notification_kind != NOTICE_HANDOFF else 0,
            body=str(
                policy.get(notice.notification_kind) or policy[NOTICE_HEARTBEAT]
            ).format(position=position),
            now=observed_at,
            existing_notice=notice,
        )

    last_sent = _last_sent_notice(
        db,
        entry.id,
        (NOTICE_INITIAL, NOTICE_POSITION_UPDATE, NOTICE_HEARTBEAT),
    )
    if last_sent is None:
        return _replace_due_notice(
            notice,
            send_initial_queue_notice(
                db, entry=entry, conversation=conversation, now=observed_at
            ),
        )
    if last_sent.queue_position is not None and position != last_sent.queue_position:
        return _replace_due_notice(
            notice,
            _send_notice(
                db,
                entry=entry,
                conversation=conversation,
                kind=NOTICE_POSITION_UPDATE,
                position=position,
                body=str(policy[NOTICE_POSITION_UPDATE]).format(position=position),
                now=observed_at,
            ),
        )
    elapsed = (
        _aware_utc(observed_at) - _aware_utc(last_sent.sent_at)
        if last_sent.sent_at is not None
        else timedelta()
    )
    if elapsed >= timedelta(minutes=heartbeat_minutes):
        return _replace_due_notice(
            notice,
            _send_notice(
                db,
                entry=entry,
                conversation=conversation,
                kind=NOTICE_HEARTBEAT,
                position=position,
                body=str(policy[NOTICE_HEARTBEAT]).format(position=position),
                now=observed_at,
            ),
        )
    if last_sent.sent_at is not None:
        target_due = _aware_utc(last_sent.sent_at) + timedelta(
            minutes=heartbeat_minutes
        )
        notice.next_due_at = max(target_due, observed_at + timedelta(minutes=1))
    else:
        _schedule_next_due(notice, now=observed_at, minutes=update_minutes)
    return None


def sweep_queue_notifications(
    db: Session, command: QueueNotificationSweepCommand
) -> QueueNotificationSweepResult:
    observed_at = command.now or datetime.now(UTC)

    def _operation() -> QueueNotificationSweepResult:
        sent = 0
        skipped = 0
        failed = 0
        due_notices = (
            db.query(InboxQueueNotification)
            .filter(InboxQueueNotification.status.in_(("sent", "failed")))
            .filter(InboxQueueNotification.next_due_at.isnot(None))
            .filter(InboxQueueNotification.next_due_at <= observed_at)
            .order_by(InboxQueueNotification.next_due_at.asc())
            .limit(command.limit)
            .with_for_update(skip_locked=True)
            .all()
        )
        for due_notice in due_notices:
            notice = _process_due_notice(db, notice=due_notice, observed_at=observed_at)
            if notice is None:
                skipped += 1
            elif notice.status == "sent":
                sent += 1
            else:
                failed += 1
        return QueueNotificationSweepResult(sent=sent, skipped=skipped, failed=failed)

    return execute_owner_command(
        db,
        definition=_QUEUE_NOTICE_COMMAND,
        context=command.context,
        operation=_operation,
    )
