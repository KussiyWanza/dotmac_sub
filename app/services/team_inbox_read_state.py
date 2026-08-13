"""Canonical operator read cursors and unread projection for Team Inbox."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select

from app.models.team_inbox import (
    InboxConversation,
    InboxConversationReadState,
    InboxMessage,
    InboxMessageDirection,
)
from app.services.domain_errors import DomainError
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

OWNER = "communications.team_inbox_operator_state"
_MARK_READ = OwnerCommandDefinition(
    owner=OWNER,
    concern="operator read cursor",
    name="mark_team_inbox_conversation_read",
)
_REBUILD = OwnerCommandDefinition(
    owner=OWNER,
    concern="operator unread projection repair",
    name="rebuild_team_inbox_operator_read_state",
)


class TeamInboxReadStateError(DomainError):
    """Stable operator-state error mapped only by adapters."""


@dataclass(frozen=True, slots=True)
class MarkConversationReadCommand:
    context: CommandContext
    conversation_id: UUID
    person_id: UUID
    through_message_id: UUID | None = None
    read_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class RebuildOperatorReadStateCommand:
    context: CommandContext
    person_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ConversationReadOutcome:
    conversation_id: UUID
    person_id: UUID
    through_message_id: UUID | None
    last_read_at: datetime
    changed: bool
    command_id: UUID


@dataclass(frozen=True, slots=True)
class ReadStateRepairOutcome:
    inspected: int
    repaired: int


def _error(suffix: str, message: str, **details: object) -> TeamInboxReadStateError:
    return TeamInboxReadStateError(
        code=f"{OWNER}.{suffix}", message=message, details=details
    )


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _latest_message(db: Session, conversation_id: UUID) -> InboxMessage | None:
    return db.execute(
        select(InboxMessage)
        .where(InboxMessage.conversation_id == conversation_id)
        .order_by(
            InboxMessage.received_at.desc().nullslast(),
            InboxMessage.sent_at.desc().nullslast(),
            InboxMessage.created_at.desc(),
            InboxMessage.id.desc(),
        )
        .limit(1)
    ).scalar_one_or_none()


def mark_conversation_read(
    db: Session,
    command: MarkConversationReadCommand,
) -> ConversationReadOutcome:
    """Advance one operator cursor; retries never move the cursor backwards."""

    def operation() -> ConversationReadOutcome:
        conversation = db.execute(
            select(InboxConversation)
            .where(InboxConversation.id == command.conversation_id)
            .with_for_update()
        ).scalar_one_or_none()
        if conversation is None or not conversation.is_active:
            raise _error(
                "conversation_not_found",
                "Inbox conversation was not found.",
                conversation_id=str(command.conversation_id),
            )
        message = (
            db.get(InboxMessage, command.through_message_id)
            if command.through_message_id is not None
            else _latest_message(db, conversation.id)
        )
        if message is not None and message.conversation_id != conversation.id:
            raise _error(
                "message_scope_mismatch",
                "Read cursor message does not belong to the conversation.",
            )
        read_at = command.read_at or datetime.now(UTC)
        if read_at.tzinfo is None:
            raise _error(
                "invalid_read_time", "Read cursor time must be timezone-aware."
            )
        state = db.execute(
            select(InboxConversationReadState)
            .where(
                InboxConversationReadState.conversation_id == conversation.id,
                InboxConversationReadState.person_id == command.person_id,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if state is None:
            state = InboxConversationReadState(
                conversation_id=conversation.id,
                person_id=command.person_id,
                last_read_message_id=message.id if message is not None else None,
                last_read_at=read_at.astimezone(UTC),
            )
            db.add(state)
            changed = True
        elif read_at.astimezone(UTC) <= _utc(state.last_read_at):
            changed = False
        else:
            state.last_read_at = read_at.astimezone(UTC)
            state.last_read_message_id = message.id if message is not None else None
            changed = True
        db.flush()
        return ConversationReadOutcome(
            conversation_id=conversation.id,
            person_id=command.person_id,
            through_message_id=state.last_read_message_id,
            last_read_at=state.last_read_at,
            changed=changed,
            command_id=command.context.command_id,
        )

    return execute_owner_command(
        db,
        definition=_MARK_READ,
        context=command.context,
        operation=operation,
    )


def conversation_unread_message_counts(
    db: Session,
    *,
    conversation_ids: Sequence[UUID],
    person_id: UUID,
) -> dict[UUID, int]:
    """Count inbound messages after one operator's authoritative read cursor."""

    requested_ids = tuple(dict.fromkeys(conversation_ids))
    if not requested_ids:
        return {}
    counts = dict.fromkeys(requested_ids, 0)
    rows = db.execute(
        select(InboxMessage.conversation_id, func.count(InboxMessage.id))
        .select_from(InboxMessage)
        .outerjoin(
            InboxConversationReadState,
            and_(
                InboxConversationReadState.conversation_id
                == InboxMessage.conversation_id,
                InboxConversationReadState.person_id == person_id,
            ),
        )
        .where(
            InboxMessage.conversation_id.in_(requested_ids),
            InboxMessage.direction == InboxMessageDirection.inbound.value,
            InboxMessage.received_at.is_not(None),
            or_(
                InboxConversationReadState.id.is_(None),
                InboxConversationReadState.last_read_at < InboxMessage.received_at,
            ),
        )
        .group_by(InboxMessage.conversation_id)
    ).all()
    for conversation_id, unread_count in rows:
        counts[conversation_id] = int(unread_count)
    return counts


