"""Customer-visible FIFO queue notices for Team Inbox."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.ai_intake import AiIntakePolicyVersion, AiIntakeSession
from app.models.notification import Notification, NotificationStatus
from app.models.service_team import ServiceTeam
from app.models.team_inbox import (
    InboxChannelType,
    InboxConversation,
    InboxConversationAssignment,
    InboxConversationQueueEntry,
    InboxMessage,
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
logger = logging.getLogger(__name__)
_QUEUE_NOTICE_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="customer-visible FIFO queue notification evidence",
    name="execute_team_inbox_queue_notification_command",
)
_QUEUE_SUPPRESSION_COMMAND = OwnerCommandDefinition(
    owner="communications.team_inbox_queue_notifications",
    concern="queue notification delivery ledger writes",
    name="settle_rejected_queue_delivery",
)

SUPPORTED_NOTICE_CHANNELS = frozenset(
    {
        InboxChannelType.whatsapp.value,
        InboxChannelType.facebook_messenger.value,
        InboxChannelType.instagram_dm.value,
        InboxChannelType.chat_widget.value,
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


@dataclass(frozen=True, slots=True)
class QueueDeliveryPreflightOutcome:
    applies: bool
    allowed: bool
    reason: str
    queue_entry_id: UUID | None = None
    admission_generation: int | None = None
    current_visible_position: int | None = None


@dataclass(frozen=True, slots=True)
class SettleRejectedQueueDeliveryCommand:
    context: CommandContext
    notification_id: UUID


@dataclass(frozen=True, slots=True)
class QueueDeliverySettlement:
    suppressed: bool
    deferred: bool = False


def settle_rejected_queue_delivery(
    db: Session, command: SettleRejectedQueueDeliveryCommand
) -> QueueDeliverySettlement:
    """Recheck a denied delivery under the owner transaction before suppressing it.

    A changed decision returns the claimed notification to the queue; only a
    fresh worker claim may contact the provider with current lifecycle locks.
    """
    from app.services.communication_intents import record_delivery_outcome

    def operation() -> QueueDeliverySettlement:
        observed = db.get(Notification, command.notification_id)
        if observed is None:
            return QueueDeliverySettlement(suppressed=False)
        preflight_queue_notification_delivery(
            db, notification=observed, record_suppression=False
        )
        notification = (
            db.query(Notification)
            .filter(Notification.id == command.notification_id)
            .with_for_update()
            .populate_existing()
            .one_or_none()
        )
        if notification is None or notification.status not in {
            NotificationStatus.sending,
            NotificationStatus.queued,
        }:
            return QueueDeliverySettlement(suppressed=False)
        decision = preflight_queue_notification_delivery(db, notification=notification)
        if not decision.applies or decision.allowed:
            notification.status = NotificationStatus.queued
            db.flush()
            return QueueDeliverySettlement(suppressed=False, deferred=True)
        notification.status = NotificationStatus.canceled
        notification.last_error = f"queue_notification_suppressed:{decision.reason}"
        notification.metadata_ = {
            **dict(notification.metadata_ or {}),
            "queue_suppression_reason": decision.reason,
        }
        record_delivery_outcome(db, notification)
        db.flush()
        return QueueDeliverySettlement(suppressed=True)

    return execute_owner_command(
        db,
        definition=_QUEUE_SUPPRESSION_COMMAND,
        context=command.context,
        operation=operation,
    )


def current_visible_position(db: Session, entry: InboxConversationQueueEntry) -> int:
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
                & (
                    InboxConversationQueueEntry.queue_position
                    <= entry.admission_sequence
                )
            )
        )
        .scalar()
        or 1
    )
    return int(ahead)


def current_queue_position(db: Session, entry: InboxConversationQueueEntry) -> int:
    """Compatibility alias; this value is a live customer-visible rank."""

    return current_visible_position(db, entry)


def cancel_queue_lifecycle_notifications(
    db: Session,
    *,
    entry: InboxConversationQueueEntry,
    reason: str,
) -> int:
    """Cancel undelivered customer notices for one admission generation."""

    notices = (
        db.query(InboxQueueNotification)
        .filter(InboxQueueNotification.queue_entry_id == entry.id)
        .filter(
            InboxQueueNotification.admission_generation == entry.admission_generation
        )
        .filter(InboxQueueNotification.notification_kind != NOTICE_HANDOFF)
        .filter(InboxQueueNotification.status != NOTICE_CANCELLED)
        .with_for_update()
        .all()
    )
    message_ids = [
        notice.outbound_message_id for notice in notices if notice.outbound_message_id
    ]
    messages = (
        db.query(InboxMessage).filter(InboxMessage.id.in_(message_ids)).all()
        if message_ids
        else []
    )
    notification_ids = [
        message.notification_id for message in messages if message.notification_id
    ]
    deliveries = (
        db.query(Notification)
        .filter(Notification.id.in_(notification_ids))
        .filter(
            Notification.status.in_(
                (
                    NotificationStatus.queued,
                    NotificationStatus.failed,
                    NotificationStatus.sending,
                )
            )
        )
        .with_for_update()
        .all()
        if notification_ids
        else []
    )
    for delivery in deliveries:
        delivery.status = NotificationStatus.canceled
        delivery.last_error = f"queue_notification_suppressed:{reason}"[:255]
        delivery.metadata_ = {
            **dict(delivery.metadata_ or {}),
            "queue_suppression_reason": reason,
        }
    cancelled_delivery_ids = {delivery.id for delivery in deliveries}
    cancelled_message_ids = {
        message.id
        for message in messages
        if message.notification_id in cancelled_delivery_ids
    }
    for notice in notices:
        notice.next_due_at = None
        notice.suppression_reason = reason[:80]
        if (
            notice.outbound_message_id is None
            or notice.outbound_message_id in cancelled_message_ids
            or notice.status in {"pending", "failed"}
        ):
            notice.status = NOTICE_CANCELLED
    for message in messages:
        if message.notification_id not in cancelled_delivery_ids:
            continue
        message.metadata_ = {
            **dict(message.metadata_ or {}),
            "delivery_status": "cancelled",
            "queue_suppression_reason": reason,
        }
    if notices or deliveries:
        logger.info(
            "team_inbox_queue_notifications_cancelled",
            extra={
                "event": "team_inbox_queue_notifications_cancelled",
                "queue_entry_id": str(entry.id),
                "queue_lifecycle": _queue_lifecycle(entry),
                "team_id": str(entry.service_team_id),
                "admission_sequence": entry.admission_sequence,
                "notification_reason": reason,
                "ledger_count": len(notices),
                "delivery_count": len(deliveries),
            },
        )
    db.flush()
    return len(deliveries)


def preflight_queue_notification_delivery(
    db: Session, *, notification: Notification, record_suppression: bool = True
) -> QueueDeliveryPreflightOutcome:
    """Serialize provider delivery against assignment and queue settlement."""

    metadata = dict(notification.metadata_ or {})
    if metadata.get("automation_kind") != "queue_notification":
        return QueueDeliveryPreflightOutcome(
            applies=False, allowed=True, reason="not_queue_notification"
        )
    try:
        entry_id = UUID(str(metadata.get("queue_entry_id")))
        conversation_id = UUID(str(metadata.get("conversation_id")))
        raw_generation = metadata.get("admission_generation")
        if isinstance(raw_generation, bool) or not isinstance(
            raw_generation, (int, str)
        ):
            raise ValueError("invalid admission generation")
        generation = int(raw_generation)
        if generation < 1:
            raise ValueError("invalid admission generation")
    except (TypeError, ValueError):
        return QueueDeliveryPreflightOutcome(
            applies=True, allowed=False, reason="invalid_queue_metadata"
        )
    kind = str(metadata.get("queue_notification_kind") or "")
    queue_dedupe_key = str(metadata.get("queue_notification_dedupe_key") or "")
    expected_position_raw = metadata.get("current_visible_position")
    expected_position = (
        int(expected_position_raw)
        if isinstance(expected_position_raw, (int, str))
        and str(expected_position_raw).isdigit()
        else None
    )
    conversation = (
        db.query(InboxConversation)
        .filter(InboxConversation.id == conversation_id)
        .with_for_update()
        .one_or_none()
    )
    entry = (
        db.query(InboxConversationQueueEntry)
        .filter(InboxConversationQueueEntry.id == entry_id)
        .with_for_update()
        .one_or_none()
    )
    ledger: InboxQueueNotification | None = None

    def outcome(
        allowed: bool,
        reason: str,
        position: int | None = None,
        *,
        cancel_ledger: bool = True,
    ) -> QueueDeliveryPreflightOutcome:
        if record_suppression and not allowed and ledger is not None:
            ledger.suppression_reason = reason[:80]
            if cancel_ledger:
                _cancel_notice(ledger, reason)
            db.flush()
        level = logger.info if allowed else logger.warning
        level(
            "team_inbox_queue_delivery_preflight",
            extra={
                "event": "team_inbox_queue_delivery_preflight",
                "queue_entry_id": str(entry_id),
                "queue_lifecycle": f"generation:{generation}",
                "notification_kind": kind,
                "notification_idempotency_key": queue_dedupe_key,
                "new_visible_position": position,
                "notification_reason": reason,
                "notification_suppressed": not allowed,
            },
        )
        return QueueDeliveryPreflightOutcome(
            applies=True,
            allowed=allowed,
            reason=reason,
            queue_entry_id=entry_id,
            admission_generation=generation,
            current_visible_position=position,
        )

    if conversation is None or entry is None:
        return outcome(False, "queue_state_missing")
    if entry.conversation_id != conversation.id:
        return outcome(False, "queue_conversation_mismatch")
    if entry.admission_generation != generation:
        return outcome(False, "superseded_admission_generation")
    ledger = (
        db.query(InboxQueueNotification)
        .filter(InboxQueueNotification.dedupe_key == queue_dedupe_key)
        .one_or_none()
    )
    if ledger is None or ledger.status == NOTICE_CANCELLED:
        return outcome(False, "notification_ledger_cancelled")
    active_assignment = (
        db.query(InboxConversationAssignment.id)
        .filter(InboxConversationAssignment.conversation_id == conversation.id)
        .filter(InboxConversationAssignment.is_active.is_(True))
        .scalar()
    )
    if kind == NOTICE_HANDOFF:
        if (
            entry.status == InboxQueueEntryStatus.promoted.value
            and active_assignment is not None
        ):
            return outcome(True, "current_handoff")
        return outcome(False, "handoff_state_invalid")
    if not conversation.is_active:
        return outcome(False, "conversation_inactive")
    if conversation.status == "resolved":
        return outcome(False, "conversation_resolved")
    if active_assignment is not None:
        return outcome(False, "human_assignment_active")
    if entry.status != InboxQueueEntryStatus.queued.value:
        return outcome(False, "queue_lifecycle_inactive")
    if conversation.primary_service_team_id != entry.service_team_id:
        return outcome(False, "queue_team_mismatch")
    visible_position = current_visible_position(db, entry)
    if kind in {NOTICE_INITIAL, NOTICE_POSITION_UPDATE} and (
        expected_position is None or expected_position != visible_position
    ):
        if not record_suppression:
            return outcome(False, "visible_position_stale", visible_position)
        if expected_position is not None and visible_position < expected_position:
            _cancel_notice(ledger, "visible_position_stale")
            policy = _queue_policy(db, conversation)
            _send_notice(
                db,
                entry=entry,
                conversation=conversation,
                kind=NOTICE_POSITION_UPDATE,
                position=visible_position,
                body=_render_queue_template(
                    policy[NOTICE_POSITION_UPDATE],
                    position=visible_position,
                    team_name=_queue_team_name(db, entry),
                ),
                now=datetime.now(UTC),
            )
            return outcome(False, "visible_position_stale", visible_position)
        policy = _queue_policy(db, conversation)
        _schedule_next_due(
            ledger,
            now=datetime.now(UTC),
            minutes=_queue_policy_minutes(
                policy,
                "position_update_minutes",
                ai_conversation_intake.DEFAULT_QUEUE_POSITION_UPDATE_MINUTES,
            ),
        )
        return outcome(
            False,
            "visible_position_stale",
            visible_position,
            cancel_ledger=False,
        )
    if kind == NOTICE_HEARTBEAT and not bool(
        _queue_policy(db, conversation).get("heartbeat_enabled", False)
    ):
        return outcome(False, "heartbeat_disabled", visible_position)
    return outcome(True, "queue_notification_current", visible_position)


def _last_sent_notice(
    db: Session,
    entry_id: UUID,
    admission_generation: int,
    kinds: tuple[str, ...],
) -> InboxQueueNotification | None:
    return (
        db.query(InboxQueueNotification)
        .filter(InboxQueueNotification.queue_entry_id == entry_id)
        .filter(InboxQueueNotification.admission_generation == admission_generation)
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
        "heartbeat_enabled": _queue_policy_bool(
            raw.get("heartbeat_enabled"),
            default=ai_conversation_intake.DEFAULT_QUEUE_HEARTBEAT_ENABLED,
        ),
        "display_name": (
            session.display_name
            if session is not None
            else ai_conversation_intake.DEFAULT_DISPLAY_NAME
        ),
    }


def _queue_policy_bool(value: object, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off", ""}:
            return False
    return default


def _queue_policy_minutes(policy: dict[str, object], key: str, default: int) -> int:
    value = policy.get(key)
    try:
        return int(value) if isinstance(value, (str, int, float)) else default
    except (TypeError, ValueError):
        return default


def _queue_team_name(db: Session, entry: InboxConversationQueueEntry) -> str:
    team = db.get(ServiceTeam, entry.service_team_id)
    return str(team.name) if team is not None else "the support team"


def _render_queue_template(
    template: object,
    *,
    position: int,
    team_name: str,
) -> str:
    body = str(template or "")
    variables = {
        "current_visible_position": str(position),
        "position": str(position),
        "queue_position": str(position),
        "team_name": team_name,
    }
    for key, value in variables.items():
        body = body.replace("{{" + key + "}}", value)
        body = body.replace("{" + key + "}", value)
    return body


def _queue_lifecycle(entry: InboxConversationQueueEntry) -> str:
    return f"generation:{entry.admission_generation}"


def _logical_key(
    *,
    entry: InboxConversationQueueEntry,
    kind: str,
    position: int,
    now: datetime,
) -> str:
    lifecycle = _queue_lifecycle(entry)
    if kind in {NOTICE_INITIAL, NOTICE_HANDOFF}:
        return f"queue-notice:{entry.id}:{lifecycle}:{kind}"
    if kind == NOTICE_POSITION_UPDATE:
        return f"queue-position:{entry.id}:{lifecycle}:{position}"
    if kind == NOTICE_HEARTBEAT:
        return f"queue-heartbeat:{entry.id}:{lifecycle}:{now.isoformat()}"
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


def _suppressed_by_human_takeover(
    db: Session,
    *,
    conversation: InboxConversation,
    kind: str,
) -> bool:
    if kind == NOTICE_HANDOFF:
        return False
    return ai_conversation_intake.has_human_takeover(db, conversation)


def _cancel_notice(
    notice: InboxQueueNotification, reason: str = "queue_lifecycle_inactive"
) -> None:
    notice.status = NOTICE_CANCELLED
    notice.next_due_at = None
    notice.suppression_reason = reason[:80]


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
    dedupe_key = (
        existing_notice.dedupe_key
        if existing_notice is not None
        else _logical_key(
            entry=entry,
            kind=kind,
            position=position,
            now=now,
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
            admission_generation=entry.admission_generation,
            queue_position=position,
            status="pending",
            dedupe_key=dedupe_key,
            next_due_at=None,
            metadata_={
                "source": "team_inbox_queue_notifications",
                "queue_lifecycle": _queue_lifecycle(entry),
                "admission_sequence": entry.admission_sequence,
            },
        )
        db.add(notice)
    db.flush()
    if _suppressed_by_human_takeover(db, conversation=conversation, kind=kind):
        _cancel_notice(notice, "human_takeover")
        notice.suppression_reason = "human_takeover"
        notice.metadata_ = {
            **dict(notice.metadata_ or {}),
            "delivery_kind": "suppressed",
            "delivery_reason": "human_takeover",
            "queue_lifecycle": _queue_lifecycle(entry),
        }
        db.flush()
        return notice
    if conversation.channel_type not in SUPPORTED_NOTICE_CHANNELS:
        _cancel_notice(notice, "unsupported_channel")
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
            "current_visible_position": position,
            "admission_generation": entry.admission_generation,
            "queue_notification_kind": kind,
            "queue_notification_dedupe_key": dedupe_key,
        },
        dedupe_key=dedupe_key,
        now=now,
    )
    notice.status = "sent" if result.kind == "queued" else "failed"
    notice.outbound_message_id = UUID(result.message_id) if result.message_id else None
    notice.sent_at = now if result.kind == "queued" else None
    if notice.status == "sent" and kind in {NOTICE_INITIAL, NOTICE_POSITION_UPDATE}:
        entry.last_notified_position = position
        entry.last_position_notified_at = now
    elif notice.status == "sent" and kind == NOTICE_HEARTBEAT:
        entry.last_heartbeat_at = now
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
    logger.info(
        "team_inbox_queue_notification_decision",
        extra={
            "event": "team_inbox_queue_notification_decision",
            "queue_entry_id": str(entry.id),
            "queue_lifecycle": _queue_lifecycle(entry),
            "team_id": str(entry.service_team_id),
            "admission_sequence": entry.admission_sequence,
            "new_visible_position": position,
            "notification_kind": kind,
            "notification_status": notice.status,
            "notification_reason": result.reason,
            "notification_idempotency_key": dedupe_key,
        },
    )
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
    team_name = _queue_team_name(db, entry)
    return _send_notice(
        db,
        entry=entry,
        conversation=conversation,
        kind=NOTICE_INITIAL,
        position=position,
        body=_render_queue_template(
            policy["initial"],
            position=position,
            team_name=team_name,
        ),
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
    team_name = _queue_team_name(db, entry)
    return _send_notice(
        db,
        entry=entry,
        conversation=conversation,
        kind=NOTICE_HANDOFF,
        position=0,
        body=_render_queue_template(
            policy["handoff"],
            position=0,
            team_name=team_name,
        ),
        now=observed_at,
    )


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
        _cancel_notice(notice, "missing_or_inactive_conversation")
        return None
    if notice.admission_generation != entry.admission_generation:
        _cancel_notice(notice, "superseded_admission_generation")
        return None
    if (
        entry.status != InboxQueueEntryStatus.queued.value
        or conversation.channel_type not in SUPPORTED_NOTICE_CHANNELS
    ):
        _cancel_notice(notice, "queue_lifecycle_inactive")
        return None
    if _suppressed_by_human_takeover(
        db,
        conversation=conversation,
        kind=notice.notification_kind,
    ):
        _cancel_notice(notice, "human_takeover")
        return None
    position = current_visible_position(db, entry)
    policy = _queue_policy(db, conversation)
    team_name = _queue_team_name(db, entry)
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
            body=_render_queue_template(
                policy.get(notice.notification_kind) or policy[NOTICE_HEARTBEAT],
                position=position,
                team_name=team_name,
            ),
            now=observed_at,
            existing_notice=notice,
        )

    last_sent = _last_sent_notice(
        db,
        entry.id,
        entry.admission_generation,
        (NOTICE_INITIAL, NOTICE_POSITION_UPDATE, NOTICE_HEARTBEAT),
    )
    if last_sent is None:
        return _replace_due_notice(
            notice,
            send_initial_queue_notice(
                db, entry=entry, conversation=conversation, now=observed_at
            ),
        )
    last_position = entry.last_notified_position
    if last_position is None and last_sent.queue_position is not None:
        last_position = last_sent.queue_position
    if last_position is not None and position < last_position:
        return _replace_due_notice(
            notice,
            _send_notice(
                db,
                entry=entry,
                conversation=conversation,
                kind=NOTICE_POSITION_UPDATE,
                position=position,
                body=_render_queue_template(
                    policy[NOTICE_POSITION_UPDATE],
                    position=position,
                    team_name=team_name,
                ),
                now=observed_at,
            ),
        )
    if last_position is not None and position > last_position:
        logger.warning(
            "team_inbox_queue_visible_position_worsened",
            extra={
                "event": "team_inbox_queue_visible_position_worsened",
                "queue_entry_id": str(entry.id),
                "queue_lifecycle": _queue_lifecycle(entry),
                "team_id": str(entry.service_team_id),
                "admission_sequence": entry.admission_sequence,
                "old_visible_position": last_position,
                "new_visible_position": position,
                "notification_reason": "worsening_position_suppressed",
            },
        )
    heartbeat_enabled = bool(policy.get("heartbeat_enabled", False))
    last_customer_notice_at = entry.last_position_notified_at
    if entry.last_heartbeat_at is not None and (
        last_customer_notice_at is None
        or _aware_utc(entry.last_heartbeat_at) > _aware_utc(last_customer_notice_at)
    ):
        last_customer_notice_at = entry.last_heartbeat_at
    elapsed = (
        _aware_utc(observed_at) - _aware_utc(last_customer_notice_at)
        if last_customer_notice_at is not None
        else timedelta()
    )
    if heartbeat_enabled and elapsed >= timedelta(minutes=heartbeat_minutes):
        return _replace_due_notice(
            notice,
            _send_notice(
                db,
                entry=entry,
                conversation=conversation,
                kind=NOTICE_HEARTBEAT,
                position=position,
                body=_render_queue_template(
                    policy[NOTICE_HEARTBEAT],
                    position=position,
                    team_name=team_name,
                ),
                now=observed_at,
            ),
        )
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
        due_candidates = (
            db.query(
                InboxQueueNotification.id,
                InboxQueueNotification.conversation_id,
                InboxQueueNotification.queue_entry_id,
            )
            .filter(InboxQueueNotification.status.in_(("sent", "failed")))
            .filter(InboxQueueNotification.next_due_at.isnot(None))
            .filter(InboxQueueNotification.next_due_at <= observed_at)
            .order_by(InboxQueueNotification.next_due_at.asc())
            .limit(command.limit)
            .all()
        )
        for notice_id, conversation_id, entry_id in due_candidates:
            db.query(InboxConversation.id).filter(
                InboxConversation.id == conversation_id
            ).with_for_update().scalar()
            db.query(InboxConversationQueueEntry.id).filter(
                InboxConversationQueueEntry.id == entry_id
            ).with_for_update().scalar()
            due_notice = (
                db.query(InboxQueueNotification)
                .filter(InboxQueueNotification.id == notice_id)
                .filter(InboxQueueNotification.status.in_(("sent", "failed")))
                .filter(InboxQueueNotification.next_due_at.isnot(None))
                .filter(InboxQueueNotification.next_due_at <= observed_at)
                .with_for_update(skip_locked=True)
                .one_or_none()
            )
            if due_notice is None:
                logger.info(
                    "team_inbox_queue_notification_suppressed: "
                    "due_notice_lock_not_acquired",
                    extra={
                        "event": "team_inbox_queue_notification_suppressed",
                        "queue_entry_id": str(entry_id),
                        "notification_reason": "due_notice_lock_not_acquired",
                    },
                )
                skipped += 1
                continue
            notice = _process_due_notice(db, notice=due_notice, observed_at=observed_at)
            if notice is None:
                suppression_reason = (
                    due_notice.suppression_reason or "not_due_by_policy"
                )
                logger.info(
                    "team_inbox_queue_notification_suppressed: %s (status=%s)",
                    suppression_reason,
                    due_notice.status,
                    extra={
                        "event": "team_inbox_queue_notification_suppressed",
                        "queue_entry_id": str(entry_id),
                        "queue_lifecycle": (
                            f"generation:{due_notice.admission_generation}"
                        ),
                        "notification_kind": due_notice.notification_kind,
                        "notification_reason": suppression_reason,
                        "notification_idempotency_key": due_notice.dedupe_key,
                    },
                )
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