def conversation_is_unread(
    db: Session,
    *,
    conversation_id: UUID,
    person_id: UUID,
) -> bool:
    return (
        conversation_unread_message_counts(
            db,
            conversation_ids=(conversation_id,),
            person_id=person_id,
        ).get(conversation_id, 0)
        > 0
    )


def _latest_inbound_by_conversation():
    """One grouped fact set shared by unread filtering and fleet counts."""

    return (
        select(
            InboxMessage.conversation_id.label("conversation_id"),
            func.max(InboxMessage.received_at).label("last_inbound_at"),
        )
        .where(
            InboxMessage.direction == InboxMessageDirection.inbound.value,
            InboxMessage.received_at.is_not(None),
        )
        .group_by(InboxMessage.conversation_id)
        .subquery()
    )


def unread_conversation_ids_select(
    person_id: UUID,
    *,
    conversation_ids: Sequence[UUID] = (),
) -> Select[tuple[UUID]]:
    """Return a set-based unread conversation selector for one operator."""

    latest_inbound = _latest_inbound_by_conversation()
    query = (
        select(latest_inbound.c.conversation_id)
        .outerjoin(
            InboxConversationReadState,
            and_(
                InboxConversationReadState.conversation_id
                == latest_inbound.c.conversation_id,
                InboxConversationReadState.person_id == person_id,
            ),
        )
        .where(
            or_(
                InboxConversationReadState.id.is_(None),
                InboxConversationReadState.last_read_at
                < latest_inbound.c.last_inbound_at,
            )
        )
    )
    if conversation_ids:
        query = query.where(latest_inbound.c.conversation_id.in_(conversation_ids))
    return query


def unread_conversation_ids(
    db: Session,
    *,
    conversation_ids: Sequence[UUID],
    person_id: UUID,
) -> set[UUID]:
    """Which requested conversations this operator has not caught up on.

    One grouped statement serves a whole page. Callers previously asked
    :func:`conversation_is_unread` once per row, and that issued two of its own
    each time — fifty round trips to render one twenty-five row page.
    """
    ids = tuple(dict.fromkeys(conversation_ids))
    if not ids:
        return set()
    return set(
        db.scalars(
            unread_conversation_ids_select(person_id, conversation_ids=ids)
        ).all()
    )


def unread_conversation_count(db: Session, *, person_id: UUID) -> int:
    """How many active conversations this operator has not caught up on.

    One aggregate. This used to select every active conversation id and then
    issue two queries per id, so the badge alone cost tens of thousands of
    round trips on a production-sized inbox — on every page load.
    """
    return int(
        db.scalar(
            select(func.count(InboxConversation.id))
            .where(InboxConversation.is_active.is_(True))
            .where(InboxConversation.id.in_(unread_conversation_ids_select(person_id)))
        )
        or 0
    )


def rebuild_operator_read_state(
    db: Session,
    command: RebuildOperatorReadStateCommand,
) -> ReadStateRepairOutcome:
    """Idempotently clear impossible cross-conversation message cursors."""

    def operation() -> ReadStateRepairOutcome:
        query = select(InboxConversationReadState).with_for_update()
        if command.person_id is not None:
            query = query.where(
                InboxConversationReadState.person_id == command.person_id
            )
        states = list(db.scalars(query).all())
        repaired = 0
        for state in states:
            if state.last_read_message_id is None:
                continue
            message = db.get(InboxMessage, state.last_read_message_id)
            if message is not None and message.conversation_id == state.conversation_id:
                continue
            state.last_read_message_id = None
            repaired += 1
        db.flush()
        return ReadStateRepairOutcome(inspected=len(states), repaired=repaired)

    return execute_owner_command(
        db,
        definition=_REBUILD,
        context=command.context,
        operation=operation,
    )
